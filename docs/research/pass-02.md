# Agent Runtime — Research Pass 2

> **Scope correction after cross-repository reassessment:** The blocker verdict below describes the reviewed OpenHands integration, not the absence of reusable OSS implementations. Subsequent source inspection identified existing OpenHands action-blocking hooks and concrete donor implementations for filesystem policy, sandboxed shell execution, bounded executor handling, process termination, skill execution separation, and browser cancellation cleanup. Read [OSS Gap Reassessment](oss-gap-reassessment.md) for the revised base-repository-plus-donors findings. The original findings are retained below for traceability; their product-wide interpretation is superseded by that reassessment.

**Status:** Final pre-implementation research report; architecture is not frozen.  
**Date:** 2026-09-05.  
**Prior evidence:** [Research Pass 1](pass-01.md).
**Scope:** OSS reuse, runtime behavior, authority boundaries, lifecycle semantics, and architecture readiness.  
**Final verdict:** `BLOCKED_BY_CONFIRMED_GAPS`.

This report records the completed Pass 2 findings. It is not an implementation plan, a final stack selection, or a proposal to build replacement subsystems. The user's subsequent request to write down the result authorizes this research document; it does not authorize implementation or changes to reference repositories.

The latest research direction was to stop testing and benchmarking in this environment and focus on architecture and runtime source. Accordingly, the conclusions below distinguish implementation evidence from practical capacity claims. No further tests or benchmarks are required by this report. Earlier limited diagnostic activity does not establish production capacity or end-to-end provider compatibility.

9Router is treated as the user's intended external endpoint, tracking its latest main/origin and providing OpenAI-compatible and Anthropic-compatible APIs. Router-specific integration defects may be resolved later. No specific 9Router defect is claimed here, and live 9Router validation is not an architecture-freeze prerequisite.

## 1. Executive verdict

**Most frozen requirements appear supportable primarily through existing OSS. OpenHands SDK plus Agent Server remains the strongest reviewed candidate for full-agent execution and persistent Agent Session lifecycle. Its current permission and execution boundaries, however, do not satisfy every frozen requirement without additional integration work.**

The major findings are:

1. **The core session runtime already exists.** OpenHands provides independent conversations that map naturally to Agent Sessions: stable identity, per-session agent/model/workspace configuration, server-owned background execution, discovery, events, steering, interruption, continuation, final responses, and persistent state.
2. **Cross-client lifecycle control is supported by the implementation.** The server owns execution; a later client can use the session ID and APIs without retaining the original client object. A server whose lifetime is independent of the invoking CLI is still necessary.
3. **The normal async path is not globally limited to ten sessions.** The ten-worker setting applies to a synchronous fallback executor. Session catalog loading and listing are also lighter than Pass 1 suggested.
4. **Tool scoping is useful but incomplete as a permission boundary.** Ordinary model tool calls are constrained by the resolved tool map. Automatically attached built-ins and execution outside the tool path require separate consideration.
5. **A concrete skill-rendering bypass exists.** Inline commands execute through `subprocess.run(shell=True)` with the rendering process's privileges and inherited environment. They bypass the terminal tool's environment sanitization and do not pass through a workspace execution policy.
6. **Dispatcher authentication exists, but broad worker execution is not automatically separated from it.** Removing a delegation tool prevents one invocation path. It does not prevent shell, filesystem, network, or custom-tool paths from exercising exposed dispatcher authority.
7. **Cancellation is not universal effect termination.** Agent cancellation preserves resumable conversation history, but arbitrary threads, browser activity, and some shell descendants may continue. Parallel-tool cleanup has a source-supported path that can block the shared event loop while waiting for threads.
8. **Provider capability is substantially reusable.** OpenHands/LiteLLM and Pydantic AI have real agent-workload support. Their parameter transformations, model profiles, and native protocol handling differ; placing both on the same request path is not automatically safe or useful.
9. **Artifacts do not currently justify a new subsystem.** Existing file, workspace, observation, event, and result capabilities can support explicit artifact references and retrieval. A shared workspace does not by itself establish file authorship.
10. **Scheduling can remain external and optional.** Existing generic schedulers can invoke a dispatcher without owning Agent Sessions. OpenHands Automation currently owns more of execution and workspace behavior than this product permits a scheduler to own.

The important distinction is between a confirmed gap in the reviewed integration and a claim that no OSS solution exists. This report establishes the former for authority enforcement and cancellation coupling. It does not establish the latter and does not justify a replacement agent runtime.

## 2. Pass 1 uncertainty resolution

### Evidence interpretation

- **Source-established:** directly supported by implementation control flow, data structures, or configuration.
- **Upstream-test-supported:** the repository contains a relevant test. This does not mean that every cited test was executed in this environment.
- **Inference:** a stated consequence derived from inspected implementation and documented dependency behavior.
- **Unverified:** not demonstrated by available evidence; it must not be relabeled as unsupported.

Confidence concerns the stated finding, not the maturity of an entire project. `ADAPT_THINLY` identifies a bounded integration surface where existing behavior can be retained; it is not a final estimate or an implementation commitment.

### Resolution table

| Unresolved Pass 1 item | Evidence | Verified behavior | Remaining limitation | Confidence | Reuse classification |
|---|---|---|---|---|---|---|
| Approximately 50–100 concurrent sessions | OpenHands conversation catalog, per-session EventService, async execution, parallel executor | Independent async tasks; per-session locks; lazy hydration; ten-worker pool applies to sync fallback | Shared executor contention and synchronous cancellation cleanup can affect responsiveness; exact capacity unverified | High on architecture; medium on practical capacity | `ADAPT_THINLY` |
| Enforceable dispatcher boundary | API authentication, terminal sanitizer, skill command renderer | Authentication and terminal key stripping exist | All execution paths do not share a protected authority boundary; skill subprocess inherits process authority | High | `GAP_REMAINS` |
| Actual steering behavior | EventService message handling; LocalConversation processing and cancellation | New input remains in the same history; native non-interrupting input does not cancel the active model request; interrupt preserves continuation state | Input incorporation occurs at boundaries; tool effects may survive cancellation; ACP semantics differ | High | `REUSE_WITH_CONFIGURATION` |
| Artifact discovery and retrieval | Workspace router, file router, observation persistence, final-response API | Known paths and associated observation files are retrievable; session events/results can identify artifacts | No exhaustive generic attribution of arbitrary shared-workspace writes | High | `ADAPT_THINLY` |
| Provider fidelity | OpenHands LLM implementation and locked LiteLLM behavior; Pydantic provider adapters and tests | Agent tool round trips, streaming, reasoning representations, schemas, usage, timeouts and errors have implementations | Parameter dropping, profile gates, native protocol differences; live 9Router behavior unverified | High on client behavior | `REUSE_WITH_CONFIGURATION` |
| Claude skill portability | OpenHands and Pydantic Harness loaders; rendering and invocation code | Standard skill instructions and supporting files can be shared | Claude behavioral metadata is not generally equivalent; executable rendering is not bounded by terminal policy | High | `ADAPT_THINLY` |
| Scheduling without lifecycle ownership | OpenHands Automation dispatcher/backend/watchdog; generic OS scheduler source | Generic scheduler can invoke an external dispatch operation | OpenHands Automation is not a trigger-only drop-in; richer automation history remains optional | High | `REUSE_WITH_CONFIGURATION` for generic scheduling; `REJECT` for Automation as a drop-in trigger-only owner |

