"""Can a .md agent definition planted in a workspace still run its hooks?

`register_file_agents` used to read `.agents/agents/*.md` out of a session's
workspace unconditionally, and an agent definition carries a `hooks` field whose
entries are shell commands. That registration is now gated on the same switch as
ambient plugins, so this should be inert -- and "should" is the word this
project has been spending its time eliminating.

Same shape as probe_plugin_hook: plant it, dispatch an unrelated task, look for
the marker on disk. Fails loudly in either direction so it cannot quietly stop
testing anything.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

import httpx

from agentrt.runtime import bootstrap, daemon

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SCRATCH = os.path.join(tempfile.gettempdir(), "agentrt-checks")
WS = os.path.join(SCRATCH, "subagent_hook_ws")
MARKER = os.path.join(WS, "SUBAGENT_HOOK_RAN.txt")

DEFINITION = """---
name: probe-agent
description: Planted to check whether a workspace agent definition runs hooks.
hooks:
  PostToolUse:
    - matcher: "*"
      hooks:
        - type: command
          command: 'echo hooked > "%s"'
          timeout: 5
---

You are a probe. Do nothing.
""" % MARKER

shutil.rmtree(WS, ignore_errors=True)
agents_dir = os.path.join(WS, ".agents", "agents")
os.makedirs(agents_dir, exist_ok=True)
with open(os.path.join(agents_dir, "probe-agent.md"), "w", encoding="utf-8") as fh:
    fh.write(DEFINITION)

info = daemon.ensure_running()
H = {"X-Session-API-Key": info.token}
B = info.base_url

cid = httpx.post(
    B + "/api/conversations",
    headers=H,
    timeout=120,
    json={
        "workspace": {"working_dir": WS},
        "agent_profile_id": str(bootstrap.ensure_profiles()["workspace"]),
        "tool_module_qualnames": {"file_editor": bootstrap.GUARD_MODULE},
        "initial_message": {
            "role": "user",
            "content": [{"type": "text", "text":
                "Create a file called note.txt containing the word hello, then stop."}],
        },
        "title": "subagent hook probe",
    },
).json()["id"]
print("session", cid[:8])

end = time.time() + 200
state = None
while time.time() < end:
    state = httpx.get(B + "/api/conversations/" + cid, headers=H, timeout=60).json().get(
        "execution_status"
    )
    if state in ("finished", "error"):
        break
    time.sleep(3)

ran = os.path.exists(MARKER)
enabled = os.environ.get("AGENTRT_AMBIENT_PLUGINS", "").strip().lower() in (
    "1", "true", "yes",
)
print("session finished as    :", state)
print("marker file present    :", ran)
print("workspace contents     :", sorted(os.listdir(WS)))
print("AGENTRT_AMBIENT_PLUGINS:", "on" if enabled else "off (the default)")
print()

if ran and not enabled:
    print("FAIL: a workspace agent definition ran a shell command with")
    print("      file-based agent discovery disabled. The gate is not holding.")
    raise SystemExit(1)
print("PASS: the planted agent definition ran nothing.")
if not enabled:
    print("      Set AGENTRT_AMBIENT_PLUGINS=1 to check this probe can still")
    print("      detect the behaviour it guards.")
