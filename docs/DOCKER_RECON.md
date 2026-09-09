# Docker sandbox: what works, and the one thing that does not

`workspace` confines the file editor and grants a terminal, and a terminal
defeats path confinement. Only a sandbox fixes that, which is why this was
looked at. The groundwork is done and two real bugs were fixed on the way; the
remaining blocker is in a vendored type and is not a wiring job.

## What was established, by running it

- **Docker is available here**: server 29.7.2, `linux/amd64`, reached through a
  `.cmd` shim at `~/.local/bin`. The image `ghcr.io/openhands/agent-server:
  latest-python` pulls.
- **`DockerWorkspace` works.** Validating a spec in-process starts a container,
  reports a host URL, and cleans up. Container startup is not the problem.
- **The mount form matters and only one works.** This docker daemon lives in
  WSL, so a Windows path must be translated: `C:\x` becomes `/mnt/c/x`. Passing
  the Windows path gives `invalid mode: /workspace`, because docker splits the
  volume string on the drive-letter colon. The `//c/` form is worse -- it
  silently mounts an empty directory instead of failing.
- **`artifacts` would keep working.** The workspace enters the container as a
  bind mount, so the host directory is the same directory; walking it on the
  host still sees what the session wrote.

## Two bugs fixed getting this far, both worth having anyway

**`execute_command` could not launch a `.cmd` on Windows.** `CreateProcess`
only appends `.exe`, so `["docker", "version"]` -- `DockerWorkspace`'s first
call -- raised `FileNotFoundError` while `docker` worked in any shell and
`shutil.which` found it. The list form now resolves through `PATH` first. This
affects every `.cmd` shim, so npm and yarn were equally unreachable.

**The daemon never registered the container workspace kinds.** `BaseWorkspace`
is a discriminated union and a member exists only once its module is imported;
the server imports the local and remote kinds only. A request naming
`kind: "DockerWorkspace"` was rejected during validation with an assertion
carrying an empty message, and the daemon log showed a validation error and no
docker output at all -- which was the diagnosis, since nothing had tried to
start a container. `agentrt.runtime.server_launch` now registers them before
handing over to the server, and `DockerWorkspace` appears in the daemon's own
OpenAPI schema.

## The blocker

`POST /api/conversations` cannot express a container workspace. The request
type pins it:

    # agentrt/sdk/conversation/request.py
    workspace: LocalWorkspace = Field(...)

The *response* model uses `BaseWorkspace`, which is what makes this look
supported from the outside. The create path is narrower than the read path.

Widening the field is one line and not the work. The conversation service is
written around a host path:

    source_workspace = Path(workspace.working_dir).resolve()
    LocalWorkspace(working_dir=workspace_dir)
    Path(a.working_dir).resolve() == Path(b.working_dir).resolve()

`_prepare_request_workspace` resolves the directory, may create a git worktree
under it, and hands a freshly constructed `LocalWorkspace` onward. For a
container, `working_dir` is `/workspace` -- a path that does not exist on the
host -- so each of those has to learn the difference between the path the agent
sees and the path the daemon can touch. That includes our own `artifacts`,
which reads `working_dir` from the conversation record and would need the
bind-mount source instead.

So the honest estimate: a vendored change to the conversation service, in the
core of the daemon, not an integration. That is a scope decision rather than a
task, and it is recorded here rather than half-built.

## The experiment, and why it stopped

The estimate above was tested rather than left as an estimate. Widening the one
field to `BaseWorkspace` took a line, and it worked in the sense that mattered:
the 422 disappeared and the daemon went all the way to `docker run` with the
right image, the right mount and the right port mapping. So the type really was
the only thing preventing the request from being expressed.

It stopped one step further on. Every container the daemon launches fails with
`Bind for 0.0.0.0:<port> failed: port is already allocated` -- for a port picked
by the workspace, and equally for an explicit `host_port` verified free
beforehand. The same picker followed by the same `docker run` succeeds three
times out of three from an ordinary process, and publishing ports in that range
by hand works every time. No container or stale endpoint holds them; the only
container on this machine belongs to something else. So it is specific to the
daemon process, and it is not the port range, not the picker, and not the
check-then-use race.

That was the boundary set for the experiment, so it was reverted. What it
bought is a better answer than the estimate: **the architecture is not the
obstacle, the environment is.** A Linux host, where docker is not reached
through a WSL port proxy, would very likely get further on the same code.

Two corrections to what is written above, both found by doing it:

- `_prepare_request_workspace` returns the request untouched unless `worktree`
  is set, which defaults to false. It does not unconditionally rebuild the
  workspace as a `LocalWorkspace`; that path is opt-in. The host-path
  assumptions are real but narrower than stated.