### Corrections and refinements to Pass 1

**Session loading:** startup does not eagerly instantiate every full conversation and transcript. It loads lightweight metadata and base status, hydrating live conversation/event services as needed. Idle-session discovery therefore costs less than a full-history scan.

**Execution pool:** `max_concurrent_runs=10` is not a universal cap on the native async agent path. It is the synchronous fallback pool size.

**Credential handling:** terminal environment sanitization already removes server-key variables. The remaining problem is inconsistent coverage across execution paths, not an absence of all credential sanitization.

**Authentication roles:** separate dispatcher and worker API roles are not inherently required. Denying workers all control credentials could satisfy the product, provided their other authority cannot recover or bypass those credentials. Equivalent server API keys are therefore not independently a blocker.

**Scheduling:** coupling in OpenHands Automation does not establish that reusable external scheduling is absent. Generic scheduling primitives provide a narrower option.

### Local evidence baseline

| Repository | Inspected commit/ref | Role in this pass |
|---|---|---|
| `software-agent-sdk` | `f47083cc370a85160f0348f32e531ee3514399e5` | Primary execution, lifecycle, permission, provider, skills and artifact evidence |
| `pydantic-ai` | `b57cec28acdb836ec98fa29257225eb1b4e3e104` | Independent provider and tool-guardrail comparison |
| `pydantic-ai-harness` | `41d51a828880c1e33155f2fc77e8e623e21483ef` | Skill portability, filesystem boundaries and retained Pass 1 persistence evidence |
| `deepagents` | `4e5f9350e4d77b8bf19e472e8414662d3fa59dc0` | Narrow permission/skill comparison and retained Pass 1 lifecycle findings |
| `agent-framework` | `cc8c1fa0a4c718a4a4cdbcee3340f0bf01746006` | Pass 1 background-lifecycle evidence retained; no repeated broad survey |
| `automation` | `90423e1729671e4400be68a4df36e9a27fb3516f` | Scheduler ownership and coupling |
| `claude-code` | `d7dbd9a09f59775726ed14bbea8fc9dfdff62f7b` | Pass 1 behavioral reference; not a reusable OSS core runtime |

Public sandbox-runtime and systemd source inspections supplement the local baseline. Those moving `main` sources were not locally integrated or pinned as project dependencies.

## 3. Dispatcher authority findings

### Required distinction

The authorization subject is an external dispatcher, represented by a protected authority such as a credential or execution identity. The runtime need not prove the brand name of the client process. Claude Code and Codex must be able to exercise the authority; Agent Sessions must not.

| Mechanism | Actual guarantee | Satisfies the frozen requirement alone? |
|---|---|---|
| Prompt instruction prohibiting delegation | Requests model cooperation | No |
| Omitting a dispatch/delegation tool | Removes that model-visible invocation path | No, when shell/API access remains broad |
| Server API authentication | Rejects callers without accepted credentials when configured | Only if workers cannot acquire or bypass that authority |
| Execution and credential isolation | Can prevent indirect shell/filesystem/network paths from exercising dispatcher authority | Relevant enforceable boundary; complete candidate integration remains unresolved |

### OpenHands authentication

`check_session_api_key` checks the request against configured API keys. If the configured list is empty, authentication is not enforced. Accepted keys are equivalent control credentials, rather than separate create/read/worker roles.

The workspace cookie is accepted only by workspace routes, but its underlying value must not be mistaken for a separate least-privilege credential. A raw client possessing the corresponding accepted API-key value could send it in the control header.

Evidence: [dependencies.py](../../repos/software-agent-sdk/openhands-agent-server/openhands/agent_server/dependencies.py), especially line 24 onward.

### Existing terminal protection

The command environment sanitizer removes `SESSION_API_KEY`, `OH_SECRET_KEY`, and variables under the server-key prefix. The terminal environment builder applies sanitization to its execution environment.

This is useful existing behavior that should not be rebuilt. It protects a particular environment-exposure route; it is not process or filesystem isolation and does not automatically remove every unrelated provider credential.

Evidence: [command.py](../../repos/software-agent-sdk/openhands-sdk/openhands/sdk/utils/command.py), line 25 onward; [terminal environment](../../repos/software-agent-sdk/openhands-tools/openhands/tools/terminal/env.py).

### Confirmed uncovered execution path

OpenHands skill rendering can execute inline commands through `subprocess.run(shell=True)`. That call supplies no sanitized environment and does not use the terminal tool or workspace execution abstraction. It therefore inherits the renderer's process environment and privileges.

The renderer's own module documentation states that commands execute with full process privileges and require trusted skill sources. Trusting a skill is not equivalent to enforcing the orchestrator's assigned permission profile: a trusted skill can still request an operation outside a read-only or restricted profile.

The skill invocation tool is also auto-attached after ordinary regex filtering when invocable AgentSkills-format skills exist. An empty ordinary tool selection is therefore not proof that no execution-capable path remains.

Evidence: [agent/base.py](../../repos/software-agent-sdk/openhands-sdk/openhands/sdk/agent/base.py), lines 565–611; [invoke_skill.py](../../repos/software-agent-sdk/openhands-sdk/openhands/sdk/tool/builtins/invoke_skill.py); [skills/execute.py](../../repos/software-agent-sdk/openhands-sdk/openhands/sdk/skills/execute.py), lines 52–73.

### What can and cannot coexist

Broad filesystem, shell, Git, browser and API capabilities can coexist with dispatcher exclusion when they operate within a boundary that excludes dispatcher identity, credentials and control resources.

Literal unrestricted administrative control over the host, including the enforcing service and its credentials, cannot coexist with an unbypassable prohibition on using that service. This is a consequence of the authorities granted, not a missing prompt technique.

The product's intended broad execution remains supportable. Architecture must not silently interpret it as authority over the dispatcher itself or claim that shell-tool removal supplies the required separation.

