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

Start with **[CLAUDE.md](CLAUDE.md)** for runtime context and working
conventions, or **[docs/README.md](docs/README.md)** for the categorized
documentation index. Machine consumers should use
[`docs/manifest.json`](docs/manifest.json).
