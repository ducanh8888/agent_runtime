#!/usr/bin/env python3
"""Validate the repository documentation registry, names and local links."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
MANIFEST = DOCS / "manifest.json"
NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*\.md$")
LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
TYPES = {"guide", "plan", "reference", "research", "result"}
LIFECYCLES = {"active", "current", "complete", "historical"}
AUTHORITIES = {"normative", "reference", "evidence", "informative"}


def _local_target(
    source: Path, raw: str, optional_roots: tuple[Path, ...]
) -> Path | None:
    target = raw.strip().strip("<>")
    if not target or target.startswith("#") or "://" in target:
        return None
    target = unquote(target.split("#", 1)[0])
    if not target:
        return None
    resolved = (source.parent / target).resolve()
    if any(resolved.is_relative_to(root) for root in optional_roots):
        return None
    return resolved


def main() -> int:
    errors: list[str] = []
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        errors.append("manifest schema_version must be 1")

    documents = data.get("documents")
    if not isinstance(documents, list):
        errors.append("manifest documents must be a list")
        documents = []

    optional_values = data.get("optional_link_roots", [])
    if not isinstance(optional_values, list) or not all(
        isinstance(value, str) for value in optional_values
    ):
        errors.append("manifest optional_link_roots must be a list of strings")
        optional_values = []
    optional_roots = tuple((ROOT / value).resolve() for value in optional_values)

    ids: set[str] = set()
    paths: set[str] = set()
    ordered_ids: list[str] = []
    active_plans: list[str] = []
    for index, document in enumerate(documents):
        if not isinstance(document, dict):
            errors.append(f"documents[{index}] must be an object")
            continue
        identifier = document.get("id")
        path = document.get("path")
        if not isinstance(identifier, str) or not identifier:
            errors.append(f"documents[{index}].id must be a non-empty string")
        elif identifier in ids:
            errors.append(f"duplicate document id: {identifier}")
        else:
            ids.add(identifier)
            ordered_ids.append(identifier)
        if not isinstance(path, str) or not path.startswith("docs/"):
            errors.append(f"documents[{index}].path must start with docs/")
            continue
        if path in paths:
            errors.append(f"duplicate document path: {path}")
        paths.add(path)
        resolved = (ROOT / path).resolve()
        if not resolved.is_relative_to(DOCS) or not resolved.is_file():
            errors.append(f"manifest path does not exist under docs: {path}")
        if document.get("type") not in TYPES:
            errors.append(f"invalid type for {identifier}: {document.get('type')}")
        if document.get("lifecycle") not in LIFECYCLES:
            errors.append(
                f"invalid lifecycle for {identifier}: {document.get('lifecycle')}"
            )
        if document.get("authority") not in AUTHORITIES:
            errors.append(
                f"invalid authority for {identifier}: {document.get('authority')}"
            )
        if document.get("type") == "plan" and document.get("lifecycle") == "active":
            active_plans.append(path)

    if ordered_ids != sorted(ordered_ids):
        errors.append("manifest documents must be sorted by id")
    if active_plans != [data.get("active_plan")]:
        errors.append("manifest active_plan must identify the single active plan")

    markdown = sorted(DOCS.rglob("*.md"))
    registered = {
        str(path.relative_to(ROOT)) for path in markdown if path != DOCS / "README.md"
    }
    for missing in sorted(registered - paths):
        errors.append(f"Markdown document missing from manifest: {missing}")
    for stale in sorted(paths - registered):
        errors.append(f"manifest entry is not a Markdown document: {stale}")

    for path in markdown:
        if path.name != "README.md" and not NAME.fullmatch(path.name):
            errors.append(
                f"document name is not lowercase kebab-case: {path.relative_to(ROOT)}"
            )
        for part in path.relative_to(DOCS).parts[:-1]:
            if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", part):
                errors.append(f"document directory is not lowercase kebab-case: {part}")

    sources = [ROOT / "README.md", ROOT / "CLAUDE.md", *markdown]
    for source in sources:
        for raw in LINK.findall(source.read_text(encoding="utf-8")):
            target = _local_target(source, raw, optional_roots)
            if target is not None and not target.exists():
                errors.append(f"broken local link in {source.relative_to(ROOT)}: {raw}")

    for key in ("entrypoint", "active_plan"):
        value = data.get(key)
        if not isinstance(value, str) or not (ROOT / value).is_file():
            errors.append(f"manifest {key} does not name an existing file")

    if errors:
        print("documentation validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"documentation validation passed: {len(documents)} registered documents")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
