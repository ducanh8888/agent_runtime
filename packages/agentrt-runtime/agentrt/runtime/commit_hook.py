"""Stop-hook checker for dispatch(require='commit')."""

from __future__ import annotations

import subprocess
import sys


REASON = (
    "task requires a commit: HEAD has not advanced since dispatch; "
    "commit your work before finishing"
)


def main() -> int:
    start_head = sys.argv[1]
    try:
        current = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        advanced = (
            current != start_head
            and subprocess.run(
                ["git", "merge-base", "--is-ancestor", start_head, current],
                check=False,
            ).returncode
            == 0
        )
    except (OSError, subprocess.CalledProcessError):
        advanced = False
    if advanced:
        return 0
    print(REASON, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
