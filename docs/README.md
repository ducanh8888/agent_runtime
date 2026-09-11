# Documentation index

This directory separates current operating guidance from plans, research and
immutable result records. Machine consumers should read
[`manifest.json`](manifest.json) instead of inferring authority from filenames.

## Start here

1. [DeepSeek hardening plan](plans/deepseek-hardening.md) — active H0–H7 plan.
2. [Daemon behavior](reference/daemon-behavior.md) — measured runtime behavior.
3. [Orchestration guide](guides/orchestration.md) — how to dispatch and verify.
4. [H2 result](results/h2.md) — latest completed stage evidence.

## Layout

| Directory | Purpose | Authority |
|---|---|---|
| `guides/` | Procedures for operators and developers | Current guidance |
| `plans/` | Active and historical implementation plans | Check manifest lifecycle |
| `reference/` | Current measured behavior and contracts | Current reference |
| `research/` | Audits, reconnaissance and decision history | Informative/historical |
| `results/` | Immutable phase and baseline evidence | Evidence for its revision |

## Naming and lifecycle contract

- Paths use lowercase kebab-case. Phase result IDs stay lowercase (`h0`, `p4`).
- `manifest.json` is the canonical document registry. Every Markdown document
  except this index must appear exactly once.
- `lifecycle` is one of `active`, `current`, `complete`, or `historical`.
- `authority` is one of `normative`, `reference`, `evidence`, or `informative`.
- Result records are append-only evidence. Correct broken links or metadata,
  but put new facts in a new result/current document rather than rewriting what
  an old revision observed.
- Use relative Markdown links. Do not add uppercase filenames, spaces,
  duplicated status indexes, or links to planned result files that do not exist.
- When moving or adding documentation, update `manifest.json`, root entrypoints,
  and every internal link in the same commit.
- Historical research may link into the optional, untracked `repos/` source
  corpus. That exception is declared in `manifest.json`; all other local links
  must resolve in a normal checkout.

Validate documentation changes from the repository root:

```bash
python3 tools/check_docs.py
```
