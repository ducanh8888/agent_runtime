# Agent Runtime — Research Pass 1 Report

Date: 2026-09-05

Scope: read-only inspection of the seven local repositories under `repos`, supplemented by current public documentation where local source was unavailable. At the end of research, `agent_runtime` remained empty and every inspected repository remained clean at the commits listed in the evidence index. This report was subsequently saved at the user's request; no production code or upstream repository was changed.

This is a research report, not a final architecture or implementation plan.

## 1. Executive findings

1. **OpenHands Software Agent SDK plus Agent Server is the closest existing match to the required product.** It already combines a full tool-capable agent loop with persistent conversations, detached background execution, status discovery, event history, steering, interruption, cancellation, reconnection, per-session workspace selection, and final-response retrieval.

2. **No inspected component provides the complete required permission model.** OpenHands offers the strongest integrated tool selection and confirmation policy, while Pydantic AI Harness provides stronger filesystem confinement. Neither constitutes generic enforcement over filesystem, shell, network, Git, MCP, and API authority.

3. **Preventing recursive dispatch requires an enforcement boundary outside the prompt.** Removing delegation tools prevents ordinary model-mediated spawning, but a broadly privileged worker could call the session server through shell or HTTP if it possesses the server address and dispatcher credential. OpenHands Agent Server currently exposes no separate dispatcher role in the inspected authentication layer.

4. **Pydantic AI has the strongest provider abstraction among the inspected libraries.** Its native OpenAI and Anthropic clients handle streaming, tool calls, structured output, reasoning fields, usage, custom base URLs, and explicit model profiles. OpenHands' LiteLLM layer offers broader provider coverage and is already integrated into the strongest session runtime.

5. **9Router does not constrain Pass 1 architecture.** Per the user's direction, it is treated as an external OpenAI/Anthropic-compatible endpoint on latest `main`/origin. The remaining question is whether the selected client path needs profile or protocol adjustments; router defects can be fixed during integration.

6. **The 50–100-session target is plausible but unproven.** OpenHands uses independent asynchronous tasks and per-conversation locks, but its checked-in stress budget covers 16 concurrent conversations. Its synchronous fallback defaults to ten workers. No inspected test establishes 50–100 simultaneously active full agents.

7. **Claude-compatible skills can be reused, but metadata semantics differ.** OpenHands and Deep Agents preserve considerably more of an Agent Skills directory than Pydantic AI Harness. None should be trusted to turn a skill's `allowed-tools` declaration into a security boundary.

8. **Durability has two distinct meanings upstream.** OpenHands persists session state and events and supports reconnect, but does not transparently continue an in-flight tool action after server death. Pydantic AI's DBOS/Temporal/Prefect integrations offer stronger execution checkpointing at the cost of adding another lifecycle owner.

9. **Scheduling already exists, but should remain optional.** OpenHands Automation provides cron, event dispatch, persistent history, and multi-worker claim logic. It also brings automation-specific workspace and sandbox ownership, so its suitability as a thin authorized dispatcher requires focused Pass 2 validation.

10. **Claude Code is the right behavioral reference, not a reusable dependency.** Its current background-session behavior closely matches the desired lifecycle, while the checked-in repository does not expose an OSS runtime suitable for reuse.

## 2. Requirement-to-OSS capability matrix

