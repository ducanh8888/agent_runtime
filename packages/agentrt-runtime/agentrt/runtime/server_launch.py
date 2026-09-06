"""Start the vendored agent server with every workspace kind registered.

``BaseWorkspace`` is a discriminated union, and a member of that union only
exists once the module defining it has been imported. The server imports the
local and remote kinds and nothing else, so a request naming
``kind: "DockerWorkspace"`` was rejected during validation -- before docker was
ever consulted -- with an assertion carrying an empty message. The daemon log
showed a validation error and no docker output at all, which is the whole
diagnosis: nothing had gone wrong with the container, because nothing had tried
to start one.

Importing here rather than patching the vendored server keeps the fork's edit
surface small, and it is the only place that knows which kinds this runtime
means to support.
"""

from __future__ import annotations

import runpy


def _register_workspace_kinds() -> list[str]:
    """Import the workspace classes, returning the names that registered.

    Failures are swallowed on purpose. ``agentrt-workspace`` brings a container
    stack that an installation may not want, and a runtime that cannot use
    Docker should still start and run local sessions rather than refusing to
    boot over a workspace kind nobody asked for.
    """
    registered: list[str] = []
    try:
        from agentrt.workspace.docker.workspace import DockerWorkspace  # noqa: F401

        registered.append("DockerWorkspace")
    except Exception:  # pragma: no cover - depends on what is installed
        pass
    try:
        from agentrt.workspace.docker.dev_workspace import (  # noqa: F401
            DockerDevWorkspace,
        )

        registered.append("DockerDevWorkspace")
    except Exception:  # pragma: no cover - depends on what is installed
        pass
    return registered


def main() -> None:
    _register_workspace_kinds()
    # run_name="__main__" so the server's own entry point runs exactly as it
    # does under `python -m agentrt.agent_server`; argv is untouched, so its
    # argument parsing is unaffected.
    runpy.run_module("agentrt.agent_server", run_name="__main__")


if __name__ == "__main__":
    main()
