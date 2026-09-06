"""What does hitting max_iterations actually look like from outside?

Written before the tool description, because the description has to say what an
orchestrator will see and guessing would put three guesses in it: the state the
session lands in, whether a follow-up gets a fresh budget, and whether the agent
is told the limit is coming or simply cut off.
"""
import os, sys, tempfile, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from agentrt.runtime.client import Client

ws = os.path.join(tempfile.gettempdir(), "agentrt-checks", "iterations")
os.makedirs(ws, exist_ok=True)
c = Client()
s = c.dispatch(
    workspace=ws,
    task=("Do these five things one at a time, each in its own step: "
          "create step1.txt containing 1, then step2.txt containing 2, then "
          "step3.txt containing 3, then step4.txt containing 4, then step5.txt "
          "containing 5. Check each file after writing it before moving on."),
    permission="workspace",
    title="probe: max_iterations",
    max_iterations=2,
)
sid = s["id"]
print("dispatched", s["short_id"], "with max_iterations=2")

for _ in range(80):
    st = c.status(sid)
    if (st.get("status") or "").lower() not in ("running", "idle"):
        break
    time.sleep(3)

st = c.status(sid)
print("\n1. STATE AFTER THE LIMIT")
for k in ("status", "error"):
    if k in st:
        print("   %-8s %r" % (k, st[k]))
print("   full keys:", sorted(st))
made = sorted(f for f in os.listdir(ws) if f.startswith("step"))
print("   files written:", made, "(of 5 asked for)")

print("\n2. IS THE AGENT TOLD?")
ev = c.transcript(sid, limit=100)["events"]
print("   events:", len(ev))
for e in ev[-4:]:
    text = (e.get("text") or e.get("output") or e.get("thought") or "")[:110]
    print("   %-11s %s" % (e.get("type"), text.replace("\n", " ")))

print("\n3. DOES A FOLLOW-UP GET A FRESH BUDGET?")
c.send(sid, "Continue: create the remaining step files.")
for _ in range(60):
    if (c.status(sid).get("status") or "").lower() not in ("running", "idle"):
        break
    time.sleep(3)
after = sorted(f for f in os.listdir(ws) if f.startswith("step"))
print("   status after send:", c.status(sid).get("status"))
print("   files now:", after)
print("   -> the limit is", "PER RUN (send got a fresh budget)" if len(after) > len(made) else "LIFETIME (send did nothing more)")
