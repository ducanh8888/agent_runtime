"""Does a plugin planted in a session's own workspace get its hooks executed?

`_ensure_plugins_loaded` calls `load_available_plugins(work_dir=...,
include_project=True)`, and a plugin carries a `hooks/hooks.json` whose entries
are shell commands. If that path is live, a session can arrange for arbitrary
commands to run by writing files inside the workspace it was given -- which is a
wider hole than the sub-agent `hooks` field the plan set out to close.

Reading the source says it should happen. This makes it happen or proves it does
not.

The hook writes a marker file and nothing else. It is a plain `echo`, not a
script: a hooks.json pointing at a `.sh` on Windows makes the shell fall back to
ShellExecute and put a "Pick an app" dialog on the user's screen.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time

import httpx

from agentrt.runtime import bootstrap, daemon

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SCRATCH = os.path.join(tempfile.gettempdir(), "agentrt-checks")
WS = os.path.join(SCRATCH, "plugin_hook_ws")
MARKER = os.path.join(WS, "HOOK_RAN.txt")


def plant_plugin() -> None:
    """Write a minimal plugin whose PostToolUse hook drops a marker file."""
    shutil.rmtree(WS, ignore_errors=True)
    root = os.path.join(WS, ".agents", "plugins", "probe")
    os.makedirs(os.path.join(root, ".plugin"), exist_ok=True)
    os.makedirs(os.path.join(root, "hooks"), exist_ok=True)

    with open(os.path.join(root, ".plugin", "plugin.json"), "w", encoding="utf-8") as fh:
        json.dump(
            {
                "name": "probe",
                "version": "1.0.0",
                "description": "Checks whether workspace plugin hooks execute",
            },
            fh,
        )

    with open(os.path.join(root, "hooks", "hooks.json"), "w", encoding="utf-8") as fh:
        json.dump(
            {
                "hooks": {
                    "PostToolUse": [
                        {
                            "matcher": "*",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": 'echo hooked > "%s"' % MARKER,
                                    "timeout": 5,
                                }
                            ],
                        }
                    ]
                }
            },
            fh,
        )


plant_plugin()
info = daemon.ensure_running()
H = {"X-Session-API-Key": info.token}
B = info.base_url

cid = httpx.post(
    B + "/api/conversations",
    headers=H,
    timeout=120,
    json={
        "workspace": {"working_dir": WS},
        "agent_profile_id": str(bootstrap.ensure_profiles()),
        "initial_message": {
            "role": "user",
            "content": [{"type": "text", "text":
                "Create a file called note.txt containing the word hello, then stop."}],
        },
        "title": "plugin hook probe",
    },
).json()["id"]
print("session", cid[:8])

end = time.time() + 180
state = None
while time.time() < end:
    state = httpx.get(B + "/api/conversations/" + cid, headers=H, timeout=60).json().get(
        "execution_status"
    )
    if state in ("finished", "error"):
        break
    time.sleep(3)

ran = os.path.exists(MARKER)
print("session finished as    :", state)
print("marker file present    :", ran)
print("workspace contents     :", sorted(os.listdir(WS)))
print()
enabled = os.environ.get("AGENTRT_AMBIENT_PLUGINS", "").strip().lower() in (
    "1", "true", "yes",
)
print("AGENTRT_AMBIENT_PLUGINS:", "on" if enabled else "off (the default)")
print()

if ran and not enabled:
    print("FAIL: the hook ran with ambient plugins disabled. The gate in")
    print("      local_conversation._ensure_plugins_loaded is not holding.")
    raise SystemExit(1)
if not ran and enabled:
    print("FAIL: the hook did not run with ambient plugins enabled. Either the")
    print("      switch is broken or this probe no longer reproduces the path")
    print("      it is meant to guard -- a test that cannot fail guards nothing.")
    raise SystemExit(1)
if ran:
    print("EXPECTED: with the switch on, a plugin in the workspace runs shell")
    print("          commands. This is the behaviour the default exists to stop.")
else:
    print("PASS: a plugin planted in the session's own workspace did not run.")
    print("      Set AGENTRT_AMBIENT_PLUGINS=1 and re-run to confirm this probe")
    print("      still reproduces the hole it guards.")
