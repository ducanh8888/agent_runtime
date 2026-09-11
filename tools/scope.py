#!/usr/bin/env python3
"""Run the narrow test selection for an area instead of the full suites.

The whole server suite takes about five minutes and the SDK conversation suite
about half a minute. A phase usually touches a fraction of that, and running
everything after each edit is time the work does not get back. Name the areas
you changed, or name the phase:

    python3 tools/scope.py runtime server-workspace
    python3 tools/scope.py --phase h4
    python3 tools/scope.py --list

Run the full suites once, at the phase gate, not on every edit. Two notes that
cost real time to learn: always run from ``packages/`` so the repository's
``addopts`` markers apply (from the repository root the same command collects
the stress suite and does not finish), and run each area as its own selection
because a test-local ``Tool`` subclass left in the process-wide registry makes a
later OpenAPI export fail when the suites share one process.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ROOT / "packages"
PYTHON = PACKAGES / ".venv" / "bin" / "python"

#: Area name -> pytest paths, relative to ``packages/``.
AREAS: dict[str, list[str]] = {
    "runtime": ["tests/runtime"],
    "conversation": ["tests/sdk/conversation"],
    "llm": ["tests/sdk/llm"],
    "git": ["tests/sdk/git"],
    "workspace": ["tests/sdk/workspace", "tests/workspace"],
    "file-editor": ["tests/tools/file_editor"],
    "tools": ["tests/tools"],
    "server-core": [
        "tests/agent_server/test_conversation_service.py",
        "tests/agent_server/test_event_service.py",
        "tests/agent_server/test_models.py",
        "tests/agent_server/test_conversation_router.py",
        "tests/agent_server/test_conversation_response.py",
        "tests/agent_server/test_conversation_tags.py",
        "tests/agent_server/test_h2_request_scope.py",
        "tests/agent_server/test_h3_finalize.py",
        "tests/agent_server/test_h5_admission.py",
        # These build a mock `stored` record with explicit field values, so a
        # new projected field is caught here or not at all: they were missed by
        # the H4/H5 narrow runs and only the full suite failed.
        "tests/agent_server/test_agent_profile_conv_start.py",
        "tests/agent_server/test_agent_launch_additions.py",
        "tests/agent_server/test_auto_title_span_metadata.py",
        "tests/agent_server/test_deployment_llm_policy_wiring.py",
    ],
    "server-workspace": [
        "tests/agent_server/test_workspace_router.py",
        "tests/agent_server/test_workspaces_router.py",
        "tests/agent_server/test_workspace_cookie_auth.py",
    ],
    "server-events": [
        "tests/agent_server/test_event_streaming.py",
        "tests/agent_server/test_event_router.py",
        "tests/agent_server/test_event_router_websocket.py",
    ],
    "server-openapi": [
        "tests/agent_server/test_openapi_contract.py",
        "tests/agent_server/test_openapi_discriminator.py",
    ],
}

#: Phase -> areas that phase is expected to touch. Extended as phases land.
PHASES: dict[str, list[str]] = {
    "h5": ["server-core", "server-openapi", "runtime"],
    "h4": [
        "workspace",
        "file-editor",
        "git",
        "server-workspace",
        "server-core",
        "runtime",
    ],
}

#: Paths that do not exist in this checkout are skipped rather than failing the
#: run, so an area can name a file that a later fork renames.
def _existing(paths: list[str]) -> list[str]:
    return [path for path in paths if (PACKAGES / path).exists()]


def _selection(args: argparse.Namespace) -> list[str]:
    areas: list[str] = []
    if args.phase:
        phases = {name.strip() for name in args.phase.split(",") if name.strip()}
        unknown = phases - set(PHASES)
        if unknown:
            raise SystemExit(f"unknown phase(s): {', '.join(sorted(unknown))}")
        for phase in phases:
            areas.extend(PHASES[phase])
    areas.extend(args.areas)

    unknown = [area for area in areas if area not in AREAS]
    if unknown:
        raise SystemExit(
            f"unknown area(s): {', '.join(unknown)}. Use --list to see them."
        )

    paths: list[str] = []
    for area in areas:
        for path in AREAS[area]:
            if path not in paths:
                paths.append(path)
    return _existing(paths)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("areas", nargs="*", help="areas to run (see --list)")
    parser.add_argument("--phase", help="run the areas a phase touches")
    parser.add_argument("--list", action="store_true", help="list areas and phases")
    parser.add_argument("-k", dest="keyword", help="pass a -k expression to pytest")
    args = parser.parse_args()

    if args.list:
        for area, paths in sorted(AREAS.items()):
            print(f"{area:16} {' '.join(paths)}")
        for phase, areas in sorted(PHASES.items()):
            print(f"phase {phase:12} {' '.join(areas)}")
        return 0

    paths = _selection(args)
    if not paths:
        parser.error("name at least one area, or a phase")

    command = [str(PYTHON), "-m", "pytest", *paths, "-q", "-p", "no:cacheprovider"]
    if args.keyword:
        command += ["-k", args.keyword]
    print(f"$ cd {PACKAGES} && {' '.join(command)}", flush=True)
    return subprocess.call(command, cwd=PACKAGES)


if __name__ == "__main__":
    sys.exit(main())
