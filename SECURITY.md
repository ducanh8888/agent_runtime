# Security

## What AgentRT is, and what it is not

AgentRT runs coding agents against directories on your own machine, and exposes
them over a local HTTP API, a CLI, and an MCP server. The daemon binds to
`127.0.0.1` on an ephemeral port and authenticates its clients with a token
written to `daemon.json` in the state directory.

**Permission presets are not an OS sandbox.** They constrain an agent's
ordinary behaviour, which is useful and is not the same thing as containment.
With a terminal present — the `workspace` and `broad` presets — an agent can
open any file your user can, and path confinement does not change that.
[Permissions and what they actually enforce](docs/reference/security-permissions.md)
states precisely what each preset stops, and the cases where it is
intentionally weaker than its name suggests, including the ones that cannot be
fixed by a better guard.

Do not run AgentRT against a hostile workload, or with credentials whose
compromise you would not accept, on the strength of a preset name.

## What is protected

- **The runtime's own credentials.** `readonly` and `inspect` sessions are kept
  away from the provider credential in the state directory, and the path guard
  compares device and inode against the set of runtime-owned files that may
  hold credentials or private agent state — so a hard link inside the workspace
  does not read around the guard. Known limits: a symlink retargeted between
  the check and the open is a race this cannot win, and on a filesystem that
  reports no inode (FAT, some network shares) the identity check is off rather
  than weakened.
- **Secrets are not printed.** `agentrt config` reports provenance per setting
  with values redacted. Transcripts exclude the system prompt, and exclude the
  model's private deliberation unless you ask for it.
- **Sessions are isolated from each other** by the event log and the state
  directory layout, not by process isolation — they are runs inside one daemon.

## Reporting a vulnerability

Use the repository's **Security** tab and open a private advisory
("Report a vulnerability"). That keeps the report private until there is a fix,
and does not require anyone to publish a contact address.

For anything that is not sensitive — a documentation error, a defence that
could be stricter, a preset that reads as stronger than it is — a normal issue
is fine and preferable.

Please include what you ran, what you expected, and what happened. If the
report is about the guard, a reproduction against a real session is worth far
more than a description: several of the limitations documented in
[the permissions reference](docs/reference/security-permissions.md) were found
that way.

## Supported versions

Pre-1.0 (`agentrt-runtime` 0.1.0). There are no release branches and no
backports: fixes land on `main`. Security issues in the vendored upstream
packages should also be reported upstream to
[OpenHands](https://github.com/OpenHands/software-agent-sdk), which is where
that code originates.
