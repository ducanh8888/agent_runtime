"""What can a session actually reach today, and do sessions block each other?

Two claims in the P4 checklist are cheap to test before porting anything, and
either could shrink the work:

1. Whether the daemon's token or the provider key reach a session's environment.
   If they are already stripped, the dispatcher-authority item is mostly done.
2. Whether one busy session delays control of another. If it does not, the
   bounded-executor port may be solving a problem this daemon does not have.

Both run as real sessions, because the question is what an agent can see, not
what the code appears to pass.
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
info = daemon.ensure_running()
H = {"X-Session-API-Key": info.token}
B = info.base_url
PROFILE = str(bootstrap.ensure_profiles())


def dispatch(task: str, title: str, name: str) -> str:
    ws = os.path.join(SCRATCH, name)
    shutil.rmtree(ws, ignore_errors=True)
    os.makedirs(ws, exist_ok=True)
    return httpx.post(
        B + "/api/conversations",
        headers=H,
        timeout=120,
        json={
            "workspace": {"working_dir": ws},
            "agent_profile_id": PROFILE,
            "initial_message": {"role": "user", "content": [{"type": "text", "text": task}]},
            "title": title,
        },
    ).json()["id"]


def status(cid: str) -> str | None:
    return httpx.get(B + "/api/conversations/" + cid, headers=H, timeout=60).json().get(
        "execution_status"
    )


def wait(cid: str, seconds: int = 180) -> str | None:
    end = time.time() + seconds
    while time.time() < end:
        s = status(cid)
        if s in ("finished", "error"):
            return s
        time.sleep(3)
    return status(cid)


def transcript_text(cid: str) -> str:
    """Everything the session saw, as one blob, for substring searching."""
    out, page = [], None
    while True:
        params: dict[str, object] = {"limit": 100, "sort_order": "TIMESTAMP"}
        if page:
            params["page_id"] = page
        d = httpx.get(
            B + "/api/conversations/" + cid + "/events/search",
            headers=H, params=params, timeout=60,
        ).json()
        out.append(str(d.get("items")))
        page = d.get("next_page_id")
        if not page:
            break
    return "".join(out)


print("=== 1. does the daemon token or provider key reach a session? ===")
cid = dispatch(
    "Run this command and show the complete output:\n"
    "python -c \"import os,json; print(json.dumps(dict(os.environ), indent=0))\"\n"
    "Then stop. Do not create or edit any file.",
    "authority probe: environment",
    "authority_env",
)
print("session", cid[:8], "->", wait(cid))

blob = transcript_text(cid)
key = bootstrap.config.load_router_config().api_key
findings = [
    ("daemon token", info.token in blob),
    ("provider api key", key in blob),
    ("AGENTRT_SESSION_API_KEYS name", "AGENTRT_SESSION_API_KEYS" in blob),
    ("AGENTRT_9ROUTER name", "AGENTRT_9ROUTER" in blob),
]
for label, present in findings:
    print("  %-32s %s" % (label, "VISIBLE TO SESSION" if present else "not present"))

print("\n=== 2. does a busy session delay control of another? ===")
busy = dispatch(
    "Run this one command and wait for it to finish:\n"
    "python -c \"import time; time.sleep(120)\"\n"
    "Do not run anything else.",
    "authority probe: busy",
    "authority_busy",
)
for _ in range(40):
    if status(busy) == "running":
        break
    time.sleep(2)
print("busy session", busy[:8], "is", status(busy))

t0 = time.time()
other = dispatch("Reply with the single word OK and stop.", "authority probe: other", "authority_other")
dispatch_seconds = time.time() - t0
final = wait(other, 120)
total = time.time() - t0
print("  dispatch while busy took %.1fs; second session reached %s after %.1fs total"
      % (dispatch_seconds, final, total))

t0 = time.time()
httpx.post(B + "/api/conversations/" + busy + "/interrupt", headers=H, timeout=300)
print("  interrupting the busy session took %.1fs -> %s" % (time.time() - t0, status(busy)))

for c in (busy, other):
    httpx.delete(B + "/api/conversations/" + c, headers=H, timeout=60)
print("\nprobe sessions for part 2 deleted; the environment session is kept as evidence")
