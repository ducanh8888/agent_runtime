"""Extract the daemon's live REST surface as a compact reference.

The point is to stop guessing. When a spec asks for code that calls an endpoint,
the cheapest thing to hand the implementer is the endpoint as it actually is --
its path, its parameters, and the field names it really returns. Everything else
is inference, and inference is both billed and occasionally wrong: `status`
versus `execution_status` cost a debugging round and a 237-second polling loop
that should have taken 9.

Usage:
    python tools/api_context.py                 # whole conversation surface
    python tools/api_context.py --filter events # only paths containing "events"
"""

from __future__ import annotations

import argparse
import json
import sys

import httpx

from agentrt.runtime import daemon


def _resolve(schema: dict, spec: dict, depth: int = 0) -> dict | None:
    """Follow a $ref one level so field names become visible."""
    if depth > 3 or not isinstance(schema, dict):
        return None
    ref = schema.get("$ref")
    if ref and ref.startswith("#/components/schemas/"):
        name = ref.rsplit("/", 1)[1]
        return spec.get("components", {}).get("schemas", {}).get(name)
    return schema


def _fields(schema: dict | None, spec: dict) -> list[str]:
    schema = _resolve(schema or {}, spec)
    if not isinstance(schema, dict):
        return []
    props = schema.get("properties")
    if isinstance(props, dict):
        return sorted(props)
    # Paged responses hide the interesting shape one level down.
    items = schema.get("items")
    if items:
        return _fields(items, spec)
    return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--filter", default="conversations")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    info = daemon.ensure_running()
    spec = httpx.get(
        f"{info.base_url}/openapi.json",
        headers={"X-Session-API-Key": info.token},
        timeout=60,
    ).json()

    lines: list[str] = [
        "Authoritative REST surface, read from the running daemon.",
        "Every call needs the header X-Session-API-Key.",
        "",
    ]
    for path, methods in sorted(spec.get("paths", {}).items()):
        if args.filter and args.filter not in path:
            continue
        for method, op in sorted(methods.items()):
            if method not in ("get", "post", "patch", "delete", "put"):
                continue
            lines.append(f"{method.upper()} {path}")
            summary = (op.get("summary") or "").strip()
            if summary:
                lines.append(f"    {summary}")
            params = [
                f"{p.get('name')}{'' if p.get('required') else '?'}"
                for p in op.get("parameters", [])
            ]
            if params:
                lines.append("    query/path: " + ", ".join(params))
            body = (
                op.get("requestBody", {})
                .get("content", {})
                .get("application/json", {})
                .get("schema")
            )
            bf = _fields(body, spec)
            if bf:
                lines.append("    body fields: " + ", ".join(bf))
            ok = (
                op.get("responses", {})
                .get("200", op.get("responses", {}).get("201", {}))
                .get("content", {})
                .get("application/json", {})
                .get("schema")
            )
            rf = _fields(ok, spec)
            if rf:
                lines.append("    returns: " + ", ".join(rf))
            lines.append("")

    text = "\n".join(lines)
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        print(f"wrote {args.out} ({text.count(chr(10))} lines)")
    else:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
