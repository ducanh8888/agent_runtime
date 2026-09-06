"""Drive the whole P3 lifecycle through the CLI, as an operator would.

The methods were written and read back, but the state-changing verbs were never
called through the front end that ships. This runs the full loop against a real
session and prints the observed status after each step, so the sequence is
evidence rather than an assumption.
"""
import json
import os
import shutil
import tempfile
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
# Session workspaces go to a temp directory, never inside the repository:
# a dispatched session writes into its workspace, and this repository is
# not a scratch area.
SCRATCH = os.path.join(tempfile.gettempdir(), "agentrt-checks")
REPO = r"C:\Users\ADMIN\Desktop\ORCHESTRATOR\agent_runtime"
PY = os.path.join(REPO, "packages", ".venv", "Scripts", "python.exe")
WS = os.path.join(SCRATCH, "cli_loop_ws")

FAILURES = []


def cli(*args, expect_ok=True):
    """Run one CLI subcommand and return its parsed stdout."""
    proc = subprocess.run(
        [PY, "-m", "agentrt.runtime.cli", *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    label = " ".join(args[:2])
    if expect_ok and proc.returncode != 0:
        FAILURES.append("%s exited %d: %s" % (label, proc.returncode, proc.stderr[:200]))
        return None
    if not expect_ok and proc.returncode == 0:
        FAILURES.append("%s unexpectedly succeeded" % label)
        return None
    try:
        return json.loads(proc.stdout)
    except (ValueError, TypeError):
        return proc.stdout


def step(name, *args, **kw):
    out = cli(*args, **kw)
    state = out.get("status") if isinstance(out, dict) else None
    print("  %-28s -> %s" % (name, state if state else "(ok)"))
    return out


shutil.rmtree(WS, ignore_errors=True)
os.makedirs(WS, exist_ok=True)
shutil.copy(os.path.join(HERE, "tick.py"), os.path.join(WS, "tick.py"))

print("dispatch")
started = cli(
    "dispatch",
    "Run this one command and wait for it to finish:\npython tick.py\n"
    "Do not create or edit any file. Do not run anything else.",
    "--workspace",
    WS,
    "--title",
    "cli loop",
)
sid = started["short_id"]
print("  session %s" % sid)

# Wait until it is genuinely working, so interrupt has something to cancel.
for _ in range(60):
    if os.path.exists(os.path.join(WS, "ticks.txt")):
        break
    time.sleep(1)
time.sleep(2)

print("\nlifecycle")
step("status", "status", sid)
tr = cli("transcript", sid, "--limit", "20")
print("  %-28s -> %d events, cursor=%s" % ("transcript", len(tr["events"]), tr["next_cursor"]))
step("interrupt", "interrupt", sid)
time.sleep(2)
step("send", "send", sid, "Ignore the previous command. Reply with the single word OK and stop.")
time.sleep(3)
step("stop", "stop", sid)
time.sleep(2)
step("resume", "resume", sid)
time.sleep(6)
step("status after resume", "status", sid)

print("\nread back")
res = cli("result", sid)
print("  %-28s -> %r" % ("result", (res.get("result") or "")[:70]))
arts = cli("artifacts", sid)
print("  %-28s -> %d files: %s" % ("artifacts", len(arts["files"]),
                                   [f["path"] for f in arts["files"]][:4]))
one = cli("artifacts", sid, "--path", "tick.py")
print("  %-28s -> %d chars" % ("artifacts --path", len(one["content"])))

print("\nguards")
cli("delete", sid, expect_ok=False)
print("  %-28s -> refused without --yes" % "delete (no --yes)")
cli("artifacts", sid, "--path", "../../daemon.json", expect_ok=False)
print("  %-28s -> refused" % "artifacts traversal")

print("\ndelete")
step("delete --yes", "delete", sid, "--yes")
gone = cli("status", sid, expect_ok=False)
print("  %-28s -> session no longer resolvable" % "status after delete")

print("\n%s" % ("FAILURES: " + "; ".join(FAILURES) if FAILURES else "ALL STEPS PASSED"))