Docker and OS sandboxes provide existing enforcement primitives. However, selecting OpenHands `DockerWorkspace` alone does not prove the boundary: the Agent Server can itself run in that environment. The location of the enforcing service relative to worker-controlled execution matters.

**Finding:** `GAP_REMAINS` for the complete reviewed integration. Existing authentication and isolation capabilities remain reuse candidates; a new identity or authentication framework is not justified by the evidence.

## 4. Permission findings

### Dimension-to-boundary mapping

| Permission dimension | Existing owner/mechanism | What is enforceable | Limitation or uncovered path |
|---|---|---|---|
| Available tools | OpenHands resolved tool map; selected tool specifications | Calls to tools absent from the actual map cannot execute through normal model-tool dispatch | Default/automatic built-ins are attached after ordinary filtering |
| Filesystem paths | Filesystem-tool checks; OS permissions; sandbox mounts | Scoped operations through the checked tool; process-wide access when enforced by the OS boundary | Shell or arbitrary in-process execution can bypass a filesystem-tool-only policy |
| Write authority | Read-only filesystem mechanisms and mounts | Writes through covered processes and paths | Prompt-level read-only rules do not constrain alternate execution paths |
| Shell authority | Tool availability plus process execution boundary | Whether and where covered commands can execute | Skill rendering can execute commands without TerminalTool |
| Git authority | Local filesystem boundary and remote credential scopes | Local repository access and remote service authority can be constrained at their respective boundaries | A Git command filter is not comprehensive when broad filesystem/network execution remains available |
| Network authority | OS/container restrictions and sandbox proxies | Reachability and supported destination restrictions | Domain access is not equivalent to API-method or business-operation authorization |
| MCP availability | Selected MCP configuration/tools | Which MCP tools the agent can directly invoke | MCP server's own credentials and execution authority require separate scoping |
| API access | Tool exposure and service-side scoped credentials | Service-defined authority and exposed operations | Broad credentials remain broad if workers can use them through shell or custom clients |
| Credentials | Environment sanitization, controlled exposure, external identity boundary | Specific environment exposure routes; externally scoped credentials | Skill subprocess inherits renderer environment; same-identity process authority remains relevant |
| Interactive approval | OpenHands confirmation policies | Whether approval is requested before tool execution | Approval behavior does not express a comprehensive externally supplied capability profile |
| Skills | Skill loading, invocation and rendering | Instruction/resource availability | Executable rendering and tool auto-attachment can expand effective behavior outside the ordinary selection path |
| Custom tools | Tool registration and executor boundary | Whether the tool is callable; any checks explicitly implemented by it | Arbitrary host-language tool code has the authority of its execution context |
| Broad execution | Broad grants within a protected execution boundary | Full useful capability within assigned authority | Must still exclude dispatcher control if recursive creation is forbidden |

### Confirmation versus enforcement

OpenHands provides policies such as always-confirm, never-confirm and risk-based confirmation. They are reusable for approval behavior. In particular, never-confirm supports unattended execution when the orchestrator has already granted authority.

It does not follow that never-confirm plus a prompt describing restrictions enforces a restrictive profile. Optional risk analyzers likewise do not become generic filesystem, network, credential or process sandboxes.

Evidence: [confirmation_policy.py](../../repos/software-agent-sdk/openhands-sdk/openhands/sdk/security/confirmation_policy.py).

### Automatically attached capabilities

The ordinary tool regex filter runs before built-ins are attached. Invocable skills can add `InvokeSkillTool`. A configured vision profile can cause `VisionInspectTool` to be attached for a non-vision model. These behaviors are useful capabilities, but the externally assigned profile cannot treat the pre-built-in filtered list as the final authority set.

The finding is not that built-ins should be removed. It is that their inclusion must remain subordinate to the same externally assigned authority.

### Other reusable OSS enforcement capabilities

Pydantic AI tool guardrails can reject selected invocations based on their arguments or results. They constrain the covered invocation path, not arbitrary shell semantics or unrelated host execution.

Pydantic AI Harness provides filesystem containment behavior. Its shell-related capability boundaries must still be distinguished from OS identity isolation. Deep Agents also has filesystem permission support and checks around execution-capable backends; that is narrower than a complete generic permission profile.

Anthropic's OSS sandbox runtime supplies filesystem/network enforcement and credential-handling mechanisms. Its manager maintains process-global configuration/proxy state, so independent per-session profiles cannot simply be assumed within one shared manager instance. Execution outside its boundary remains outside its enforcement. Source: [sandbox-manager.ts](https://raw.githubusercontent.com/anthropic-experimental/sandbox-runtime/main/src/sandbox/sandbox-manager.ts).

