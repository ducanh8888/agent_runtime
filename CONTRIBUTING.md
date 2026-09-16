# Contributing

The rules for working in this repository live in one place —
[AGENTS.md](AGENTS.md) — so they are the same whether a person or a coding
agent is doing the work. Read it first; it covers the repository layout, the
invariants, the test and lint commands, and what "done" means.

This file exists only to say where to start, not to restate those rules.

## Before you open a change

1. **Reproduce the thing you are changing.** The project's working rule is that
   a claim is not evidence: if you are fixing a defect, show it failing first,
   and show the test failing without your fix.
2. **Run the checks in [docs/guides/testing.md](docs/guides/testing.md)**,
   which lists narrow selections first and the full suites at the gate, plus
   the known environment-dependent failures so you do not chase one.
3. **If you touched documentation**, run `python3 tools/check_docs.py` from the
   repository root. It validates the manifest registry and every local link.
4. **Say what you measured.** Commit messages here record what was verified and
   what was rejected, not just what changed.

## Status

Pre-1.0 (`agentrt-runtime` 0.1.0). There are no release branches and no
backports; changes land on `main`. Work in progress, and what is deliberately
not being done, is tracked in the
[hardening plan](docs/plans/deepseek-hardening.md) rather than in this file.

## Vendored code

Most of `packages/` is a renamed fork of OpenHands'
`software-agent-sdk`. Changes there follow the rules in
[AGENTS.md](AGENTS.md#vendored-and-upstream-code), which are stricter than for
`agentrt-runtime`, because a change to the fork is a change you will have to
carry across every future upstream revision.

## Reporting security issues

See [SECURITY.md](SECURITY.md). Please do not open a public issue for those.
