# AgentRT development rules

These rules apply to repository-level work. Files under `packages/` also follow
the nearest package `AGENTS.md`.

## Read order

1. Read [`docs/README.md`](docs/README.md) and its machine-readable
   [`manifest.json`](docs/manifest.json).
2. Read the active plan named by `active_plan` in the manifest.
3. For code changes, read the closest package `AGENTS.md` before editing.

## Working rules

- Treat agent summaries as claims. Inspect diffs/artifacts and run the relevant
  reproduction before accepting them.
- Preserve unrelated user changes and existing sessions. Use disposable state
  and workspaces for integration probes.
- Keep provider policy in the AgentRT deployment layer; do not make the generic
  SDK DeepSeek-specific.
- Never print or commit credentials, provider reasoning, state `.env` contents,
  or unredacted profile payloads.
- Use `apply_patch` for intentional content edits. Mechanical formatting and
  tracked renames may use their dedicated tools.
- Follow package test conventions. Do not use `mypy`; use Pyright, Ruff,
  pycodestyle, import rules and tool-registration checks where applicable.
- Every commit must include
  `Co-authored-by: openhands <openhands@all-hands.dev>`.

## Documentation rules

- Follow [`docs/README.md`](docs/README.md): lowercase kebab-case paths,
  category directories, one canonical manifest and explicit lifecycle.
- Historical result records are evidence for their recorded revision; do not
  silently rewrite their conclusions.
- Add or move a document only when `docs/manifest.json` and all links are
  updated in the same commit.
- Run `python3 tools/check_docs.py` for every documentation change.