| Requirement | Candidate | Evidence | Current capability | Confidence | Reuse classification |
|---|---|---|---|---|---|
| Full autonomous agent loop | OpenHands SDK | Agent loop, tool registry, MCP and parallel executor | Complete coding-agent loop with shell, files, browser, MCP and custom tools | High | `REUSE_DIRECT` |
| Alternative full agent loop | Pydantic AI Harness | Coder composition and capability modules | Filesystem, shell, planning, repo context, subagents and compaction | High | `REUSE_WITH_CONFIGURATION` |
| Alternative full agent loop | Deep Agents / dcode | `create_deep_agent`, middleware and backends | Full LangGraph-based coding agent with shell, files, skills and memory | High | `REUSE_WITH_CONFIGURATION` |
| Background start/detach | OpenHands Agent Server | Conversation `run` endpoint and `asyncio.create_task` execution | Starts work and returns before completion; survives client disconnection | High | `REUSE_DIRECT` |
| Discover/list sessions | OpenHands Agent Server | Conversation search endpoint with status, sort and pagination | Cross-client machine-readable discovery, including persisted sessions | High | `REUSE_DIRECT` |
| Inspect status/error | OpenHands Agent Server | Conversation model and execution status | Running, paused, finished and error state exposed | High | `REUSE_DIRECT` |
| Transcript/event history | OpenHands Agent Server | Event search/count/get/batch endpoints | Persisted, cursor-addressable event stream | High | `REUSE_DIRECT` |
| Non-interrupting update | OpenHands Agent Server | `send_message(run=False)` | Appends instruction without cancelling the current execution | Medium | `REUSE_WITH_CONFIGURATION` |
| Explicit redirect/interrupt | OpenHands Agent Server | Interrupt endpoint and cancellation of active `arun` | Cancels current execution and leaves a resumable paused session | High | `REUSE_DIRECT` |
| Stop/cancel | OpenHands Agent Server | Interrupt, pause and delete operations | Cancellation exists; terminal stop versus resumable pause is not cleanly separated | Medium | `ADAPT_THINLY` |
| Resume | OpenHands Agent Server | Persisted state plus subsequent `run` | Paused/error sessions can be started again; active work is not automatically resumed after daemon death | High | `REUSE_WITH_CONFIGURATION` |
| Final result | OpenHands Agent Server | `agent_final_response` endpoint | Dedicated final-answer retrieval | High | `REUSE_DIRECT` |
| Artifact retrieval | OpenHands workspace/events | Workspace APIs and event data | Artifacts remain in the workspace; no complete dedicated artifact-index contract was confirmed | Medium | `ADAPT_THINLY` |
| Cross-process control | OpenHands Agent Server | REST, WebSocket, remote conversation client | Independent clients can reconnect and operate on persisted conversations | High | `REUSE_DIRECT` |
| Per-session model | OpenHands conversation configuration | Agent/LLM configuration stored with conversation | Different conversations can use different models and providers | High | `REUSE_WITH_CONFIGURATION` |
| OpenAI/Anthropic-compatible clients | Pydantic AI | Native provider modules and model profiles | Strong protocol coverage with custom base URL and explicit feature profiles | High | `REUSE_WITH_CONFIGURATION` |
| Broad provider coverage | OpenHands LiteLLM integration | LLM configuration and capability overrides | Large provider set and compatible endpoints within the existing runtime | High | `REUSE_WITH_CONFIGURATION` |
| Per-session tool scope | OpenHands agent configuration | Explicit tool configs, MCP config and filtering | Caller can choose custom/MCP tools; default tools must also be explicitly controlled | High | `REUSE_WITH_CONFIGURATION` |
| Generic permission profile | All inspected candidates | Confirmation policies, guardrails and shell-policy warnings | No unified enforceable profile covering shell, paths, network, Git, APIs and MCP | High | `GAP_REMAINS` |
| Broad unrestricted authority | OpenHands / other local backends | `NeverConfirm` and unrestricted local workspace/tool backends | Can deliberately grant broad authority without routine human confirmation | High | `REUSE_WITH_CONFIGURATION` |
| Authorized dispatch only | OpenHands Agent Server | Shared API-key dependency across lifecycle routes | Authentication exists, but no distinct create-session capability was found | High | `GAP_REMAINS` |
| Disable recursive model delegation | OpenHands / Deep Agents / Pydantic Harness | Configurable delegation/subagent tools | Delegation tool can be omitted or filtered without removing unrelated tools | High | `REUSE_WITH_CONFIGURATION` |
| Prevent shell/API bypass of dispatch boundary | All candidates | Worker shell/network plus shared server authentication | Requires credential or transport isolation absent from inspected components | High | `GAP_REMAINS` |
| Claude-style `SKILL.md` | OpenHands skills | Loader tests and skill directory handling | Preserves skill files and associated resources; rich Claude-only fields differ | High | `REUSE_WITH_CONFIGURATION` |
| Share one skill directory | OpenHands / Deep Agents | Agent Skills-compatible discovery | Common instruction skills can be shared; behavioral metadata needs compatibility rules | Medium | `ADAPT_THINLY` |
| Shared or isolated workspaces | OpenHands workspace service | Per-conversation workspace and optional worktree | Supports direct shared workspace and optional worktree isolation | High | `REUSE_DIRECT` |
| Optional worktrees | OpenHands Agent Server | Worktree request handling | Created only when requested; not a universal requirement | High | `REUSE_DIRECT` |
| 50–100 background sessions | OpenHands Agent Server | Async tasks, per-session locks, stress suite capped at 16 | Architecture is promising; target scale is not demonstrated | Medium | `GAP_REMAINS` |
| Durable state/events | OpenHands SDK/server | File-backed event log, base state, catalog and leases | Useful local persistence and reconnect with cross-process write protection | High | `REUSE_DIRECT` |
| Transparent mid-action crash continuation | OpenHands | Startup recovery marks interrupted running state as error | Conversation survives, but an in-flight action does not transparently continue | High | `GAP_REMAINS` |
| Stronger execution checkpointing | Pydantic AI durable integrations | DBOS, Temporal and Prefect adapters | Durable model/tool steps available, with significant runtime ownership implications | High | `REUSE_WITH_CONFIGURATION` |
| Machine-facing lifecycle CLI | OpenHands / dcode / Claude Code | REST client, dcode thread CLI, proprietary Claude lifecycle CLI | No reusable OSS CLI covers the entire required lifecycle | High | `ADAPT_THINLY` |
| Cron/event scheduling | OpenHands Automation | Scheduler, dispatcher, DB run history | Maintained automation subsystem with cron and concurrent claims | High | `ADAPT_THINLY` |
| Lightweight scheduling alternative | Deep Agents Talon | Persistent cron scheduler | Experimental and executes due jobs sequentially | High | `PATTERN_ONLY` |
| Claude lifecycle compatibility target | Claude Code | Current background-session CLI and agent view | Strong behavioral target; runtime itself is unavailable for OSS reuse | High | `REJECT` |

