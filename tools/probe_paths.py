"""Dispatch into a workspace whose path has spaces and diacritics.

Every path exercised while building this was ASCII and space-free, which is not
what a Windows machine looks like: user directories carry real names, and
a user directory with a real name is the ordinary case, not the exotic one. Two
things are under test and they fail separately -- the *directory* containing
non-ASCII, and a *file* the session names in non-ASCII, which additionally has
to survive percent-encoding through the daemon's workspace endpoint.

Costs one short session.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

from agentrt.runtime.client import Client

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WS = os.path.join(tempfile.gettempdir(), "agentrt-checks", "Du an thu nghiem", "thu muc con")
WS = WS.replace("Du an thu nghiem", "D\u1ef1 \u00e1n th\u1eed nghi\u1ec7m")
shutil.rmtree(os.path.dirname(WS), ignore_errors=True)
os.makedirs(WS)
with open(os.path.join(WS, "app.py"), "w", encoding="utf-8") as fh:
    fh.write("def add(a, b):\n    return a - b\n")

client = Client()
failures: list[str] = []


def check(label: str, passed: bool, detail: str = "") -> None:
    print("  %-50s %s%s" % (label, "PASS" if passed else "FAIL", (" -- " + detail) if detail else ""))
    if not passed:
        failures.append(label)


print("workspace:", WS, "\n")

session = client.dispatch(
    workspace=WS,
    task=(
        "app.py has a function add(a, b) that subtracts instead of adding. Fix it. "
        "Then write a file named exactly 'bao cao.md' in the same directory, one line, "
        "saying what you changed. Create no other file."
    ),
    permission="workspace",
    title="probe: a workspace path with spaces and diacritics",
)
sid = session["id"]
print("dispatched", session["short_id"])

deadline = time.time() + 300
while time.time() < deadline:
    if client.status(sid).get("status") in ("finished", "error"):
        break
    time.sleep(3)

status = client.status(sid)
check("the session did not error", status.get("status") == "finished", str(status.get("status")))
check("the daemon kept the workspace path intact", os.path.normcase(status.get("workspace") or "") == os.path.normcase(WS),
      repr(status.get("workspace")))

out = client.artifacts(sid)
names = [f["path"] for f in out["files"]]
print("files reported    :", names)
check("artifacts lists work done under a non-ASCII path", "app.py" in names)

on_disk = sorted(os.listdir(WS))
print("on disk           :", on_disk)
check("the edit landed on disk",
      open(os.path.join(WS, "app.py"), encoding="utf-8").read().strip().endswith("a + b"))

# The report file, whatever the session actually named it, read back through the
# daemon's workspace endpoint -- where the name is percent-encoded and has to
# survive a round trip through the URL and back onto a Windows filesystem.
report = next((n for n in names if n.lower().endswith(".md")), None)
check("the session wrote a report file", report is not None, str(names))
if report:
    disk_bytes = open(os.path.join(WS, report), "rb").read()
    fetched = client.artifacts(sid, path=report)["content"]
    check("reading it back matches the bytes on disk (name %r)" % report,
          fetched.encode("utf-8") == disk_bytes or fetched.strip() == disk_bytes.decode("utf-8").strip(),
          "fetched %r vs disk %r" % (fetched[:40], disk_bytes[:40]))

print()
if failures:
    print("FAILURES: " + "; ".join(failures))
    raise SystemExit(1)
print("PATH PROBE PASSED")
