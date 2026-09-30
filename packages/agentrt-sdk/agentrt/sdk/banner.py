"""Optional AgentRT startup banner."""

import os
import sys


_BANNER_PRINTED = False


def _print_banner(version: str) -> None:
    """Print the AgentRT startup banner when explicitly enabled."""
    global _BANNER_PRINTED

    if os.environ.get("AGENTRT_SHOW_BANNER") != "1" or _BANNER_PRINTED:
        return
    _BANNER_PRINTED = True

    banner = f"""\
+----------------------------------------------------------------------+
|  AgentRT SDK v{version:<54}|
|                                                                      |
|  Background coding-agent sessions                                   |
|  Set AGENTRT_SHOW_BANNER=1 to show this message                      |
+----------------------------------------------------------------------+
"""
    print(banner, file=sys.stderr)