`GAP_REMAINS` for 50–100 sessions means the evidence gap is a required scale validation, not proof that OpenHands cannot reach that scale.

## 3. Candidate component analysis

### OpenHands Software Agent SDK and Agent Server

This is the strongest overall candidate for agent execution and lifecycle.

The SDK provides a complete iterative agent, explicit tools, MCP integration, browser use, terminal and file operations, metrics, conversation state, and parallel tool execution. The server adds persistent conversations, asynchronous background runs, REST and WebSocket control, event history, reconnection, pause/interrupt, final-response retrieval, and optional worktrees.

Its state ownership is coherent: the conversation service owns lifecycle metadata, the event service owns execution and event publication, the SDK conversation owns agent state, and workspace objects own filesystem execution. Per-conversation locks avoid a global execution lock.

Material limitations:

- The default synchronous fallback executor has ten workers. Native asynchronous agent execution avoids that pool, but tools or alternative agents may still use it.
- The stress suite's concurrent-conversation budget is 16, not 50–100.
- Startup catalog loading and broad searches materialize conversation metadata and may become noticeable with many retained sessions.
- A server restart preserves history but converts a formerly running session to an error state.
- The same API-key mechanism appears to authorize both observation and session creation.
- Confirmation policy and tool selection are useful controls but are not a general sandbox.
- Final responses have a dedicated endpoint; artifact enumeration needs a firmer contract.

This is the only inspected OSS component that is already close to the product's external session semantics.

### Pydantic AI and Pydantic AI Harness

Pydantic AI has the strongest inspected model/provider layer. It provides native OpenAI and Anthropic implementations, explicit model profiles, custom base URLs, streaming, structured output, tool calling, reasoning/thinking handling, usage accounting, retries at transport boundaries, and asynchronous cancellation.

Its active-run API has useful steering primitives: messages can be enqueued during execution, and a run can be cancelled while preserving history for a subsequent run. These are in-process primitives rather than a persistent session service.

Pydantic AI Harness supplies a capable coding-agent composition. Its filesystem layer provides meaningful root and symlink containment. Its shell command allowlist explicitly states that it is an accident guardrail rather than a security boundary because allowed interpreters and tools can spawn arbitrary processes.

Its skill loader intentionally ignores many behavioral frontmatter fields, including `allowed-tools`, model, hooks, context, and tool declarations. It can share simple instruction-oriented `SKILL.md` files, but does not offer the closest Claude compatibility.

Step Persistence is valuable as an append-only record of settled boundaries and ambiguous tool effects. Its own documentation distinguishes that from safely resuming the whole agent graph. The DBOS, Temporal and Prefect integrations provide stronger durable execution, but would introduce a second owner for execution lifecycle and cancellation.