Linux sandbox source includes namespace and filesystem isolation mechanisms. Its existence establishes reusable primitives, not a completed integration with every OpenHands execution path. Source: [linux-sandbox-utils.ts](https://raw.githubusercontent.com/anthropic-experimental/sandbox-runtime/main/src/sandbox/linux-sandbox-utils.ts).

Docker supplies existing process/filesystem isolation and read-only mounts. Shared physical workspace access can remain intentional; isolating authority does not require creating a unique worktree for every session. Sources: [Docker security](https://docs.docker.com/engine/security/), [bind mounts](https://docs.docker.com/engine/storage/bind-mounts/).

### Permission conclusion

**Much of the required enforcement is reusable, but no inspected component alone owns the complete profile.** The confirmed missing coverage is the composition across model tool dispatch, automatically added tools, skill rendering, shell, custom execution and protected control credentials.

This does not justify implementing a new universal policy engine. It prevents declaring the current integration compliant with the frozen profile requirement.

## 5. Session lifecycle findings

### Existing lifecycle surface

| Required operation | Source-established OpenHands behavior | Important condition |
|---|---|---|
| Create/dispatch | Creates a conversation with stable UUID, agent/model configuration, workspace and optional initial message | Product may call this an Agent Session while preserving upstream internal terminology |
| Background start | EventService schedules server-owned execution | Server must outlive the client |
| Discover | Search/get/count APIs operate on server conversation records | Appropriate server/storage scope and authorization |
| Inspect status | Conversation state includes lifecycle status and metadata | Some operations can wait on state locks/executors |
| Inspect transcript/events | Persisted event history and event APIs | Token deltas are a separate transient stream |
| Inspect current/latest activity | Live events, tool observations, persisted messages and status | Exact visibility depends on the active tool and stream |
| Non-interrupting input | Appends a user message to the same conversation and continues normal native execution | Does not rewrite an already-issued model request |
| Interrupt/redirect | Cancellation plus new input and continuation use the same conversation | Effect termination depends on executor behavior |
| Pause | Requests suspension between steps | Not immediate cancellation of a model/tool action |
| Resume/continue | Runs against retained state/history | Not transparent restoration of in-flight action internals |
| Final result | Dedicated final-response endpoint | A final response must exist |
| Error inspection | Error state and persisted failure observations/events | Runtime failure may require recovery reconciliation |
| Delete | Removes session state and closes resources | Workspace is preserved; deletion is not pause |

Evidence: [conversation_router.py](../../repos/software-agent-sdk/openhands-agent-server/openhands/agent_server/conversation_router.py), line 89 onward; [event_router.py](../../repos/software-agent-sdk/openhands-agent-server/openhands/agent_server/event_router.py); [event_service.py](../../repos/software-agent-sdk/openhands-agent-server/openhands/agent_server/event_service.py), line 729 onward.

### Client and process independence

Execution belongs to an EventService task on the server. It is not scoped to the original HTTP request. Another client can discover the stable identity and use the same control APIs without possessing the original in-memory client object.

`RemoteConversation.close()` stops the client's WebSocket and related local resources. `delete_on_close` defaults to false; deleting the server conversation is opt-in. This supports detach semantics.

The guarantee does not apply to every possible wrapper. A CLI that owns and tears down the server or execution workspace can still terminate the session. The reusable server/client behavior supports independent lifetime, but integration must preserve it.

Evidence: [remote_conversation.py](../../repos/software-agent-sdk/openhands-sdk/openhands/sdk/conversation/impl/remote_conversation.py), lines 704 and 1683 onward.

### Non-interrupting update details

Normal native-agent message submission appends to conversation history. If work is already active, it need not cancel that work. The execution loop detects new user input and incorporates it at subsequent processing boundaries. EventService also handles input arriving near run completion so that it is not simply stranded after cleanup.

The state lock is released for model I/O, but not universally for every tool operation. Therefore submission/inspection can be delayed by a tool-held state lock. Non-interrupting update means preserving the active session and allowing ongoing work; it does not guarantee instantaneous injection into an already-running model request.

Normal user input also stops OpenHands' separate goal loop. That optional upstream feature should not silently become an orchestration owner. ACP prompt execution has superseding/cancellation behavior different from the normal native-agent path.

Evidence: [event_service.py](../../repos/software-agent-sdk/openhands-agent-server/openhands/agent_server/event_service.py), line 729 onward; [local_conversation.py](../../repos/software-agent-sdk/openhands-sdk/openhands/sdk/conversation/impl/local_conversation.py), lines 1813, 1884 and 2250 onward.

### Interruption, stopping and continuation

Native async interruption cancels the active task and signals the cancellation token. Interrupted tool actions receive synthetic failure observations to keep conversation history usable. The session can enter a paused state and later continue.

The server waits for interruption cleanup for a bounded interval and retains the active-task reference if cleanup has not finished. This avoids immediately starting overlapping execution against the same session merely because cancellation was requested.

Synchronous execution has weaker interruption behavior: task cancellation cannot forcibly terminate arbitrary Python threads, and fallback behavior may amount to pausing at a later boundary.

| Active operation | What interrupt does | What it does not guarantee |
|---|---|---|
| Async model call | Cancels the local awaiting task/request path | Provider-side computation or billing has stopped |
| Terminal command | Signals tool-owned terminal sessions, typically Ctrl+C/SIGINT behavior | Every descendant exits; a process cannot ignore the signal |
| Browser operation | Agent task can stop awaiting the tool | Browser executor has no interrupt override guaranteeing the operation stops |
| Arbitrary synchronous tool | Signals the executor hook if implemented | Running thread is terminated |
| Filesystem mutation | Stops future agent direction where cancellation is observed | Prior writes are rolled back |
| External API operation | Stops local continuation where applicable | A submitted external action is undone |
| Conversation history | Records interruption/failure and retains prior context | In-flight external effect and recorded cancellation become atomic |

Evidence: [ToolExecutor interrupt contract](../../repos/software-agent-sdk/openhands-sdk/openhands/sdk/tool/tool.py), line 317 onward; [parallel_executor.py](../../repos/software-agent-sdk/openhands-sdk/openhands/sdk/agent/parallel_executor.py), line 237 onward; [terminal executor](../../repos/software-agent-sdk/openhands-tools/openhands/tools/terminal/impl.py), line 581 onward; [browser executor](../../repos/software-agent-sdk/openhands-tools/openhands/tools/browser_use/impl.py).

There is no need to redefine stop as deletion. Pause/cancel with retained history and later continuation already supplies useful stop/resume behavior. A separate durable, irreversible stop tombstone is not established, but it is not a frozen requirement.

### Persisted and transient state

| State or resource | Persistence/recovery finding |
|---|---|
| Session UUID and metadata | Persisted and discoverable |
| Base conversation state/status | Persisted; reconciled during recovery |
| Settled messages and execution events | Persisted |
| Final response | Available from retained conversation history/state |
| Large tool observations | Can be persisted as session-associated observation files |
| Workspace files | Survive according to workspace/storage lifetime |
| Live execution task, locks, subscribers | In-memory; reconstructed or replaced after restart |
| Streaming token/thinking deltas | Live publication, not persisted as conversation events |
| Active browser/tool/process internals | Not transparently restored as arbitrary in-flight actions |

On restart, formerly running sessions undergo error/recovery reconciliation, including unmatched tool-call handling. This is stronger than losing all history but different from transparent crash continuation.

Client exit preserves server execution. Server or machine restart preserves persisted files, not arbitrary in-flight execution. A machine restart also requires the service to start again. The product does not require stronger arbitrary-action recovery, so no separate durable workflow platform is justified.

Evidence: [EventService stream publication and recovery](../../repos/software-agent-sdk/openhands-agent-server/openhands/agent_server/event_service.py), lines 1056 and 1140 onward; [conversation catalog/recovery](../../repos/software-agent-sdk/openhands-agent-server/openhands/agent_server/conversation_service.py), lines 674 and 2095 onward; [conversation leases](../../repos/software-agent-sdk/openhands-agent-server/openhands/agent_server/conversation_lease.py).

### CLI and Claude/Codex integration

The reusable foundation is the structured server API and remote client. The complete product CLI lifecycle surface is not established as an existing drop-in command suite. A machine-facing CLI adaptation is therefore `ADAPT_THINLY`, while session execution, persistence and control remain owned upstream.

The necessary surface is functional: create, discover, inspect, retrieve events, submit input, interrupt, stop, continue, retrieve result and retrieve artifact references/content. No command names, CLI framework or terminal UI are selected here.

Claude Code's behavior remains the requested product-level target. Its local reference repository did not provide a reusable OSS implementation of the core Claude session runtime. Exact protocol compatibility is neither required nor claimed.

## 6. Parallelism findings

### Source-supported concurrency model

OpenHands maintains independent conversation records and EventServices. Native async agent execution uses server-side async tasks, with per-session synchronization rather than a universal execution lock.

The ten-worker `max_concurrent_runs` setting belongs to synchronous fallback execution. It does not imply that only ten native async sessions can progress.

The conversation service loads a lightweight catalog and hydrates session execution/history objects on demand. Idle sessions remain unloaded; list operations operate on catalog information and materialize information for selected page items rather than eagerly hydrating every transcript. Searches still perform catalog scanning/sorting and status reconciliation where needed.

Relevant upstream tests include lazy restart/list behavior and single hydration under concurrent access. These support the interpretation of the implementation without establishing real workload capacity.

Evidence: [conversation_service.py](../../repos/software-agent-sdk/openhands-agent-server/openhands/agent_server/conversation_service.py), lines 674, 714, 1265 and 2095 onward; [test_conversation_service.py](../../repos/software-agent-sdk/tests/agent_server/test_conversation_service.py), line 860 onward.

### Remaining bottlenecks

| Area | Source-supported concern | Consequence |
|---|---|---|
| Single-tool execution | Uses the shared default executor | Long-running calls can compete with other thread-backed operations |
| Multi-tool batches | Dedicated per-batch thread pools | Concurrent batches can increase thread/resource use |
| State access | Some operations hold locks or use shared executors | Status/input responsiveness depends partly on tool duration |
| Search/catalog | Catalog scanning/sorting and reconciliation remain | Discovery is not constant-time, though it avoids full history hydration |
| Event persistence | File/state operations still consume I/O and execution resources | Storage latency matters; no throughput guarantee is claimed |
| Provider connections and limits | Client/provider-dependent | No universal promise of 100 simultaneous model requests |
| Cancellation cleanup | Synchronous thread-pool shutdown inside async control flow | Non-cooperative tools can delay the shared event loop |

### Confirmed conditional cancellation coupling

The multi-action async executor uses a synchronous `with ThreadPoolExecutor(...)` block around awaited work. When cancellation propagates out of the block, the context manager waits for its running threads. `_arun_safe` calls an executor's interrupt hook but does not terminate its thread; the base hook is a no-op.

**Inference from explicit source and Python semantics:** a tool that does not finish after interruption can make this shutdown wait occur on the shared event loop, delaying unrelated session control. This is a particular failure path, not evidence that normal execution is globally serialized.

Evidence: [parallel_executor.py](../../repos/software-agent-sdk/openhands-sdk/openhands/sdk/agent/parallel_executor.py), lines 161–253; Python [executor shutdown documentation](https://docs.python.org/3/library/concurrent.futures.html#concurrent.futures.Executor.shutdown).

### Assessment against the requirement

Approximately 50–100 retained active/background sessions are structurally plausible. The source does not reveal a requirement to redesign the session model around that count. Native async progress, independent IDs/control, and lazy state loading are favorable.

The complete practical capacity requirement remains **unverified**, rather than unsupported. The identified cleanup coupling is a separate confirmed reliability issue affecting independent control under problematic tools.

No additional benchmark campaign is requested. Earlier synthetic timing observations are not used as proof of a global lock, a hard session ceiling, or production unsuitability.

## 7. Provider compatibility findings

### Independent candidate paths

**OpenHands → LiteLLM** has the lowest demonstrated integration cost with the leading session candidate. Its message representations, metrics, context management and error behavior are already part of that execution loop.

**Pydantic AI → native OpenAI/Anthropic adapters** is a strong independent comparison for explicit protocol handling and model profiles. Its existence does not prove that replacing OpenHands' provider layer is thin or necessary.

No composition of both abstractions on one request path is selected. History conversion, reasoning data, retries, errors, usage and cancellation would all need to retain coherent ownership. Multiple abstraction layers do not automatically preserve semantics.

### Capability matrix

| Capability | OpenHands/LiteLLM evidence | Pydantic AI evidence | Compatibility qualification |
|---|---|---|---|
| Streaming | Streaming LLM support and Agent Server callback wiring | Native streamed model-response implementations | OpenHands without a token callback can fall back to non-streaming |
| Tool calls | Tool-call serialization and agent execution | Native tool-call response models | IDs/names/arguments must survive translation |
| Tool-result round trips | Chat/Responses serialization and reasoning tests | OpenAI and Anthropic adapter tests | Protocol-specific ordering and required content matter |
| Parallel tool calls | Supported options and parallel agent tool execution | Provider options and adapter coverage | Model/endpoint may restrict actual behavior |
| Structured output | Native or translated schema handling | Native/tool output strategies with model profiles | A tool-based schema fallback is not native output-format equivalence |
| Reasoning/thinking | Thinking blocks, signatures, reasoning content and Responses representations | Native reasoning/thinking fields and profile-driven serialization | Cross-protocol signatures, redacted blocks and encrypted reasoning are not universally interchangeable |
| Usage | Metrics and provider usage parsing | Usage parsing and tests | Completeness depends on endpoint fields; pricing for unknown IDs may be unavailable |
| Long histories | Conversation history and condensation | History/context management | Model limits and semantic effects of condensation still apply |
| Cancellation | Async cancellation through model execution | Stream/request cancellation paths | Local cancellation is not provider-side termination assurance |
| Timeout/retries | Configured timeout/retry behavior | Framework and underlying client behavior | Categories/defaults differ; avoid overlapping retry ownership |
| Provider failures | Error mapping and connection-error retry coverage | Status/connection error coverage | Compatible endpoints may use different payloads/status conventions |
| Custom model IDs | Provider routing and capability overrides | Custom profiles and provider selection | Unknown name does not supply reliable capability metadata automatically |
| Multiple providers at once | Per-session LLM config | Per-agent/model config | No inherent single-vendor requirement |

### Concrete OpenHands/LiteLLM consequences

OpenHands' LLM configuration includes `drop_params=True`, and the integration enables LiteLLM parameter modification. This is relevant to fidelity: successful transport does not prove that all requested fields were sent unchanged.

Inspection of the locked LiteLLM implementation established examples of client adaptation:

- Unsupported output/reasoning settings can be removed according to model capability handling.
- Anthropic structured-output requests can become a synthetic tool schema when native support is not selected.
- Thinking settings can be removed when the available conversation representation lacks necessary thinking blocks.

These behaviors belong to the client/runtime path. They should not be reported as 9Router failures.

OpenHands also requires a token callback for its streaming path; setting `stream=True` without one can produce non-streaming execution. The Agent Server supplies the callback for its configured stream path, so this is not a claim that server streaming is broken.

Custom model IDs need the appropriate provider route and capability metadata, including context limits, reasoning options and API mode. Unknown-model routing and unknown-model capabilities are separate concerns.

### 9Router issue classification

| Finding | Required classification |
|---|---|
| Client removes or transforms a requested parameter | Client/runtime limitation |
| Native thinking, hosted tools or response-state semantics do not map across protocols | Protocol compatibility limitation |
| Selected model does not implement a requested capability | Model limitation |
| Specific current 9Router implementation defect | None confirmed |
| Actual behavior through the future configured endpoint | Unverified |

9Router stays external to architectural ownership. Its API may be compatible enough for ordinary tool-capable agent workloads while particular native capabilities still depend on protocol/model behavior. No live test is claimed, and no untested feature is classified as unsupported.

### Provider evidence

- [OpenHands llm.py](../../repos/software-agent-sdk/openhands-sdk/openhands/sdk/llm/llm.py): provider invocation, configuration, streaming and parameter handling.
- [OpenHands message.py](../../repos/software-agent-sdk/openhands-sdk/openhands/sdk/llm/message.py): message and reasoning representations.
- [Responses serialization tests](../../repos/software-agent-sdk/tests/sdk/llm/test_responses_serialization.py).
- [Reasoning-content tests](../../repos/software-agent-sdk/tests/sdk/llm/test_reasoning_content.py).
- [Canonical-model resolution tests](../../repos/software-agent-sdk/tests/sdk/llm/test_model_canonical_name_resolution.py).
- [API connection retry tests](../../repos/software-agent-sdk/tests/sdk/llm/test_api_connection_error_retry.py).
- [Pydantic OpenAI adapter](../../repos/pydantic-ai/pydantic_ai_slim/pydantic_ai/models/openai.py) and [provider](../../repos/pydantic-ai/pydantic_ai_slim/pydantic_ai/providers/openai.py).
- [Pydantic Anthropic adapter](../../repos/pydantic-ai/pydantic_ai_slim/pydantic_ai/models/anthropic.py) and [provider](../../repos/pydantic-ai/pydantic_ai_slim/pydantic_ai/providers/anthropic.py).
- [Pydantic OpenAI tests](../../repos/pydantic-ai/tests/models/test_openai.py) and [Anthropic tests](../../repos/pydantic-ai/tests/models/test_anthropic.py).

The inspected locked diagnostic dependencies included LiteLLM `1.93.0`, OpenAI Python `2.33.0`, and Anthropic Python `0.75.0`. LiteLLM's `llms/anthropic/chat/transformation.py` was inspected in that installed dependency. Dependency-source findings should be rechecked when versions change; they are not assertions about every future release.

## 8. Skill compatibility findings

### Verified portable subset

| Skill element | Portable behavior | Difference or limit |
|---|---|---|
| `SKILL.md` body | Instructions can be loaded and used | Instructions cannot grant authority |
| `name`, `description` | Recognized by candidate loaders | Validation and prompt presentation limits differ |
| Standard metadata | Some retained descriptively | Preservation is not behavioral enforcement |
| `scripts/` | Script files can remain in the shared directory | Running them requires the applicable execution authority |
| `references/` | Reference files remain available | Loading is generally selective, not automatic inclusion of all content |
| `assets/` | Supporting assets can remain available | Actual access depends on filesystem scope |
| Other resource directories | Files can still be accessed as permitted | Not every directory name receives special discovery treatment |
| `allowed-tools` | OpenHands parses it | It is not an authoritative tool ceiling |
| `disable-model-invocation` | OpenHands recognizes it | Explicit invocation and trigger behavior are not completely equivalent to Claude |
| Claude `model`, `agent`, `context`, `hooks` and related fields | Some are ignored or treated only as metadata by inspected loaders | No general promise of equivalent execution semantics |
| Inline dynamic command syntax | OpenHands executes supported syntax during rendering | Runs with rendering-process authority outside terminal sanitization |
| Skill-associated MCP configuration | Metadata/configuration can be loaded | Loading does not prove externally authorized tool exposure or server launch |

### OpenHands versus narrower loaders

OpenHands has relatively broad AgentSkills-style support, including supporting resources and invocation behavior. Its description presentation can truncate content, and metadata normalization is not exact preservation of every Claude field's semantics.

Pydantic AI Harness deliberately identifies several behavioral frontmatter fields as ignored. This provides a narrower and more explicit portable subset rather than a guarantee of Claude execution equivalence.

The conclusion is not that every skill needs conversion. A directory containing ordinary instructions and supporting files can be shared. A skill depending on Claude-specific hooks, context isolation, model selection, or permission declarations cannot be assumed to behave identically.

### Authority consequence

The externally assigned profile must remain authoritative even for trusted skills. The current OpenHands dynamic renderer does not establish that property. Removing useful skills or inventing a new DSL is not justified; the required distinction is between portable content and executable behavior that must obey the same authority boundary.

Evidence: [OpenHands skill loader](../../repos/software-agent-sdk/openhands-sdk/openhands/sdk/skills/skill.py), [skill command renderer](../../repos/software-agent-sdk/openhands-sdk/openhands/sdk/skills/execute.py), [skill invocation tool](../../repos/software-agent-sdk/openhands-sdk/openhands/sdk/tool/builtins/invoke_skill.py), [Pydantic Harness loader](../../repos/pydantic-ai-harness/pydantic_ai_harness/skills/_loader.py), [Deep Agents skills](../../repos/deepagents/libs/deepagents/deepagents/middleware/skills.py).

**Classification:** `ADAPT_THINLY` for sharing the verified content subset; `GAP_REMAINS` for claiming complete externally bounded executable-skill semantics in the current OpenHands integration.

## 9. Artifact findings

### Existing capabilities

OpenHands already exposes sufficient building blocks for the minimum artifact requirement:

- Conversation-to-workspace association.
- Session-scoped retrieval of known workspace files.
- Content-type handling for served files.
- File download and archive capabilities.
- Persisted tool observations and large-output files associated with conversation state.
- Events and final responses that can carry explicit artifact paths or identities.

The workspace router resolves paths against the stored conversation workspace and checks path traversal and symlink escape. A directory is not automatically an inventory response: it may serve an index file, otherwise return not found.

The more general file routes have their own authorization and path behavior. Their existence should not be interpreted as a per-session permission policy merely because a session-scoped workspace route also exists.

Evidence: [workspace_router.py](../../repos/software-agent-sdk/openhands-agent-server/openhands/agent_server/workspace_router.py), [file_router.py](../../repos/software-agent-sdk/openhands-agent-server/openhands/agent_server/file_router.py).

### Association in shared workspaces

An isolated workspace provides an obvious container of session files, but even then not every file is necessarily a final artifact. In a deliberately shared workspace, location alone cannot identify which Agent Session created or owns a file.

Explicit artifact references in session-associated results/events are sufficient for the stated minimum when paired with existing retrieval APIs. Persisted observations also supply already-associated output files where applicable.

No generic exhaustive artifact event or inventory was established for arbitrary shell-created files. That is a limitation, not evidence that a dedicated artifact platform is required. The product did not require automatic complete provenance for every filesystem effect.

**Classification:** `ADAPT_THINLY`. Preserve workspace, event and result ownership. No separate artifact subsystem is justified by the current requirement.

## 10. Scheduling findings

### OpenHands Automation ownership

The Automation dispatcher prepares execution inputs, selects backend execution, and coordinates watchdog behavior. The local backend supplies a run-specific workspace base and execution environment. Watchdog logic owns deadlines and terminal automation state.

Those responsibilities overlap the Agent Runtime's required ownership of workspace selection, execution and lifecycle. Extracting trigger behavior may be possible, but the inspected implementation does not establish it as a thin drop-in integration.

Evidence: [scheduler.py](../../repos/automation/openhands/automation/scheduler.py), [dispatcher.py](../../repos/automation/openhands/automation/dispatcher.py), [local backend](../../repos/automation/openhands/automation/backends/local.py), [watchdog.py](../../repos/automation/openhands/automation/watchdog.py).

**Classification:** `REJECT` for adopting OpenHands Automation unchanged as a trigger-only owner; `PATTERN_ONLY` where its scheduling/claiming behavior is informative. This is not a rejection of the project for its own intended use.

### External scheduling remains possible

Generic OSS scheduling can activate a short dispatch operation while leaving the resulting Agent Session owned by the runtime. For example, systemd timers support calendar scheduling and persistent timer behavior; path units support filesystem triggers; service units execute the dispatching process.

This establishes a reusable Linux option. It does not select Linux, define a CLI, or establish native-Windows integration. Filesystem events are not equivalent to arbitrary external webhook integrations. Scheduler invocation history and Agent Session history also remain distinct.

Sources: [systemd.timer source](https://raw.githubusercontent.com/systemd/systemd/main/man/systemd.timer.xml), [systemd.path source](https://raw.githubusercontent.com/systemd/systemd/main/man/systemd.path.xml), [systemd.service source](https://raw.githubusercontent.com/systemd/systemd/main/man/systemd.service.xml).

**Conclusion:** scheduling can remain an optional external authorized dispatcher. Rich automation history and additional event sources can remain outside current scope. No custom scheduler is warranted.

## 11. Final requirement-to-OSS matrix

| Major requirement | OSS owner candidate | Evidence | Reuse classification | Remaining gap or qualification |
|---|---|---|---|---|
| Full autonomous agent execution | OpenHands SDK | Agent loop, tool execution, LLM integration | `REUSE_DIRECT` | Actual capability follows assigned tools and permissions |
| External orchestrator retains task/model/workspace decisions | Agent Server request/control API | Conversation request and lifecycle routes | `REUSE_WITH_CONFIGURATION` | Optional upstream goal/delegation features must not silently take ownership |
| Worker cannot create Agent Sessions | Server authentication plus execution boundary | Auth, terminal sanitation, uncovered skill subprocess | `GAP_REMAINS` | Current integration does not protect every path to dispatcher authority |
| Per-session model/provider | OpenHands LLM configuration | Request and LLM implementations | `REUSE_WITH_CONFIGURATION` | Unknown-model capability metadata must be accurate |
| Per-session skills | Existing skill loader and agent context | Skill loading and invocation | `ADAPT_THINLY` | Portable subset and executable behavior differ |
| Per-session tools | OpenHands tool resolution | Tool specifications, filter, built-ins | `ADAPT_THINLY` | Automatic tools must remain subordinate to the profile |
| Authoritative permission profile | Existing tool, OS and service boundaries | Section 4 enforcement mapping | `GAP_REMAINS` | Complete composition across skills, shell, custom code and credentials is absent |
| Noninteractive execution under granted authority | Confirmation policy | Never-confirm behavior | `REUSE_WITH_CONFIGURATION` | Must coexist with real restrictive enforcement where requested |
| Detached/background execution | Agent Server | Server-owned task | `REUSE_WITH_CONFIGURATION` | Server lifetime independent of CLI/workspace teardown |
| Separate-client discovery | Conversation catalog/API | Search/get/count | `REUSE_DIRECT` | Shared service/storage scope and authorization |
| Stable session identity | Conversation UUID | Request, persistence and routes | `REUSE_DIRECT` | Upstream name remains conversation internally |
| Status/timestamps/errors | Conversation state/API | State and recovery | `REUSE_DIRECT` | Responsiveness can depend on executor/lock behavior |
| Transcript and structured events | Event service/API | Persisted event history | `REUSE_DIRECT` | Partial streaming deltas are transient |
| Non-interrupting instruction | Native EventService path | Append and continuation logic | `REUSE_DIRECT` | Incorporation at processing boundaries |
| Interrupting redirect | Native async lifecycle | Cancellation and history reconciliation | `ADAPT_THINLY` | Active effects can outlive interruption; cleanup coupling remains |
| Stop/pause and continuation | Conversation lifecycle | Pause/interrupt/run | `REUSE_DIRECT` | Continuation is not in-flight action restoration |
| Final result | Final-response API | Conversation router | `REUSE_DIRECT` | Requires a final response |
| Artifact retrieval and association | Workspace/file/events/results | Section 9 evidence | `ADAPT_THINLY` | Explicit association for arbitrary shared-workspace outputs |
| Shared or isolated workspaces | LocalWorkspace; optional worktrees/backends | Request/workspace handling | `REUSE_WITH_CONFIGURATION` | Workspace sharing is distinct from authority isolation |
| Optional remote capabilities retained | Existing workspace/backend abstractions | Pass 1 retained evidence | `REUSE_WITH_CONFIGURATION` | Not a v1 requirement; no reason established to remove |
| 50–100 independent concurrent sessions | Native async Agent Server | Per-session tasks and lazy catalog | `ADAPT_THINLY` | Practical capacity unverified; executor cleanup issue confirmed |
| State survives originating interaction | Agent Server and persistent conversation state | Server/client lifetime and persistence | `REUSE_DIRECT` | Independent service lifetime required |
| Reconnect and useful restart recovery | Remote client and persisted state/events | Reconciliation and recovery code | `REUSE_DIRECT` | No transparent arbitrary-action continuation |
| OpenAI/Anthropic compatible APIs | Existing LiteLLM or Pydantic AI path | Adapters and upstream tests | `REUSE_WITH_CONFIGURATION` | One request-path owner; profile and translation differences |
| 9Router endpoint | Same compatible client paths | No router-specific failure established | `REUSE_WITH_CONFIGURATION` | End-to-end behavior unverified; router remains external |
| Machine-consumed CLI | Existing REST API and remote client | Complete lifecycle primitives | `ADAPT_THINLY` | Full product command surface not established as drop-in CLI |
| Claude-native-like functional control | Existing lifecycle primitives | Section 5 operations | `ADAPT_THINLY` | Behavioral equivalence only; exact protocol not required |
| Optional scheduling | External generic OSS scheduler | Timer/path/service primitives | `REUSE_WITH_CONFIGURATION` | Platform-specific; richer automation optional |
| OpenHands Automation as trigger-only owner | Automation dispatcher/backend | Workspace and watchdog ownership | `REJECT` | Exceeds scheduler ownership boundary |

### Ownership conflicts to retain in architecture evaluation

Although this pass does not select a stack, the following overlaps are established:

- OpenHands and another full harness can both own the agent loop, tool execution, history and interruption. Combining them is not automatically selective reuse.
- OpenHands/LiteLLM and Pydantic AI can both own provider requests, history serialization, retries, usage and reasoning conversion. No need for dual ownership was demonstrated.
- Agent Server and an additional workflow/lifecycle platform can both own continuation, cancellation, recovery and session status. Stronger durability alone does not justify that overlap under the frozen requirements.
- OpenHands Automation and the Agent Runtime would overlap workspace, execution and timeout ownership if adopted unchanged.
- Skill-declared tools or behavior must not become a second authority source alongside the orchestrator's permission profile.
- An artifact layer duplicating workspace and event state is not justified by the minimum retrieval requirement.

The reusable capabilities that should almost certainly not be reimplemented are the agent loop, conversation persistence, event retrieval, provider protocol adapters, workspace file serving, and generic scheduling primitives.

## 12. Remaining blockers

### Confirmed blockers

**1. The externally assigned permission profile is not authoritative across all inspected execution paths.**

Skill rendering can execute shell commands with process authority outside terminal sanitization and workspace execution controls. Automatically attached tools also bypass ordinary regex filtering. These are concrete implementation behaviors, not speculative threats or absent test coverage.

The profile requirement includes read-only/restricted execution and explicitly forbids skills from widening authority. The current inspected integration cannot be declared compliant with that requirement.

**2. Worker exclusion from dispatcher authority is not established by the current integration.**

Authentication exists, but credentials and the enforcing service must be outside worker authority. Terminal sanitization does not cover the skill subprocess path, filesystem/process access to control resources, or literal unrestricted host administration.

This blocker is closely related to the first: both concern incomplete authority coverage. It is listed separately because root-only dispatch is an independent frozen requirement and must remain enforceable even for otherwise broadly capable workers.

**3. Independent lifecycle responsiveness has a confirmed conditional failure path.**

Parallel-tool cancellation can exit through synchronous thread-pool shutdown on the event loop. Non-cooperative work can consequently delay unrelated control operations. Browser/custom executor behavior makes it unsafe to assume every worker finishes promptly after the interrupt hook.

This is a bounded runtime reliability issue. It does not imply that ordinary execution is serialized, that every cancellation blocks, or that a new session runtime is necessary.

### What these blockers do not establish

They do not prove that no OSS components can satisfy the requirements. Authentication, OS isolation, tool scoping and lifecycle primitives already exist. The missing or insufficient part is the reviewed integration's coverage and the identified cleanup behavior.

They do not authorize implementation, mandate a custom policy engine, or require a broad new repository survey. Nor does this report label a prospective integration thin merely because an API exists.

### Non-blocking limitations

| Limitation | Why it is not an architecture-freeze blocker by itself |
|---|---|
| Exact 50–100-session capacity unverified | User requested source/runtime focus; architecture is concurrent and no benchmark gate is imposed |
| Live 9Router behavior unverified | Router remains external; no defect demonstrated; integration issues may be fixed later |
| Native-Windows sandbox composition not established | No native-Windows execution requirement was frozen; a Windows workspace path alone does not select runtime isolation technology |
| Cancellation does not undo completed effects | Rollback of arbitrary filesystem/API actions is not required |
| Some descendants or tools can continue after cancel | Must be described honestly; universal immediate effect termination was not frozen, though global control blockage is a separate blocker |
| Streaming deltas are not durable | Settled transcript/events remain available; every partial token is not required |
| No exhaustive artifact attribution for arbitrary shared writes | Explicit artifact references meet the minimum; complete provenance was not required |
| Claude-specific skill behavior differs | A shared portable subset is usable; exact semantics were qualified by the product |
| Product CLI needs adaptation | Existing structured lifecycle operations avoid custom lifecycle machinery |
| Rich scheduling remains optional | Product explicitly permits deferral |
| Arbitrary in-flight crash recovery absent | Stronger recovery is not mandatory and does not justify extra workflow ownership |

## 13. Architecture-freeze readiness

**`BLOCKED_BY_CONFIRMED_GAPS`**

The majority of the runtime can be reused. OpenHands remains a credible owner for agent execution, persistent Agent Sessions, history, workspace access and lifecycle control. Existing provider and skill capabilities also supply substantial reusable behavior.

Architecture freeze is blocked specifically by:

1. Executable skill and automatic-tool paths that are not consistently subordinate to the externally assigned permission profile.
2. Incomplete protection of dispatcher authority from broadly capable workers across those execution paths.
3. Cancellation cleanup that can couple one non-cooperative tool batch to shared event-loop responsiveness.

The verdict is about the reviewed reuse paths, not a conclusion that OSS lacks suitable enforcement primitives. It does not select a final stack or prescribe custom implementation methods.

The unresolved architecture decisions concern enforceable authority coverage and the identified runtime cancellation behavior. They do not require another broad survey, testing in this environment, a benchmark campaign, a new artifact platform, a new scheduler, or a replacement agent execution loop.

### Evidence preservation

Local source links refer to the unchanged reference clones at the revisions listed in Section 2. Referenced line locations describe those inspected revisions and may move after an upstream update. Public `main` links are supplemental moving-source evidence, not pinned dependencies.

Pass 1 remains unchanged. This file records research only; it introduces no production code, scaffolding, database schema, CLI syntax, module structure or implementation backlog.
