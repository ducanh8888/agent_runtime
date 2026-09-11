# H1 — contiguous reads, guarded inspection and read evidence

Date: 2026-09-11 (Asia/Bangkok).
Implementation baseline: `a584e47e5be564e16968204c94e0f1fc4ecd8e5d`.
Verified code revision: `5b19bc8` (documentation/release record follows it).

## Outcome

H1 passed its source, compatibility, adversarial and disposable live gates. It
adds an `inspect` preset for useful repository inspection without a general
terminal or file mutation, exact paged file views, bounded structured search
and Git reads, typed artifact outcomes, and versioned read evidence. The
candidate was not installed into the production daemon; H7 still owns the one
production cutover.

## What shipped

- File views return exact page content plus encoding, file size/hash, requested
  and returned line/character ranges, continuation cursor, partial-line state,
  EOF and truncation. Concatenating pages reconstructs LF, CRLF, Unicode,
  missing-final-newline and long-line fixtures byte-for-byte after decoding.
- Cursors bind continuation to the file version and view options. Mutation is
  reported as `file_changed`; empty, binary, decoding and out-of-range cases
  have explicit outcomes rather than fabricated source lines.
- `inspect` sits between `readonly` and `workspace`. It has guarded file views,
  bounded structured search, narrow read-only Git operations, executable
  versions, sanitized environment metadata and task tracking, but no terminal
  and no file-editor mutation.
- Search applies the workspace and protected-state guards to every candidate,
  bounds line/file/output sizes, uses a regex deadline, and stops counting at
  10,000 matches. `match_count_exact=false` distinguishes that cap from an
  exact count and pagination continues with an offset.
- Git reads validate argv and disable ambient Git redirection, credential
  helpers, external diff, textconv, filters, hooks, pagers, fsmonitor and
  optional locks as appropriate. Git-delta archives use a scratch index and do
  not execute repository clean/process filters.
- Runtime and server readers refuse direct protected-state paths and known
  same-inode aliases, including explicit persistence/conversation roots.
  Ordinary files avoid the previous per-read scan of every conversation.
- Read evidence is derived from successful file-editor observations, grouped by
  file version, and records returned ranges, partial-line offsets and honest
  observed/delivered/understood stages. Repeated-read warnings compare actual
  returned spans, so normal continuation is not mislabeled as repetition.
- Artifact listing keeps legacy fields while adding typed outcomes for listed,
  empty, truncated, unfiltered, partial, unavailable and failed scans. An empty
  list from a read-only session is documented as a successful result.

## Verification

Targeted and domain gates at the final code revision:

```text
runtime + file-editor suites                           258 passed
SDK Git + public-skill compatibility                  168 passed
server file/workspace routes                          119 passed
focused final security/inspect regression             154 passed
changed Python files under Pyright                      0 errors
Ruff, format, pycodestyle, import and registration     passed
```

The server suite emits one upstream Starlette/httpx deprecation warning. No H1
test failed. `uv lock --check` and imports from the frozen workspace environment
also pass.

A disposable candidate daemon used state
`/tmp/agentrt-h1-live.PD59UD` and workspace
`/tmp/agentrt-h1-live-ws.O3PnVW`. Inspect session
`605f97ff` returned `H1_INSPECT_OK 5`; its workspace stayed clean and artifact
outcome was typed empty. Persisted file evidence carried the actual hash,
returned range and EOF. The persisted LLM profile was direct
`openai/deepseek-flash`, Chat Completions, thinking enabled and reasoning effort
`high`. The candidate daemon was stopped; production PID 20796 was not
restarted.

## Review evidence and rejected claims

AgentRT evidence sessions retained:

- writers: `37877e4c`, `31f68d85`, `7076c375`, `f200a364`;
- integration reviews: `9e3b6741`, `2a9b2a4b`, `cc9862c3`;
- first final snapshot: `ce5460fc`, `5d79f785`, `af43480b`;
- adversarial delta reviews: `16ecce30`, `ce1fc8c6`, `1f451e48`.

Worker summaries were treated as claims. Root review rejected the first
integration snapshot after finding incomplete server credential guards,
repository helper execution, unbounded output and inaccurate evidence ranges.
The `9d3b4cd` snapshot was also not declared complete: reviewers reproduced an
unbounded state-directory scan, Git archive clean/process filter execution,
ambient `GIT_DIR` redirection, dense-search memory growth, duplicate metadata,
false repeated-read warnings and partial-line coverage overclaim. Each
reproduction received a focused regression before this result was recorded.
The last delta review then found a worktree-config variant of the Git filter,
a state-inside-workspace regression, upload/download check-open races, an
inexact clipped-line count and a non-advancing search offset at the 10,000-match
cap. Those findings produced commit `5b19bc8` and its final 154-test regression
gate.

One reviewer also claimed it created no repository files because `git status`
was clean. `artifacts` disproved that claim by listing generated, ignored
`*.egg-info` files in its review worktree; the other two reviewers had typed
empty artifact results. None of those files entered the integration branch,
but the mismatch reinforces that a clean tracked tree is not evidence of zero
filesystem writes.

The review run itself reproduced current orchestration shortcomings that remain
assigned to later phases: dispatch initially reports `idle`, tag keys reject
underscores without advertising the rule, and completion still requires status
polling. H2/H5 retain those contracts.

## Remaining limits

- Evidence proves what a tool returned. Serialized-request delivery is not yet
  wired, so `delivered` and `understood` remain `unknown` rather than being
  inferred. H6 can extend per-call provenance without changing this truth model.
- A cursor is a deterministic continuity token, not an authorization token.
  Protected-state checks still run on every access.
- The guarded model-side file editor still cannot close a hostile concurrent
  symlink swap between validation and its later open; server downloads now pin
  and stream a verified descriptor, and uploads recheck the opened inode. A
  credential copied into an ordinary file or committed into Git history has a
  new inode and is not discoverable as an alias. Those cases require workspace
  trust/snapshot isolation or an OS sandbox; H4 reduces mutation races but does
  not claim sandboxing.
- Search deliberately stops after 10,000 matches. Its typed inexact count tells
  callers to narrow the query; it does not attempt an expensive exact count.
- Search continuation is an offset and rescans the bounded window; unlike file
  paging it is not version-bound. H4 snapshot pinning owns stable reads while a
  separate writer is mutating the repository.
- A non-secret hard-linked candidate still requires an exact protected-inode
  comparison and therefore scans persisted conversation metadata. Generated
  directories are pruned, but repositories dominated by hard links can make
  inspection slower; correctness takes precedence over caching without
  write-side invalidation.
- `inspect` prevents workspace writes, while `task_tracker` may persist its own
  task state under the conversation state directory. It is not a zero-write
  process profile.
- H1 is complete in source only. The installed production runtime remains the
  pre-H1 build until the H7 migration/rollback gate.

## Rollback

H1 adds fields and one permission profile without a persisted-state migration.
Source rollback is a normal revert of the H1 commits. Existing sessions are not
retargeted or resumed. Do not install or restart an older binary over state
after a future phase introduces a migration.
