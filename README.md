# agentrt

A local agent runtime driven through MCP: background agent sessions that
survive the orchestrator exiting. Dispatch a task, close your terminal, come
back later and collect the result.

A hard fork of [OpenHands' software-agent-sdk](https://github.com/OpenHands/software-agent-sdk)
(`f47083cc`) with `openhands.*` renamed to `agentrt.*`, plus `agentrt.runtime.*`
— the daemon, CLI, and MCP surface — which is the only code that is ours. See
[LICENSE](LICENSE) (MIT, OpenHands contributors) and [packages/](packages/)
for the vendored tree.

## Install

    uv tool install packages/agentrt-runtime

Gives `agentrt` (CLI) and `agentrt-mcp` (MCP server, stdio). Needs
[uv](https://docs.astral.sh/uv/) and a provider credential — copy
[.env.example](.env.example) to `<state-dir>/.env` (`agentrt config` prints
the exact path) and fill it in.

## Where to go next

**[CLAUDE.md](CLAUDE.md)** is the real entry point — written for an agent
session picking up this project, but the right first read for a human too: how
the pieces fit together, how to run the checks, and the working conventions
this was built with. `docs/` holds the design record and defect log behind
that summary.