### Deep Agents and dcode

Deep Agents provides a strong full-agent loop built on LangGraph, with filesystem and shell backends, summarization, memory, skills, synchronous subagents and optional asynchronous subagents. `dcode` adds SQLite-backed threads and JSON thread listing.

Its `AsyncSubAgentMiddleware` is a useful client-side lifecycle reference: it can start, list, inspect, update with an interrupt strategy, and cancel remote tasks through Agent Protocol. The local demonstration server is explicitly minimal; its update and cancellation paths update database state without reliably cancelling the underlying fire-and-forget task.

The production LangGraph server path introduces Redis, PostgreSQL and licensing/deployment considerations. That is disproportionate for a single-operator local runtime unless Pass 2 finds a lighter supported deployment path. See the [official standalone-server documentation](https://docs.langchain.com/langsmith/deploy-standalone-server).

Deep Agents' filesystem permissions are tool-level rules. The inspected implementation does not support those execution permission rules when a shell execution backend is enabled. Its tool-exclusion middleware also explicitly says it is not a security surface.

Its skill compatibility is good: it parses standard Agent Skills metadata and leaves scripts/references/resources accessible in the skill directory. Enforcement of `allowed-tools` was not found.

### Microsoft Agent Framework

Agent Framework now contains a substantial harness: model invocation, tool use, shell tools, skills, message injection, sessions and background agents.

The background-agent provider is unsuitable as the persistent lifecycle owner. Its runtime handles are in-memory and explicitly cannot survive process restarts; restored tasks become `LOST`. It also does not provide the required cross-process session-control surface.

File session storage persists snapshots atomically, but uses process-local coordination and last-writer-wins behavior across processes. Message injection and background task controls are useful implementation patterns.

Its skill implementation retains richer resources and scripts than Pydantic AI Harness. Its shell policy explicitly warns that regex policy is not a security boundary.

### OpenHands Automation

OpenHands Automation is the strongest scheduling candidate. It provides cron parsing, event-triggered dispatch, persistent run records, callbacks, retry/history state, and PostgreSQL `SKIP LOCKED` claiming for multiple scheduler workers. SQLite operation assumes one scheduler process.

It is not a neutral session manager. Its automation flow owns run workspaces, sandbox lifecycle and packaged execution behavior. Local mode can target a persistent Agent Server, which may permit reuse as an authorized dispatcher, but that boundary needs validation before taking on the subsystem.

### Claude Code

The local repository does not expose the core runtime implementation under a reusable OSS license. Source-level reuse is therefore rejected.

Current Claude Code behavior is still the best compatibility reference:

- `--bg` starts a detached full session.
- `claude agents --json` provides machine-readable discovery.
- Sessions continue after terminal detachment.
- Persisted sessions can be revisited and restarted by replying.
- Per-session model, permission, plugin/MCP and optional worktree settings are supported.
- Current subagents can themselves nest by default; the product requirement is stricter and must remove dispatch capability entirely.

The appropriate target is Claude's background full-session behavior, rather than treating child subagents as the product's top-level object. See the current [CLI reference](https://code.claude.com/docs/en/cli-reference), [agent view documentation](https://code.claude.com/docs/en/agent-view), and [subagent documentation](https://code.claude.com/docs/en/sub-agents).

## 4. Compatibility risks

### Claude Code integration

A machine-facing CLI can sit over OpenHands' existing REST lifecycle, but no inspected OSS CLI already covers the entire contract. Stable IDs, JSON output, cursor-based event retrieval and well-defined exit/error behavior will matter more than interactive terminal presentation.

Claude's own command names should not be copied blindly. Its state semantics—detached continuation, rediscovery, status, reattachment, redirection, stopping and replying to an exited session—are the valuable compatibility target.

### 9Router and compatible endpoints

9Router is treated as a provider-side compatibility endpoint, as directed. No local 9Router source was available, and no end-to-end test was performed.

Client-side risks remain:

- Unknown model IDs may receive conservative or incorrect capability defaults.
- Anthropic thinking blocks and signatures may not survive an OpenAI translation path.
- OpenAI Responses API fields may differ from Chat Completions compatibility.
- Parallel-tool-call hints, strict schemas, prompt caching and reasoning fields can be endpoint-specific.
- Streaming usage and cancellation behavior require a live request to verify.

Pydantic AI addresses unknown models through explicit profiles. OpenHands/LiteLLM exposes capability overrides and broad routing. Either path can accommodate later 9Router fixes without making the router a Pass 1 selection criterion.

### Permissions

This is the largest functional risk. Tool visibility, approval prompts and shell command regexes do not enforce all effects available to a process.

A worker with unrestricted shell access can potentially:

- access paths outside the intended workspace;
- invoke Git directly;
- open arbitrary network connections;
- call MCP or local APIs using available credentials;
- call the dispatcher unless its credential or endpoint is withheld.

The eventual permission profile must have enforceable owners at the relevant boundaries. Skills must never widen the externally assigned authority.

### Persistence

OpenHands provides practical local durability, but “resume” means reconstructing conversation state and starting another execution. It does not mean transparently continuing an arbitrary subprocess, browser operation or partially completed shell effect after a daemon crash.

Adding DBOS, Temporal, Prefect or LangGraph persistence may improve checkpointing while introducing competing ownership of retries, cancellation, events and run identity.

### Concurrency

No checked-in evidence validates the target load. Likely pressure points are:

- the ten-worker synchronous fallback;
- provider connection pools and rate limits;
- terminal, browser and MCP resources;
- per-session file event logs;
- SQLite writers in candidate checkpoint stores;
- sequential catalog reconstruction;
- shared-workspace contention;
- memory retained by 50–100 conversation histories.

OpenHands' design does not impose obvious global serialization during normal execution, but a targeted soak test is still required.

### Skills

A common skill directory is feasible for the portable subset: frontmatter name/description plus Markdown instructions and colocated files.

Compatibility risks include:

- ignored Claude-only behavioral fields;
- different discovery precedence;
- different script execution semantics;
- differing interpolation or context rules;
- `allowed-tools` being descriptive rather than enforced;
- skill files assuming Claude-specific built-in tool names.

## 5. Confirmed reusable capabilities

The following should almost certainly not be implemented from scratch:

- Full coding-agent loop and standard tools.
- Model request streaming and tool-result round trips.
- OpenAI and Anthropic protocol clients.
- MCP client/tool integration.
- Persistent append-only conversation events.
- Background asynchronous conversation execution.
- REST/WebSocket session discovery and control.
- Per-conversation state, locking and local leases.
- Remote reconnection and event reconciliation.
- Optional worktree creation.
- Skill discovery and `SKILL.md` parsing.
- Filesystem root and symlink checks.
- Model/tool-call usage accounting.
- Cron parsing, automation history and database-backed scheduler claiming.
- Durable workflow adapters if stronger checkpointing is later justified.

## 6. Confirmed gaps

### Enforceable dispatcher-only authority

No inspected component distinguishes an observer/worker credential from the authority to create Agent Sessions. Removing a delegation tool is insufficient when a worker can reach the server through shell or HTTP.

### Unified permission enforcement

No component supplies one caller-provided profile that securely governs arbitrary tools, shell commands, path access, Git, network, MCP and API credentials.

### Complete machine-facing lifecycle CLI

OpenHands supplies most lifecycle operations through APIs, but there is no inspected reusable OSS CLI that covers dispatch, discovery, events, updates, interruption, stop, resume, results and artifacts.

### Transparent recovery of in-flight execution

OpenHands recovers conversation state but marks an interrupted running execution as erroneous. Pydantic Step Persistence records settled boundaries but explicitly does not claim complete graph recovery.

### Verified 50–100-session operation

The required concurrency level has not been demonstrated by source tests or benchmarks. This remains an evidence gap rather than a proven design failure.

### Stable artifact inventory contract

Final-answer retrieval is explicit. A first-class, session-scoped artifact list with metadata and stable retrieval semantics was not confirmed.

## 7. Conflicts between upstream components

| Concern | Potential competing owners | Conflict |
|---|---|---|
| Agent loop | OpenHands, Pydantic AI Harness, Deep Agents, Agent Framework | Combining their loops would duplicate tool invocation, history, retries and context management |
| Session lifecycle | OpenHands Agent Server, LangGraph Server, durable workflow engines | Each has its own run identity, status machine, persistence and cancellation |
| Event history | OpenHands EventLog, LangGraph checkpoints, Pydantic Step Persistence | Multiple event truths can disagree about completion and retry state |
| Tool permissions | OpenHands confirmation policy, Pydantic guardrails, Deep Agents permissions, OS sandbox | Tool-level allow/deny rules overlap but do not enforce the same boundary |
| Workspace | OpenHands Workspace, Deep Agents Backend, Pydantic Filesystem, Automation sandbox | More than one workspace owner would create path translation and cleanup ambiguity |
| Skills | OpenHands, Deep Agents, Pydantic Harness, Agent Framework | All parse similar files but attach different meaning to metadata and resources |
| Scheduling | OpenHands Automation, Talon, external OS scheduler | Each can own dispatch records, retries and execution history |
| Model abstraction | LiteLLM, Pydantic AI providers, LangChain models, Agent Framework clients | Stacking provider abstractions risks lossy streaming, tool and reasoning conversion |

The evidence favors one owner per concern. It does not yet determine which owners should be combined.

## 8. Research Pass 2 questions

1. **Can OpenHands Agent Server pass a realistic 50–100-session soak test?** Measure mostly waiting sessions, concurrent model streams, shell-heavy sessions, event retrieval, memory, restart time and cancellation latency.

2. **What is the smallest enforceable dispatcher boundary?** Test whether separate server credentials, route-level capabilities, local transport ACLs or process isolation can prevent worker-created sessions while retaining broad shell/network authority.

3. **What exact steering behavior does OpenHands provide during a normal active agent turn?** Verify when a non-interrupting message becomes visible, what state survives interruption, and whether redirecting can leave orphaned tools or subprocesses.

4. **Can OpenHands expose artifacts through an existing stable API?** Determine whether workspace APIs and artifact events already cover list, metadata and retrieval without creating a second artifact subsystem.

5. **Which provider path best preserves both OpenAI and Anthropic semantics through compatible endpoints?** Run a small protocol matrix for streaming, tools, parallel tools, reasoning/thinking, schemas, usage, cancellation, retryable failures and long histories. 9Router-specific bugs remain outside architectural selection.

6. **How much Claude skill compatibility is actually portable?** Test representative shared skills containing scripts, references, resources and frontmatter, and document which fields remain descriptive.

7. **Can OpenHands Automation dispatch into the chosen session API without owning workspace or agent lifecycle?** If it cannot act as a thin external dispatcher, defer scheduling from v1.

## 9. Evidence index

Source links below are relative to this report and point into the unchanged local reference clones. Line locations refer to the inspected revisions.

### OpenHands Software Agent SDK

Repository: [OpenHands/software-agent-sdk](https://github.com/OpenHands/software-agent-sdk)

Inspected ref: `f47083cc370a85160f0348f32e531ee3514399e5`.

- Lifecycle REST routes: [conversation_router.py](../repos/software-agent-sdk/openhands-agent-server/openhands/agent_server/conversation_router.py), line 89 onward.
- Event search and message submission: [event_router.py](../repos/software-agent-sdk/openhands-agent-server/openhands/agent_server/event_router.py), line 65 onward.
- Background execution, steering, interruption and restart recovery: [event_service.py](../repos/software-agent-sdk/openhands-agent-server/openhands/agent_server/event_service.py), line 729 onward.
- Conversation locks, execution pool and worktree handling: [conversation_service.py](../repos/software-agent-sdk/openhands-agent-server/openhands/agent_server/conversation_service.py), line 689 onward.
- Cross-process lease protection: [conversation_lease.py](../repos/software-agent-sdk/openhands-agent-server/openhands/agent_server/conversation_lease.py).
- Reconnect and event reconciliation: [remote_conversation.py](../repos/software-agent-sdk/openhands-sdk/openhands/sdk/conversation/impl/remote_conversation.py).
- Parallel tools and resource locks: [parallel_executor.py](../repos/software-agent-sdk/openhands-sdk/openhands/sdk/agent/parallel_executor.py).
- Confirmation policies: [confirmation_policy.py](../repos/software-agent-sdk/openhands-sdk/openhands/sdk/security/confirmation_policy.py).
- Parallel-executor tests: [test_parallel_executor.py](../repos/software-agent-sdk/tests/sdk/agent/test_parallel_executor.py).
- Server lifecycle tests: [test_conversation_service.py](../repos/software-agent-sdk/tests/agent_server/test_conversation_service.py).
- Lease tests: [test_conversation_lease.py](../repos/software-agent-sdk/tests/agent_server/test_conversation_lease.py).
- Skill-loading tests: [test_load_project_skills.py](../repos/software-agent-sdk/tests/sdk/skills/test_load_project_skills.py).

### Pydantic AI

Repository: [pydantic/pydantic-ai](https://github.com/pydantic/pydantic-ai)

Inspected ref: `b57cec28acdb836ec98fa29257225eb1b4e3e104`.

- Native OpenAI provider configuration: [openai.py](../repos/pydantic-ai/pydantic_ai_slim/pydantic_ai/providers/openai.py).
- Native Anthropic provider configuration: [anthropic.py](../repos/pydantic-ai/pydantic_ai_slim/pydantic_ai/providers/anthropic.py).
- Active-run enqueue and cancellation: [run.py](../repos/pydantic-ai/pydantic_ai_slim/pydantic_ai/run.py), line 556 onward.

### Pydantic AI Harness

Repository: local clone `repos/pydantic-ai-harness`.

Inspected ref: `41d51a828880c1e33155f2fc77e8e623e21483ef`.

- Step persistence scope and limitations: [README.md](../repos/pydantic-ai-harness/pydantic_ai_harness/step_persistence/README.md).
- Persistence stores and types: [_store.py](../repos/pydantic-ai-harness/pydantic_ai_harness/step_persistence/_store.py).
- Durability tests: [test_durable_step_persistence.py](../repos/pydantic-ai-harness/tests/step_persistence/test_durable_step_persistence.py).
- Skill compatibility tests: [test_skills.py](../repos/pydantic-ai-harness/tests/skills/test_skills.py).

### Deep Agents

Repository: [langchain-ai/deepagents](https://github.com/langchain-ai/deepagents)

Inspected ref: `4e5f9350e4d77b8bf19e472e8414662d3fa59dc0`.

- Async subagent lifecycle client: [async_subagents.py](../repos/deepagents/libs/deepagents/deepagents/middleware/async_subagents.py).
- Async lifecycle tests: [test_async_subagents.py](../repos/deepagents/libs/deepagents/tests/unit_tests/test_async_subagents.py).
- Skill parsing and disclosure: [skills.py](../repos/deepagents/libs/deepagents/deepagents/middleware/skills.py).
- Experimental Talon background agents: [async_subagents.py](../repos/deepagents/libs/talon/deepagents_talon/async_subagents.py).
- Sequential cron scheduler: [scheduler.py](../repos/deepagents/libs/talon/deepagents_talon/cron/scheduler.py).
- Current standalone LangGraph server requirements: [official documentation](https://docs.langchain.com/langsmith/deploy-standalone-server).

### Microsoft Agent Framework

Repository: [microsoft/agent-framework](https://github.com/microsoft/agent-framework)

Inspected ref: `cc8c1fa0a4c718a4a4cdbcee3340f0bf01746006`.

- In-memory background agent lifecycle and lost-task recovery: [_background_agents.py](../repos/agent-framework/python/packages/core/agent_framework/_harness/_background_agents.py).
- Background-agent tests: [test_harness_background_agents.py](../repos/agent-framework/python/packages/core/tests/core/test_harness_background_agents.py).
- Skills and resources: [_skills.py](../repos/agent-framework/python/packages/core/agent_framework/_skills.py).
- Shell policy limitations: [_policy.py](../repos/agent-framework/python/packages/tools/agent_framework_tools/shell/_policy.py).

### OpenHands Automation

Repository: [OpenHands/automation](https://github.com/OpenHands/automation)

Inspected ref: `90423e1729671e4400be68a4df36e9a27fb3516f`.

- Scheduler and database claim logic: [scheduler.py](../repos/automation/openhands/automation/scheduler.py).
- Scheduler tests: [test_scheduler.py](../repos/automation/tests/test_scheduler.py).

### Claude Code

Repository: [anthropics/claude-code](https://github.com/anthropics/claude-code)

Inspected local ref: `d7dbd9a09f59775726ed14bbea8fc9dfdff62f7b`.

The repository supplied behavioral documentation and plugin material, but not a reusable OSS implementation of the core session runtime. Current behavior was cross-checked against the official [CLI](https://code.claude.com/docs/en/cli-reference), [agent view](https://code.claude.com/docs/en/agent-view), and [subagent](https://code.claude.com/docs/en/sub-agents) documentation.
