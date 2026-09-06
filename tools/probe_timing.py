"""Time the pause and interrupt calls themselves.

Both verbs leave a session at `paused`, so the names suggest they are
interchangeable. They are not: this measures how long each call takes and
whether the command the agent is running survives it. Counting ticks between
sleeps was not enough -- it could not separate "the call returned quickly" from
"the call blocked while work continued".

Run it with the daemon configured; it dispatches two short sessions and deletes
them.
"""
import os
import shutil
import tempfile
import sys
import time

import httpx

from agentrt.runtime import bootstrap, daemon

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
# Workspaces go to a temp directory, not into the repository.
SCRATCH = os.path.join(tempfile.gettempdir(), "agentrt-checks")
info = daemon.ensure_running()
H = {"X-Session-API-Key": info.token}
B = info.base_url
PROFILE = str(bootstrap.ensure_profiles()["workspace"])


def probe(verb: str) -> None:
    ws = os.path.join(SCRATCH, "timing_" + verb)
    shutil.rmtree(ws, ignore_errors=True)
    os.makedirs(ws, exist_ok=True)
    shutil.copy(os.path.join(HERE, "tick.py"), os.path.join(ws, "tick.py"))

    cid = httpx.post(
        B + "/api/conversations",
        headers=H,
        timeout=120,
        json={
            "workspace": {"working_dir": ws},
            "agent_profile_id": PROFILE,
            "initial_message": {
                "role": "user",
                "content": [{"type": "text", "text":
                    "Run this one command and wait for it to finish:\n"
                    "python tick.py\n"
                    "Do not create or edit any file. Do not run anything else."}],
            },
            "title": "timing " + verb,
        },
    ).json()["id"]

    def ticks() -> int:
        p = os.path.join(ws, "ticks.txt")
        if not os.path.exists(p):
            return 0
        with open(p) as fh:
            return sum(1 for _ in fh)

    for _ in range(90):
        if ticks() >= 3:
            break
        time.sleep(1)
    if ticks() < 3:
        print("%-9s never started" % verb)
        return

    before = ticks()
    t0 = time.time()
    r = httpx.post(B + "/api/conversations/" + cid + "/" + verb, headers=H, timeout=300)
    call_seconds = time.time() - t0
    right_after = ticks()

    time.sleep(15)
    later = ticks()
    state = httpx.get(B + "/api/conversations/" + cid, headers=H, timeout=60).json().get(
        "execution_status"
    )

    print(
        "%-9s POST took %6.1fs (http %s) | ticks before=%d immediately_after=%d +15s=%d | status=%s"
        % (verb, call_seconds, r.status_code, before, right_after, later, state)
    )
    print(
        "          -> the call %s; the command %s"
        % (
            "BLOCKS" if call_seconds > 5 else "returns at once",
            "kept running" if later - right_after >= 10 else "stopped",
        )
    )
    httpx.delete(B + "/api/conversations/" + cid, headers=H, timeout=60)


for v in ("interrupt", "pause"):
    probe(v)