- The claim that our guard cannot run inside the container was never verified.
  `DockerWorkspace` is a `RemoteWorkspace`, which may keep the agent loop local
  and ship only execution into the container -- in which case the guard applies
  as it does now. It is moot while this is blocked, but it was presented as
  established and it was not.

The two fixes made getting here -- `.cmd` resolution on Windows, and
registering the workspace kinds -- are independent of all this and stay.

## The Linux repeat, 2026-09-09: the environment theory was wrong

The Windows write-up above concluded "the architecture is not the obstacle,
the environment is" and named the WSL port proxy as the suspect. This
machine's docker is native -- no WSL, no proxy -- so it was the predicted
clean run. It was not. The port-binding failure reproduces here too, and
tracing it to a cause the Windows session could not have found changes the
conclusion: **this was never the environment.**

Repeating the experiment exactly: `.cmd` resolution and workspace-kind
registration are already in the tree (from Windows) and untouched. Widening
`ConversationConfig.workspace` from `LocalWorkspace` to `BaseWorkspace` is the
same one-line change, made against a daemon started from the source venv (the
installed snapshot doesn't see source edits). The daemon's own OpenAPI schema
confirmed both effects of that line: `workspace` resolves to a `BaseWorkspace`
discriminated union, and `DockerWorkspace-Input` is a member of it.

Dispatching a `kind: DockerWorkspace` conversation directly against
`POST /api/conversations` (bypassing the CLI/MCP client, which still only
knows `LocalWorkspace`) failed every time with the same shape of error as
Windows:

    {"detail": "Internal Server Error", "exception": "Port 32113 is not available"}

Three more attempts, three more failures, three different random ports. That
by itself looks like Windows's ending. It is not what was happening.

**Every failed request had already started a healthy container.** `docker ps`
showed four running `agent-server` containers, one per attempt, each
`Up`, each with a working port mapping, each backed by a real `docker-proxy`
process bound to exactly the port the client was told was unavailable. The
daemon log confirms the order per attempt: `docker run` succeeds, `Started
container`, the health check passes, `Docker workspace is ready` -- and *then*
`RuntimeError: Port <same port> is not available`, twice, before the request
finally fails. The container that made the port genuinely busy by the second
check is the one the first, successful construction started.

**The cause: the workspace is constructed twice per request, and the second
construction is not a copy.** `_create_conversation` calls
`request.model_dump(mode="json", ...)` on the already-live `request.workspace`
(the `DockerWorkspace` instance FastAPI built while parsing the POST body, with
a concrete `host_port` and a running container behind it), then splats that
dict into `StoredConversation(id=..., **request_data)` to build the persisted
record. `StoredConversation.workspace` is typed `BaseWorkspace` -- it already
was, independent of anything changed here -- so Pydantic validates that dict
as a fresh model rather than accepting the live object, and validating a
`DockerWorkspace` dict is not inert: `model_post_init` runs again,
`host_port` is already fixed in the dump so `find_available_tcp_port` is
skipped, and `check_port_available` correctly refuses the port the first
container is still holding. The exception aborts `stored` before it is ever
assigned, so nothing references the first container -- it is not stopped, not
recorded in `agentrt list`, not visible to anything but `docker ps`. Four
attempts, four orphaned containers, cleaned up here by hand
(`docker stop`, which removes them since they run `--rm`).

`LocalWorkspace.model_post_init` only touches a directory, so building one
twice from a dump is a harmless no-op -- which is exactly why this has never
surfaced before now. It is specific to workspace kinds whose construction has
a side effect, and `DockerWorkspace` is the first one anyone tried to create
through this path.

**So the corrected estimate:** the blocker is not the request type (that part
of the original write-up holds -- widening it does make the 422 disappear and
the request reach `docker run`), and it is not this machine's environment
either. It is that `_create_conversation`'s persist step re-validates the
workspace from a dump instead of carrying the already-constructed object
forward, which is safe for every workspace kind currently reachable and wrong
for every one that is not. Fixing it is a real, scoped task -- pass the live
`request.workspace` object into `StoredConversation` directly (or exclude it
from the dump and set it separately) rather than round-tripping it through
JSON -- and it is a vendored change to the persist path, in the same class of
work as the type-widening, not a bigger one. It was not attempted here; the
boundary this time was documenting the true cause rather than shipping a fix
mid-recon.

Reverted after the finding, same as the Windows session: the field-widening
edit was not committed, and the daemon was returned to the installed snapshot.

## What to do instead, for now

Nothing here changes the standing advice. `readonly` genuinely contains a
session, because it has no terminal. `workspace` constrains ordinary behaviour
and not a determined session, and the documentation says so in those words. The
sandbox that would change that is available, reachable, and now has a named,
scoped defect standing between it and working -- not an unexplained
environment difference.
