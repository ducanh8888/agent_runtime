"""Fan out several sessions, exit the process, and pick them up from a new one.

This is the runtime's headline claim -- sessions outlive the orchestrator -- and
the thing an orchestrator most wants to do, which is dispatch several agents at
once and collect them later. Neither had been exercised: every check until now
dispatched one session and waited for it inside the same process, which proves
nothing about either.

Two phases, and they must be separate processes for the test to mean anything:

    python tools/probe_parallel.py dispatch   # starts three, writes ids, exits
    python tools/probe_parallel.py collect    # a new process finishes the job

`dispatch` exits while the sessions are still running. If the claim is false,
`collect` finds them dead.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

from agentrt.runtime.client import Client, ClientError

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.join(tempfile.gettempdir(), "agentrt-checks", "parallel")
HANDOFF = os.path.join(ROOT, "sessions.json")

TASKS = [
    ("alpha", "Write a file named alpha.txt whose only content is the word alpha. Nothing else."),
    ("beta", "Write a file named beta.txt whose only content is the word beta. Nothing else."),
    ("gamma", "Write a file named gamma.txt whose only content is the word gamma. Nothing else."),
]

failures: list[str] = []


def check(label: str, passed: bool, detail: str = "") -> None:
    print("  %-52s %s%s" % (label, "PASS" if passed else "FAIL", (" -- " + detail) if detail else ""))
    if not passed:
        failures.append(label)


def phase_dispatch() -> None:
    shutil.rmtree(ROOT, ignore_errors=True)
    client = Client()

    def start(item):
        name, task = item
        ws = os.path.join(ROOT, name)
        os.makedirs(ws, exist_ok=True)
        # Seeded before dispatch so the artifacts check below can fail. With an
        # empty workspace, "alpha.txt is in the list" is satisfied by a listing
        # that filters nothing at all -- including the behaviour the filter was
        # written to replace -- so the check asserted less than its label said.
        with open(os.path.join(ws, "PRE_EXISTING.txt"), "w", encoding="utf-8") as fh:
            fh.write("written before dispatch; must not appear as session work\n")
        return name, client.dispatch(workspace=ws, task=task, permission="workspace",
                                     title="probe parallel: " + name)

    # Dispatched from threads because that is how an orchestrator would do it,
    # and because serial dispatch would not show a client that is unsafe shared.
    started = time.time()
    with ThreadPoolExecutor(max_workers=len(TASKS)) as pool:
        results = list(pool.map(start, TASKS))
    elapsed = time.time() - started

    ids = {name: session["id"] for name, session in results}
    print("dispatched %d sessions in %.1fs: %s" % (
        len(ids), elapsed, ", ".join(s["short_id"] for _, s in results)))

    check("every dispatch returned a distinct session", len(set(ids.values())) == len(TASKS))
    running = [s for s in client.list_sessions(limit=20) if s["id"] in ids.values()]
    check("all three are visible to list immediately", len(running) == len(TASKS), str(len(running)))

    os.makedirs(ROOT, exist_ok=True)
    with open(HANDOFF, "w", encoding="utf-8") as fh:
        json.dump({"ids": ids, "dispatched_at": time.time()}, fh)
    print("\nhanded off to", HANDOFF)
    if failures:
        raise SystemExit(1)


def phase_collect() -> None:
    with open(HANDOFF, encoding="utf-8") as fh:
        handoff = json.load(fh)
    ids = handoff["ids"]
    gap = time.time() - handoff["dispatched_at"]
    print("this is a different process; %.1fs since dispatch\n" % gap)

    client = Client()  # a fresh client that has never seen these sessions
    deadline = time.time() + 300
    while time.time() < deadline:
        states = {n: client.status(i).get("status") for n, i in ids.items()}
        if all(s in ("finished", "error") for s in states.values()):
            break
        time.sleep(3)

    for name, sid in ids.items():
        state = client.status(sid).get("status")
        check("%s survived the exit and finished" % name, state == "finished", str(state))
        expected = name + ".txt"
        written = [f["path"] for f in client.artifacts(sid)["files"]]
        check("%s wrote %s" % (name, expected), expected in written, str(written))
        check("%s: the pre-existing file is not reported as its work" % name,
              "PRE_EXISTING.txt" not in written, str(written))
        on_disk = os.path.join(ROOT, name, expected)
        check("%s content is right on disk" % name,
              os.path.exists(on_disk) and open(on_disk, encoding="utf-8").read().strip().lower().startswith(name))

    # Each session must have stayed in its own workspace.
    for name in ids:
        strays = [f for f in os.listdir(os.path.join(ROOT, name))
                  if f not in (".git", "PRE_EXISTING.txt", name + ".txt")]
        check("%s workspace holds nothing from the others" % name, not strays, str(strays))


def phase_cap() -> None:
    """Check the cap refuses, and which process's environment it reads.

    The first version of this dispatched with the cap set to one and no session
    running, which is exactly the case the cap is meant to allow -- it reported a
    failure against working code. A cap is only observable while something is
    running, so this holds one session open on purpose.
    """
    client = Client()
    ws = os.path.join(ROOT, "capws")
    os.makedirs(ws, exist_ok=True)
    holder = client.dispatch(
        workspace=ws,
        task="Run this exact terminal command and report its output: python -c \"import time; time.sleep(40); print('done')\"",
        permission="workspace",
        title="probe parallel: holds a slot open",
    )
    print("  holding a session open:", holder["short_id"])

    deadline = time.time() + 60
    state = None
    while time.time() < deadline:
        state = (client.status(holder["id"]).get("status") or "").lower()
        if state == "running":
            break
        if state in ("finished", "error"):
            break
        time.sleep(1)
    check("a session reports itself running while it works", state == "running", str(state))

    if state == "running":
        # A subprocess, so the variable is set on the dispatching process only.
        # The daemon has not been restarted and cannot have read it.
        env = dict(os.environ, AGENTRT_MAX_SESSIONS="1")
        code = (
            "from agentrt.runtime.client import Client, ClientError\n"
            "import tempfile, os\n"
            "ws = os.path.join(tempfile.gettempdir(), 'agentrt-checks', 'parallel', 'capws2')\n"
            "try:\n"
            "    Client().dispatch(workspace=ws, task='write nothing at all', permission='readonly')\n"
            "    print('DISPATCHED')\n"
            "except ClientError as exc:\n"
            "    print('REFUSED:', exc)\n"
        )
        out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                             text=True, encoding="utf-8", errors="replace").stdout
        tail = [l for l in out.splitlines() if l.startswith(("REFUSED", "DISPATCHED"))]
        line = tail[-1] if tail else out.strip()[-160:]
        print("  cap=1 in the dispatching process ->", line[:120])
        check("the cap refuses a dispatch while one is running", line.startswith("REFUSED"))
        check("the cap is read where dispatch runs, not on the daemon",
              line.startswith("REFUSED"),
              "the daemon was never restarted, so it cannot have seen this value")
        check("the refusal says how to raise the limit", "AGENTRT_MAX_SESSIONS" in line)

    stopped = client.stop(holder["id"])
    print("  stopped the holder:", stopped.get("status", "ok"))
    # `stop` maps to /pause, and a paused session is kept until deleted -- so a
    # probe that only stopped it left one more behind on every run.
    client.delete(holder["id"])


if __name__ == "__main__":
    phase = sys.argv[1] if len(sys.argv) > 1 else "dispatch"
    if phase == "dispatch":
        phase_dispatch()
    elif phase == "cap":
        phase_cap()
        print()
        if failures:
            print("FAILURES: " + "; ".join(failures))
            raise SystemExit(1)
        print("CAP PHASE PASSED")
    elif phase == "collect":
        phase_collect()
        print("\n=== the session cap ===")
        phase_cap()
        print()
        if failures:
            print("FAILURES: " + "; ".join(failures))
            raise SystemExit(1)
        print("PARALLEL PROBE PASSED")
    else:
        raise SystemExit("unknown phase: " + phase)
