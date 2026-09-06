"""Dispatch into a workspace shaped like a real repository, then check `artifacts`.

Every workspace used while building this was an empty scratch directory, and
that is why `artifacts` could return two hundred dependency files and look
correct. This probe exists so that cannot happen again: the workspace is seeded
with a `.venv` and pre-existing sources *before* dispatch, and the check is that
none of them are reported as the session's work.

Run against the daemon on the current code. Costs one short session.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

from agentrt.runtime.client import Client

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WS = os.path.join(tempfile.gettempdir(), "agentrt-checks", "artifacts_repo")
shutil.rmtree(WS, ignore_errors=True)
os.makedirs(os.path.join(WS, ".venv", "Lib", "site-packages"))
os.makedirs(os.path.join(WS, "src"))

# A dependency tree, and sources the session did not write. `.` sorts before
# every letter, so under the old path ordering these filled every slot.
for i in range(400):
    with open(os.path.join(WS, ".venv", "Lib", "site-packages", f"dep{i:03d}.py"), "w") as fh:
        fh.write("# dependency\n")
for name in ("README.md", "setup.cfg"):
    with open(os.path.join(WS, name), "w", encoding="utf-8") as fh:
        fh.write("pre-existing\n")
with open(os.path.join(WS, "src", "app.py"), "w", encoding="utf-8") as fh:
    fh.write("def add(a, b):\n    return a - b\n")

client = Client()
failures: list[str] = []


def check(label: str, passed: bool, detail: str = "") -> None:
    print("  %-52s %s%s" % (label, "PASS" if passed else "FAIL", (" -- " + detail) if detail else ""))
    if not passed:
        failures.append(label)


print("workspace seeded: 400 .venv files + 3 sources, all older than the session\n")

session = client.dispatch(
    workspace=WS,
    task=(
        "src/app.py has a function add(a, b) that subtracts instead of adding. "
        "Fix it, then write FIXED.md in the repository root saying in one line "
        "what you changed. Do not create any other file."
    ),
    permission="workspace",
    title="probe: artifacts against a repo-shaped workspace",
)
sid = session["id"]
print("dispatched", session["short_id"])

deadline = time.time() + 300
while time.time() < deadline:
    if client.status(sid).get("status") in ("finished", "error"):
        break
    time.sleep(3)

out = client.artifacts(sid)
names = [f["path"] for f in out["files"]]
print("\nsince             :", out["since"])
print("total_in_workspace:", out["total_in_workspace"])
print("files reported    :", names[:10], "..." if len(names) > 10 else "")

check("no dependency file is reported as the session's work",
      not any(n.startswith(".venv/") for n in names))
check("the untouched pre-existing files are not reported",
      "README.md" not in names and "setup.cfg" not in names)
check("the file the session wrote is reported", "FIXED.md" in names,
      "the session may simply not have written it")
check("the file the session edited is reported", "src/app.py" in names)
check("the result is small enough to read", len(names) <= 10, "%d files" % len(names))
check("the .venv was pruned from the walk entirely", out["total_in_workspace"] < 20,
      "walked %d" % out["total_in_workspace"])

if "FIXED.md" in names:
    body = client.artifacts(sid, path="FIXED.md")["content"]
    check("reading one file back by path works", bool(body.strip()), body.strip()[:60])

print()
if failures:
    print("FAILURES: " + "; ".join(failures))
    raise SystemExit(1)
print("ARTIFACTS PROBE PASSED")
