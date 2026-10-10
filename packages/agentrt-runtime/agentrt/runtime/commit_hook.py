"""Stop-hook checker for dispatch(require='commit')."""

from __future__ import annotations

import re
import subprocess
import sys


REASON = (
    "task requires a commit: HEAD has not advanced since dispatch; "
    "commit your work before finishing"
)


_COMMITTING = re.compile(r"(commit|rebase|cherry-pick|merge|revert|am)\b")


def _committed_since(start_head: str) -> bool | None:
    """Whether HEAD's reflog records committing work after it was at ``start_head``.

    Used when ``start_head`` is no longer an ancestor of HEAD, which is what a
    rebase does (#6): the session's commits are all there, on a new base.
    ``None`` when the reflog never saw ``start_head``, so the caller cannot tell.
    """
    log = subprocess.run(
        ["git", "reflog", "show", "--format=%H %gs", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for line in log.splitlines():  # newest first
        sha, _, action = line.partition(" ")
        if sha == start_head:
            return False
        if _COMMITTING.match(action):
            return True
    return None


def main() -> int:
    start_head = sys.argv[1]
    try:
        current = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        advanced = current != start_head
        if advanced and (
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", start_head, current],
                check=False,
            ).returncode
            != 0
        ):
            # History was rewritten. Blocking here told a session that had
            # committed, then rebased, that nothing had advanced, so it made
            # empty commits for ~100 iterations. Only block when the reflog
            # shows no commit-like step; when it cannot tell, let it finish.
            advanced = _committed_since(start_head) is not False
    except (OSError, subprocess.CalledProcessError):
        advanced = False
    if advanced:
        return 0
    print(REASON, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
