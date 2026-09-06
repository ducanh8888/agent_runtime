# P3 — full lifecycle

Plan's done criterion: *the full loop — dispatch, detach, list, transcript,
send, interrupt, resume, result, artifacts — works from Claude Code.*

## What shipped

Seven MCP tools instead of the planned twelve. Five flat — `dispatch`, `list`,
`status`, `result`, `transcript` — plus `control(session, action)` carrying
`send`, `interrupt`, `stop`, `resume` and `delete`, and `artifacts`, which is
separate because its return shape is unlike the rest. Twelve flat descriptions
would sit in the context of every session including those that never call
agentrt; the seven cost 5,794 characters.

The CLI gained the same operations as subcommands, so the two front ends stay
one client core.

`profiles` was dropped, not deferred by accident: there is one profile, and a
tool that lists one fixed thing is a description the orchestrator pays for on
every session for nothing. It returns when P4 adds permission presets.

## Evidence

The whole lifecycle driven through the CLI against a live session
(`scratchpad/cli_loop.py`), each status observed rather than assumed:

```
status                       -> running
transcript                   -> 2 events, cursor=None
interrupt                    -> paused
send                         -> running
stop                         -> finished
resume                       -> finished
result                       -> 'OK'
artifacts                    -> 2 files: ['tick.py', 'ticks.txt']
artifacts --path             -> 202 chars
delete (no --yes)            -> refused without --yes
artifacts traversal          -> refused
delete --yes                 -> ok
status after delete          -> session no longer resolvable
ALL STEPS PASSED
```

Separately verified:

- **Condensation.** A 21-event session returns 11 events — 1 message, 5 actions,
  5 observations. `reasoning_content` and the system prompt appear nowhere in
  the output.
- **Pagination.** The 95-event runaway session pages 64 then 31, and the cursor
  terminates cleanly at None.
- **Traversal.** `..\..\daemon.json`, `C:\Windows\win.ini` and
  `../../../etc/passwd` are all refused before any request is made.
- **Daemon restart.** Killing the daemon mid-client-life brings it back on a new
  port and returns the same session list.

## Not verified, and it is the stated criterion

**"Works from Claude Code" has not been shown.** The MCP server process is a
child of Claude Code, started when the session started, and holds the pre-P3
module: three tools, no `list`, `transcript`, `control` or `artifacts`, and no
reconnect fix. It cannot pick up new code without Claude Code restarting or
reconnecting it.

Everything above ran through the CLI and the client core, which is the same code
the MCP tools call — but the MCP layer itself, and whether `instructions=`
reaches the client at all, remain unchecked until that restart.

## What this phase cost

Estimated ~$0.26 total, all model spend, against a $2 cap. 90% of prompt tokens
were cache hits.

## Findings worth keeping

Recorded in `DAEMON_BEHAVIOUR.md`; the ones that changed the design:

1. `goal/stop` and `goal/resume` are not the stop and resume verbs for a
   conversation. They belong to a separate objective subsystem and answer 400
   on an ordinary session. The real verbs are `pause` and `run`.
2. `send` does nothing without `run: true` — HTTP 200, message stored, agent
   idle.
3. `interrupt` and `pause` both end at `paused` and are otherwise unalike:
   interrupt returns in 2s and kills the running command; pause blocks 28s and
   lets it finish.
4. `events/search?limit=101` returns 500, not 422, because the schema's cap is
   expressed in a key FastAPI does not validate.
5. There is no directory-listing endpoint, so `artifacts` lists locally.
6. `metrics` is null; usage is under `stats.usage_to_metrics`, and cost must be
   computed by the caller because the router publishes no prices.

Each of those is something a reasonable person infers wrongly from the endpoint
name, and each was found by probing before writing rather than by debugging
after.
