# Agent Runtime — Đối chiếu OSS để bổ sung phần còn thiếu

Ngày: 2026-09-05.  
Phạm vi: nghiên cứu source, không implement, không vendor hoặc sửa reference repositories, không chạy test/benchmark.  
Tài liệu nền: [Pass 1](RESEARCH_PASS_1.md), [Pass 2](RESEARCH_PASS_2.md).

## 1. Kết luận đã điều chỉnh

**Chọn OpenHands Software Agent SDK + Agent Server làm repo nền cho hướng tích hợp đang đánh giá. Với từng phần thiếu, ưu tiên lấy implementation hoặc primitive có sẵn từ các repo khác và dependency của chúng.** Đây là lựa chọn repo nền theo yêu cầu đã làm rõ của người dùng; không phải chốt toàn bộ stack hoặc kiến trúc triển khai.

Lý do giữ OpenHands: phần execution loop, Agent Session, background lifetime, history, discovery, steering, continuation, workspace và result đã gần yêu cầu nhất. Đổi toàn bộ runtime để lấy một permission middleware hoặc hàm cancellation sẽ đánh đổi nhiều phần đã đáp ứng.

Lần đối chiếu này tìm được donor cụ thể, không chỉ tên framework:

| Phần cần bổ sung | Implementation đáng lấy |
|---|---|
| Chặn tool action thực sự | OpenHands `HookEventProcessor` + `block_action` đã có trong repo nền |
| Quy tắc filesystem theo operation/path | Deep Agents `FilesystemPermission`, `_check_fs_permission` và các kiểm tra bulk-operation liên quan |
| Resolve path và kiểm tra containment | Pydantic Harness `FileSystemToolset` |
| Thực thi shell trong boundary tách khỏi server | Microsoft `DockerShellTool`; sandbox-runtime là lựa chọn bổ sung khi cần filesystem/network policy chi tiết hơn |
| Dừng cây tiến trình | Microsoft `kill_process_tree` và implementation timeout của shell/container |
| Tránh đợi thread vô hạn tại mỗi lần timeout/cancel | Deep Agents glob executor; Pydantic AI executor helpers; AnyIO thread primitives |
| Cleanup browser khi bị cancel | Pydantic Harness browser-use `_kill`, `_close_session`, pending-cleanup handling |
| Tách đọc nội dung skill khỏi thực thi script | Microsoft `FileSkill`/`FileSkillScript`/`SkillScriptRunner`; Pydantic Harness passive skill loader |
| Kết nối executor async với tool sync của OpenHands | OpenHands `AsyncExecutor`/AnyIO `BlockingPortal` đã có |

**Không còn cơ sở diễn giải ba blocker trong Pass 2 thành “OSS không có phần giải quyết, phải tự code”.** Các lỗi của OpenHands vẫn có thật; nhưng đã có implementation donor liên quan, trong đó có một implementation xử lý trực tiếp cùng loại lỗi thread-pool shutdown.

Chưa có hạng mục nào trong đợt đối chiếu này chứng minh rằng phải tự xây mới agent loop, session manager, policy engine tổng quát, process manager, browser manager hay scheduler.

Việc có donor không đồng nghĩa ghép đã hoàn tất. Tài liệu phân biệt rõ module có thể dùng trực tiếp, logic có thể chuyển chọn lọc, và framework chỉ nên tham khảo. Không gọi toàn bộ một subsystem là `ADAPT_THINLY` chỉ vì nó có API.

## 2. Phạm vi và mức độ bằng chứng

Nghiên cứu tập trung vào ba nhóm thiếu đã nêu trước đó: permissions, dispatcher authority và cancellation/control responsiveness. Các khả năng đã đủ bằng chứng trong Pass 1/2 không được khảo sát rộng lại.

Các commit local vẫn là:

| Repo | Ref |
|---|---|
| OpenHands SDK | `f47083cc370a85160f0348f32e531ee3514399e5` |
| Pydantic AI | `b57cec28acdb836ec98fa29257225eb1b4e3e104` |
| Pydantic AI Harness | `41d51a828880c1e33155f2fc77e8e623e21483ef` |
| Deep Agents | `4e5f9350e4d77b8bf19e472e8414662d3fa59dc0` |
| Microsoft Agent Framework | `cc8c1fa0a4c718a4a4cdbcee3340f0bf01746006` |

“Có test upstream” nghĩa là đã đọc test để kiểm tra contract và tình huống được bao phủ, không có nghĩa test đã được chạy trong lần nghiên cứu này. Không xác nhận hiệu năng 50–100 session, khả năng cleanup mọi process trong mọi tình huống, hoặc tính tương thích dependency sau khi ghép bằng thực nghiệm.

Sandbox-runtime và AnyIO có thêm bằng chứng từ source/docs công khai. Sandbox-runtime không có clone local trong danh sách reference repositories; phần đó là bằng chứng bổ sung và không được trình bày như một integration đã kiểm chứng tại local.

## 3. Những capability thực ra đã có trong repo nền

### 3.1. PreToolUse không chỉ là thông báo

Pass 2 tập trung vào confirmation policy và tool filtering, nhưng bỏ sót một điểm ghép quan trọng: OpenHands có hook chặn execution thực sự.

Luồng source:

- `HookEventProcessor.on_event` nhận `ActionEvent`.
- `_handle_pre_tool_use` gọi `HookManager.run_pre_tool_use` với tên tool và input đã được chuyển thành dữ liệu.
- Khi verdict không cho tiếp tục, processor ghi `conversation.state.block_action(action_id, reason)`.
- Đường xử lý action trong agent đọc `pop_blocked_action` và tạo rejection thay vì thực thi action.

Đây là capability có thể tái sử dụng để nối quyết định policy vào agent loop; không cần tự tạo thêm một tool interception framework.

**Giới hạn cần giữ nguyên trong đánh giá:** hook async không chặn; nếu state chưa được gắn, processor không thể chặn; một số lỗi hook mặc định cho đi tiếp; command hook dùng exit code 2 để block, còn exit code lỗi khác không tự block. Vì vậy không được coi cấu hình hook bất kỳ là policy fail-closed hoàn chỉnh.

Hook này nhìn thấy model tool action, kể cả invocation của built-in nếu đi qua action path. Nó không mặc nhiên chặn shell command được chạy khi render skill ngoài action path. Chặn toàn bộ InvokeSkillTool để né vấn đề cũng không đáp ứng hướng giữ capability.

Nguồn:

- [conversation_hooks.py](../repos/software-agent-sdk/openhands-sdk/openhands/sdk/hooks/conversation_hooks.py), `on_event`, `_handle_pre_tool_use`.
- [manager.py](../repos/software-agent-sdk/openhands-sdk/openhands/sdk/hooks/manager.py), `run_pre_tool_use`.
- [executor.py](../repos/software-agent-sdk/openhands-sdk/openhands/sdk/hooks/executor.py), `HookResult.should_continue` và exit-code semantics.
- [agent.py](../repos/software-agent-sdk/openhands-sdk/openhands/sdk/agent/agent.py), các đường `pop_blocked_action`.

Phân loại: `REUSE_WITH_CONFIGURATION` cho interception đã có; binding vào policy và behavior khi lỗi vẫn phải được đánh giá như phần tích hợp.

### 3.2. Đã có cầu nối sync/async

OpenHands `ToolExecutor.__call__` là giao diện sync trả `Observation`; `interrupt()` cũng là sync, có thể được gọi từ thread khác. Nhiều donor executor là async. Đây là khác biệt giao diện thật, nhưng OpenHands đã có `AsyncExecutor` dựa trên AnyIO `BlockingPortal`.

`AsyncExecutor` cung cấp `run_async`, portal access và shutdown có thời hạn. Không cần invent một event-loop bridge mới. Portal task future là một điểm nối có sẵn, nhưng `run_async` hiện không tự biến mọi conversation interrupt thành cancel của operation donor.

Nguồn: [ToolExecutor](../repos/software-agent-sdk/openhands-sdk/openhands/sdk/tool/tool.py), [AsyncExecutor](../repos/software-agent-sdk/openhands-sdk/openhands/sdk/utils/async_executor.py).

Phân loại: `REUSE_DIRECT` cho primitive cầu nối; `ADAPT_THINLY` cho chuyển input/output và cancellation contract của từng executor cụ thể. Không có adapter nào được implement trong nghiên cứu này.

## 4. Donor cho permissions

### 4.1. Pydantic Harness: filesystem containment

Module: [filesystem/_toolset.py](../repos/pydantic-ai-harness/pydantic_ai_harness/filesystem/_toolset.py).

Phần implementation đáng lấy gồm:

- `_resolve_path`: resolve đường dẫn, xử lý symlink và từ chối vượt root.
- `_check_access`: allowed/denied/protected patterns.
- `_safe_resolve`: kiểm tra quyền trên canonical path thay vì chuỗi path chưa chuẩn hóa.
- `_resolve_walk_entry`: lọc từng entry khi list/search để symlink hoặc alias không bỏ qua rule.
- Các kiểm tra mở file và xử lý lỗi liên quan; không chỉ lấy một câu `is_relative_to` rồi bỏ các đường I/O khác.

`FileSystem` capability có tùy chọn read-only; protected patterns có thể cấu hình, bao gồm bỏ mặc định khi orchestrator cấp quyền rộng. Không phải một filesystem bắt buộc chỉ đọc hoặc luôn cấm Git writes.

**Điểm ghép:** file-tool access trong OpenHands. OpenHands giữ tool observation/event và workspace ownership; donor cung cấp kiểm tra path/access.

**Coupling:** `FileSystemToolset` kế thừa `FunctionToolset`, dùng `ModelRetry` và kiểu dữ liệu Pydantic AI. Import toàn capability không đồng nghĩa plug vào OpenHands. Có thể dùng package như thư viện qua adapter, hoặc chuyển nhóm implementation path/access có liên quan; cả hai đều cần map lỗi/result sang contract OpenHands.

**Giới hạn:** chỉ bảo vệ những I/O đi qua nó. Shell hoặc custom Python code chạy ngoài boundary không bị ràng buộc bởi filesystem toolset. Không được dùng nó làm thay thế OS sandbox.

Phân loại: `ADAPT_THINLY` cho nhóm kiểm tra filesystem tại ranh giới tool cụ thể; chưa chứng minh full toolset là drop-in replacement cho file editor của OpenHands.

### 4.2. Deep Agents: rule evaluator và bulk-operation checks

Module: [middleware/filesystem.py](../repos/deepagents/libs/deepagents/deepagents/middleware/filesystem.py), `FilesystemPermission`, `_check_fs_permission`, `_wildcard_delete_overlap` và các helper liên quan.

Có implementation rule theo operation/path và kết quả allow/deny/interrupt. Các thao tác recursive hoặc tìm kiếm cần xét overlap và lọc kết quả; đó là phần hữu ích hơn một allowlist tên tool đơn giản.

**Semantics không được bỏ qua:** evaluator chọn rule khớp đầu tiên; không khớp thì allow. Nó không tự có nghĩa deny-by-default hoặc deny-always-wins. Path dùng dạng POSIX; có dependency `wcmatch`. Ghép với canonical path của filesystem donor phải thống nhất semantics, không xếp hai evaluator chồng lên nhau rồi giả định cùng nghĩa.

**Điểm ghép:** quyết định operation/path ở file tools hoặc interception đã có của repo nền.

**Coupling:** các helper đơn lẻ tương đối nhỏ; cả `FilesystemMiddleware` gắn với LangChain tools/messages, ToolRuntime, backend và interrupt machinery. Không cần đưa LangGraph lên làm owner vòng đời chỉ để lấy rule matching.

**Giới hạn:** mode `interrupt` liên quan human approval của framework, không phải thứ nên mặc định dùng khi orchestrator đã cấp quyền. Filesystem execute permissions vẫn có hạn chế đã ghi ở Pass 2.

Phân loại: `ADAPT_THINLY` cho evaluator/helper chọn lọc; `PATTERN_ONLY` cho importing nguyên middleware vào agent loop OpenHands.

### 4.3. Pydantic ToolGuardrail: hữu ích nhưng không phải donor đầu tiên cho OpenHands

Module: [guardrails/_tool_guardrail.py](../repos/pydantic-ai-harness/pydantic_ai_harness/guardrails/_tool_guardrail.py).

Nó kiểm tra hai phía của tool call: validated arguments trước execution và result trước khi trả model. Có block, retry, approval và các verdict khác.

Tuy nhiên implementation gắn với `AbstractCapability`, `RunContext`, `WrapToolExecuteHandler`, `SkipToolExecution`, `ModelRetry`, `ApprovalRequired` và deferred approval flow của Pydantic AI. Lấy nguyên class sẽ kéo thêm một bộ semantics execution mà OpenHands đã có phần tương ứng.

**Quyết định nghiên cứu:** ưu tiên interception OpenHands đã có và các evaluator phù hợp. Không đề xuất kéo toàn bộ guardrail framework chỉ để có before-tool check. Donor này phù hợp để đối chiếu contract hoặc lấy phần kiểm tra thuần nếu một nhu cầu cụ thể chứng minh cần.

Phân loại: `PATTERN_ONLY` cho toàn capability trong hướng OpenHands nền. Không phải `REJECT` đối với tính hữu ích của Pydantic AI nói chung.

### 4.4. Microsoft DockerShellTool: donor quan trọng bị đánh giá chưa đủ ở Pass 2

Module: [shell/_docker.py](../repos/agent-framework/python/packages/tools/agent_framework_tools/shell/_docker.py).

Đây là implementation shell trong container thực sự, không chỉ shell regex policy. Source dựng Docker command với:

- network mode, mặc định không có network;
- user không đặc quyền;
- root filesystem read-only có thể cấu hình;
- workspace bind mount read-only hoặc read/write;
- drop capabilities và no-new-privileges;
- memory và process-count limits;
- environment truyền tường minh;
- persistent hoặc stateless execution;
- start/close và cleanup container.

`host_workdir` do caller chọn. Có thể cùng trỏ một workspace nếu orchestrator muốn; không áp đặt mỗi Agent Session phải có worktree riêng. Những mặc định hạn chế có constructor parameters để caller thay đổi trong phạm vi implementation hỗ trợ.

**Điểm ghép:** backend của tool thực thi shell; không thay Agent Server, session registry, provider hoặc orchestration.

**Coupling:** class có `run/start/close` có thể gọi mà không cần chạy Microsoft Agent. Nhưng module import `agent_framework` cho tool wrapper/telemetry, và package `agent-framework-tools` phụ thuộc `agent-framework-core>=1.13.0,<2` cùng `psutil>=5.9`. Dependency framework không tự có nghĩa framework đó phải sở hữu agent loop; vẫn phải cân nhắc chi phí package so với chuyển phần executor chọn lọc.

**Giới hạn:** chỉ shell chạy qua DockerShellTool được container bảo vệ. File editor, browser, skill renderer, hooks và custom tools chạy trên host không tự nằm trong đó. Network mode không cung cấp ngay mọi domain/API-level permission. Không có một `interrupt()` drop-in khớp OpenHands, và timeout cleanup không bao phủ mọi external cancellation.

Phân loại: `ADAPT_THINLY` cho shell backend dùng class qua adapter; không gọi là full Agent Session sandbox hoàn chỉnh. Đây là donor có giá trị thực tế, không phải lý do tự viết Docker lifecycle từ đầu.

### 4.5. Sandbox-runtime: lựa chọn bổ sung khi cần OS policy chi tiết

Đã kiểm tra source công khai của [sandbox-runtime](https://raw.githubusercontent.com/anthropic-experimental/sandbox-runtime/main/src/sandbox/sandbox-manager.ts). Nó có filesystem/network restrictions, proxy và credential-handling mechanisms. Manager có state ở mức process/module; không thể giả định một manager singleton chứa đồng thời nhiều profile độc lập.

[Request filter](https://raw.githubusercontent.com/anthropic-experimental/sandbox-runtime/main/src/sandbox/request-filter.ts) có quyết định lọc request, nhưng khả năng thấy HTTP semantics phụ thuộc loại traffic và cấu hình TLS/proxy. Không được biến domain allowlist thành lời hứa kiểm soát mọi API method.

[Package metadata](https://raw.githubusercontent.com/anthropic-experimental/sandbox-runtime/main/package.json) ghi Apache-2.0 và có dependency Node/TypeScript. Đây không phải Python module có thể copy một file vào OpenHands. Nếu dùng, hợp lý hơn là giữ component được đóng gói thay vì chép lại internals sandbox.

Phân loại: `ADAPT_THINLY` cho tích hợp một execution boundary cụ thể; chưa chứng minh coverage toàn bộ session. [README](https://raw.githubusercontent.com/anthropic-experimental/sandbox-runtime/main/README.md) mô tả Windows là alpha; không kết luận native-Windows per-session profile composition đã đủ.

Không cần chọn đồng thời DockerShellTool và sandbox-runtime để cùng sở hữu một shell execution. Đây là các lựa chọn donor cho những mức policy khác nhau, chưa phải stack được chốt.

## 5. Root-only dispatch: phần nào lấy được, phần nào là điều kiện tích hợp

Không tìm thấy trong các local donor đã kiểm tra một module “chỉ Claude/Codex được tạo Agent Session, worker không được tạo” có thể cắm nguyên vào OpenHands. Điều này không có nghĩa cần tự xây authentication.

Các thành phần cần thiết đã có:

| Phần | Owner/donor có sẵn |
|---|---|
| Xác thực control API | OpenHands `check_session_api_key` |
| Loại server key khỏi terminal environment | OpenHands `sanitized_env` |
| Shell không được đọc host process/files tùy ý | Container execution của Microsoft hoặc OS sandbox phù hợp |
| Giới hạn direct tool invocation | OpenHands tool map và PreToolUse interception |
| Filesystem tool không đọc control credentials | File access donor và boundary filesystem thực sự |
| Skill không chạy code ngầm với quyền server | Content/runner separation ở Section 6, được nối vào execution boundary |

**Điểm thiếu còn lại là bằng chứng coverage của cách ghép, không phải thiếu primitive xác thực.** Worker không được giữ hoặc khôi phục quyền dispatcher qua bất kỳ đường execution được cấp nào. Nếu chỉ sandbox shell nhưng để skill subprocess chạy với quyền server thì vẫn chưa đáp ứng.

Quyền execution rộng vẫn có thể giữ; ngoại lệ quyền dispatcher đã nằm trong yêu cầu của người dùng. Quyền administrator trên chính service/host kiểm soát không thể đồng thời bị ràng buộc bởi một lệnh cấm gọi service đó.

Không nên đưa vào một framework RBAC/multi-user hoặc identity platform chỉ vì thiếu role trên API key. Nếu worker không cần control API thì nó không cần worker-role key. Existing server authentication + execution boundary là hướng reuse cần đánh giá.

Nguồn chính: [dependencies.py](../repos/software-agent-sdk/openhands-agent-server/openhands/agent_server/dependencies.py), [command.py](../repos/software-agent-sdk/openhands-sdk/openhands/sdk/utils/command.py), [DockerShellTool](../repos/agent-framework/python/packages/tools/agent_framework_tools/shell/_docker.py).

Phân loại: `REUSE_WITH_CONFIGURATION` cho authentication; `ADAPT_THINLY` cho các boundary cụ thể đã có; toàn bộ root-only guarantee vẫn **chưa được xác nhận ở mức composition**. Không suy ra `GAP_REMAINS` của toàn hệ sinh thái OSS chỉ từ điều này.

## 6. Donor cho skill behavior không tự mở rộng quyền

### 6.1. Microsoft: nội dung skill và script runner tách biệt

Module: [core/agent_framework/_skills.py](../repos/agent-framework/python/packages/core/agent_framework/_skills.py).

Những phần đáng lấy:

- `FileSkill.get_content()` trả nội dung và mô tả resources/scripts; việc đọc nội dung không tự chạy file script.
- `FileSkillScript` giữ identity/path của script và chuyển execution cho runner.
- `SkillScriptRunner` là protocol nhận skill, script, arguments.
- Không có runner phù hợp thì file script không được mặc nhiên thực thi.
- Metadata, resource và script discovery vẫn giữ được; không cần làm skill thành chuỗi prompt mất hết tài nguyên.

**Giới hạn quan trọng:** code-defined `InlineSkillScript` của Microsoft vẫn chạy in-process. Vì vậy không thể lấy cả module rồi quảng bá là mọi loại skill đã được sandbox. File-script runner cũng không tự là sandbox; nó là điểm nối đến executor được cấp quyền.

**Khả năng ghép:** nhóm data model/content/script separation là donor hợp lý; toàn bộ `_skills.py` còn gắn với ContextProvider, FunctionTool, telemetry, MCP/archive sources và kiểu Microsoft. Import toàn skill provider không phải thay thế mỏng cho OpenHands skills.

Phân loại: `ADAPT_THINLY` cho phần content/file-script interface được chọn lọc; `PATTERN_ONLY` cho toàn provider nếu chưa có nhu cầu lấy những source khác.

### 6.2. Pydantic Harness: passive loader nhỏ hơn

Module: [skills/_loader.py](../repos/pydantic-ai-harness/pydantic_ai_harness/skills/_loader.py).

Loader dùng stdlib, Pydantic và YAML parsing; không có subprocess thực thi body. Nó trả tên, mô tả, body và danh sách behavioral frontmatter bị bỏ qua.

Đây là donor nhỏ cho việc nạp/kiểm tra metadata một cách tường minh. Tuy nhiên không giữ nguyên toàn bộ richness của OpenHands skill resources và dynamic invocation. Dùng nó nguyên trạng làm replacement sẽ thay đổi semantics; không đủ lý do bỏ những capability upstream hữu ích.

Phân loại: `ADAPT_THINLY` cho parsing/metadata diagnostics; không chọn nó để tự động thay toàn bộ OpenHands skill loader.

### 6.3. Khoảng thích nghi thực sự

OpenHands gọi renderer có inline shell execution ở cả `Skill.render_content` và `InvokeSkillExecutor.__call__`. Chỉ thay loader, override một method, hoặc chặn một tool name không bao phủ cả hai đường.

Không tìm thấy donor drop-in giữ nguyên toàn bộ cú pháp dynamic command của OpenHands/Claude đồng thời đưa execution vào profile của OpenHands. Donor Microsoft cung cấp cách tách rõ content khỏi script execution; donor sandbox/shell cung cấp boundary. Đó là nguồn implementation để thích nghi, không phải bằng chứng tích hợp đã sẵn sàng.

Hướng reuse phải giữ scripts, references/resources và dynamic behavior khi được cấp quyền; không mặc định vô hiệu hóa các tính năng đó chỉ để né enforcement.

Nguồn phía OpenHands: [skill.py](../repos/software-agent-sdk/openhands-sdk/openhands/sdk/skills/skill.py), `render_content`; [invoke_skill.py](../repos/software-agent-sdk/openhands-sdk/openhands/sdk/tool/builtins/invoke_skill.py); [execute.py](../repos/software-agent-sdk/openhands-sdk/openhands/sdk/skills/execute.py).

## 7. Donor cho cancellation và independent control

### 7.1. Deep Agents đã xử lý đúng loại lỗi thread-pool shutdown

Trong [FilesystemMiddleware](../repos/deepagents/libs/deepagents/deepagents/middleware/filesystem.py), glob sync có:

- executor được giữ ở instance thay vì tạo context manager theo từng call;
- `BoundedSemaphore` giới hạn công việc còn thực sự chạy;
- không nhận thêm công việc khi các worker timeout vẫn chưa kết thúc;
- phân biệt hết thời gian chờ với lỗi `TimeoutError` từ backend;
- trả timeout mà không thoát một `with ThreadPoolExecutor` vốn sẽ đợi worker kết thúc;
- trả slot khi worker thực sự kết thúc, hoặc khi task chưa chạy được cancel thành công.

Source giải thích trực tiếp rằng per-call context manager sẽ gọi shutdown có chờ và vô hiệu hóa timeout. Đây là cùng loại vấn đề được phát hiện trong async batch executor của OpenHands.

Test upstream [test_permissions.py](../repos/deepagents/libs/deepagents/tests/unit_tests/test_permissions.py), `test_sync_glob_rejects_when_timed_out_workers_are_saturated`, kiểm tra các worker timeout vẫn chiếm slot và lời gọi tiếp theo bị từ chối thay vì xếp hàng vô hạn.

**Điểm ghép:** phần quản lý lifetime/admission/wait của executor OpenHands. OpenHands vẫn phải giữ event ordering, resource locks, cancellation token và synthetic observations của mình.

**Giới hạn:** đây là implementation cho sync glob, không phải generic async batch executor drop-in. Không thể chép nguyên `concurrent.futures.wait` vào event loop. Nó cũng không giết thread còn chạy. Những khác biệt đó giới hạn phần có thể port, không làm donor mất giá trị.

Phân loại: `ADAPT_THINLY` cho logic executor chọn lọc; `PATTERN_ONLY` cho toàn glob tool/middleware. Đủ bằng chứng không cần invent cách xử lý lỗi này từ đầu.

### 7.2. Pydantic AI và AnyIO cung cấp primitive bổ sung

Module: [Pydantic AI _utils.py](../repos/pydantic-ai/pydantic_ai_slim/pydantic_ai/_utils.py), `using_thread_executor`, `abandon_threads_on_cancel`, `run_in_executor`.

Source có custom executor, giữ context variables và lựa chọn bỏ chờ thread khi cancellation xảy ra trong phạm vi phù hợp. `abandon_threads_on_cancel` được sử dụng quanh lời gọi có deadline; không phải default mọi tool tự bị hard-kill.

Implementation dựa trên primitive hiện có của AnyIO. [AnyIO docs](https://anyio.readthedocs.io/en/stable/threads.html) xác nhận `abandon_on_cancel=True` cho phép caller bị cancel nhưng thread vẫn chạy, kết quả bị bỏ qua. Giới hạn concurrency và công việc thực sự còn sống phải được xem riêng; không coi abandon là chấm dứt effect.

Không cần import toàn Pydantic AI chỉ để lấy primitive AnyIO mà OpenHands đã dùng. `_utils.py` là internal module còn chứa graph/message/error dependencies; nó hữu ích để đối chiếu cách dùng, không phải package executor độc lập.

Phân loại: `REUSE_WITH_CONFIGURATION` cho AnyIO primitive; `PATTERN_ONLY` hoặc port chọn lọc đối với wrapper Pydantic. Không thay session history/continuation của OpenHands bằng `AgentRun` Pydantic.

### 7.3. Microsoft có process-tree termination thực sự

Module: [shell/_killtree.py](../repos/agent-framework/python/packages/tools/agent_framework_tools/shell/_killtree.py).

`kill_process_tree` dùng `psutil` để lấy descendants, terminate rồi kill những process còn sống sau grace period. Có fallback theo platform; helper là async và tự mô tả best-effort.

Đây là donor nhỏ, chủ yếu stdlib + psutil, không cần Microsoft agent loop. Tuy nhiên nhận `asyncio.subprocess.Process`, còn OpenHands terminal có những backend/process handle khác. Không thể gọi vào tmux hoặc Popen như thể cùng contract. Cũng không được quảng bá snapshot process tree thành bảo đảm chống mọi daemonization/race.

Quan trọng hơn, code shell của Microsoft hiện bắt `TimeoutError` ở các đường đã kiểm tra, nhưng không có cleanup tương đương cho mọi external `CancelledError`. Protocol nói cancellation-safe không đủ; source chưa cho phép coi cả executor là lời giải drop-in cho interrupt.

Nguồn: [_executor.py](../repos/agent-framework/python/packages/tools/agent_framework_tools/shell/_executor.py), [_session.py](../repos/agent-framework/python/packages/tools/agent_framework_tools/shell/_session.py), [_docker.py](../repos/agent-framework/python/packages/tools/agent_framework_tools/shell/_docker.py), [test_shell_killtree.py](../repos/agent-framework/python/packages/tools/tests/test_shell_killtree.py).

Phân loại: `ADAPT_THINLY` cho helper process-tree/timeout cleanup phù hợp; không thay nguyên terminal để rồi mất tmux/persistent terminal semantics mà chưa có lý do.

### 7.4. Pydantic Harness có donor cleanup browser sát nhu cầu

Module: [browser_use/_toolset.py](../repos/pydantic-ai-harness/pydantic_ai_harness/browser_use/_toolset.py).

Các phần cụ thể:

- `_kill(session)`: shield cleanup trước cancellation, giới hạn thời gian teardown, không để lỗi teardown che lỗi/cancel gốc.
- `_close_session`: giữ lại browser session nếu cleanup thất bại.
- `_retry_pending_cleanup`: thử lại các session chưa cleanup xong.
- `_run_in_fresh_session`: cleanup trong `finally`.
- `_run_in_shared_session`: khi lỗi hoặc cancel thì bỏ session không còn chắc trạng thái và cleanup; lần sau tạo lại.

Test upstream bao phủ cancel, teardown lỗi, teardown timeout và retry cleanup: [test_browser_use.py](../repos/pydantic-ai-harness/tests/browser_use/test_browser_use.py), gồm `test_session_killed_when_the_call_is_cancelled`, `test_a_teardown_timeout_is_reported`, `test_a_failed_teardown_is_retried_before_the_next_call`.

Đây là donor đặc biệt phù hợp vì OpenHands cũng dùng browser-use và đã có cleanup gọi `_close_all_sessions`/`session.kill` ở một số đường. Phần còn thiếu không phải browser lifecycle từ con số không; là liên kết active-operation cancellation và cleanup có giới hạn vào interrupt contract.

**Không lấy toàn `BrowserUseToolset` chỉ để sửa cleanup.** Nó có autonomous browser-agent/model path riêng, Pydantic toolset và model conversion. OpenHands hiện có browser action tools; đổi cả lớp này sẽ thay capability và thêm ownership không cần thiết.

**Coupling cần lưu ý:** OpenHands browser executor chạy qua portal/background loop, donor dùng AnyIO scope. Shield semantics phải phù hợp đúng cancellation mechanism được nối vào, không suy ra rằng AnyIO shield tự xử lý mọi kiểu `Task.cancel` từ bên ngoài. Donor pin browser-use extra `>=0.13.6,<0.14`; OpenHands khai báo floor rộng hơn. Đây là API/version compatibility cần giữ khi port, không có kết quả resolver/test tích hợp trong nghiên cứu này.

Phân loại: `ADAPT_THINLY` cho cleanup protocol/helper chọn lọc; `PATTERN_ONLY` cho toàn browser-agent toolset trong vai trò sửa OpenHands.

### 7.5. Pydantic shell là donor timeout, không phải bảo đảm interrupt toàn diện

Module: [shell/_toolset.py](../repos/pydantic-ai-harness/pydantic_ai_harness/shell/_toolset.py).

Có process group, TERM/KILL escalation, bounded output drain và cleanup trong finally. Nhưng đường kill-group tường minh gắn với timeout; không được suy luận rằng external cancel luôn dừng mọi descendant. Một số primitive dùng POSIX process groups.

Vì Microsoft đã có helper cross-platform nhỏ, Pydantic shell không phải donor ưu tiên để thay toàn bộ terminal OpenHands. Có thể lấy cách giới hạn drain/cleanup khi cần đối chiếu một failure cụ thể.

Phân loại: `PATTERN_ONLY` cho thay terminal; `ADAPT_THINLY` cho helper cleanup riêng nếu contract phù hợp.

## 8. Dependency, license và chi phí ghép

| Donor | Dependency/coupling đọc từ source | Khả năng lấy chọn lọc |
|---|---|---|
| OpenHands hooks | Đã thuộc SDK, action/state/events của repo nền | Ưu tiên reuse trước khi thêm guardrail framework |
| Microsoft `_killtree.py` | Stdlib + psutil; asyncio process handle | Nhỏ; cần adapter process contract |
| Microsoft DockerShellTool | Package phụ thuộc agent-framework-core + psutil; Docker CLI/runtime | Có thể gọi executor mà không chạy Microsoft agent loop; package cost hoặc selective port cần cân nhắc |
| Pydantic filesystem | FunctionToolset, ModelRetry; stdlib I/O | Lấy nhóm access/containment logic, map lỗi; không lấy một helper rồi bỏ coverage khác |
| Deep Agents permission helpers | `wcmatch`, POSIX paths, ordered rules | Nhỏ ở evaluator; middleware đầy đủ gắn LangChain/LangGraph |
| Deep Agents glob executor logic | Futures/threading/contextvars; kết quả gắn ToolMessage/backend | Port phần execution lifetime; giữ event semantics của OpenHands |
| Pydantic executor wrappers | AnyIO + contextvars; `_utils.py` còn phụ thuộc graph/core | Ưu tiên primitive AnyIO hiện có, không import toàn internal module |
| Pydantic browser cleanup | AnyIO + BrowserSession; class ngoài gắn model/toolset | Lấy cleanup helper/protocol, không lấy autonomous browser agent |
| Microsoft skill file/script types | Module lớn gắn ContextProvider/FunctionTool/telemetry | Tách nhóm content/runner behavior; không import toàn provider nếu không cần |
| Pydantic skill loader | Stdlib, Pydantic, PyYAML | Nhỏ, nhưng không giữ đủ mọi resource/dynamic semantics nếu thay nguyên loader |
| Sandbox-runtime | Node package và OS-specific executables/proxies | Dùng component đóng gói tại execution boundary, không chép internals sang Python |

Các repo local OpenHands, Pydantic AI, Pydantic Harness, Deep Agents và Microsoft Agent Framework đều có LICENSE MIT ở phạm vi root; package tools/core của Microsoft và package Deep Agents cũng có LICENSE riêng đã tìm thấy. Việc phân phối lại phần source phải giữ notice/license tương ứng. Đây là ghi nhận từ file license, không phải kết luận rằng mọi dependency hoặc tài sản bên trong đều cùng một license.

Nguồn: [OpenHands LICENSE](../repos/software-agent-sdk/LICENSE), [Pydantic Harness LICENSE](../repos/pydantic-ai-harness/LICENSE), [Pydantic AI LICENSE](../repos/pydantic-ai/LICENSE), [Deep Agents LICENSE](../repos/deepagents/LICENSE), [Microsoft tools LICENSE](../repos/agent-framework/python/packages/tools/LICENSE), [Microsoft core LICENSE](../repos/agent-framework/python/packages/core/LICENSE).

Không có integration dependency solve trong lần này. Metadata cho biết Pydantic Harness là alpha, Microsoft tools là beta và Deep Agents là beta tại snapshot; các mức này không phủ nhận capability nhưng làm tăng chi phí theo dõi API khi port internal modules.

Không lấy core Claude Code từ clone như OSS donor: license và repository contents không cung cấp cùng điều kiện reuse core runtime như các repo MIT trên.

## 9. Ma trận chọn donor sau khi nghiên cứu lại

| Nhu cầu | Repo nền giữ gì | Donor ưu tiên | Cách reuse được bằng chứng hỗ trợ | Phân loại | Điều chưa được xác nhận |
|---|---|---|---|---|---|
| Agent loop/session/history | OpenHands giữ toàn bộ | Không cần donor | Reuse hiện có | `REUSE_DIRECT` | Không có gap mới trong phạm vi lần này |
| Before-tool enforcement | Action/state/rejection | OpenHands hooks | Cấu hình/interception hiện có | `REUSE_WITH_CONFIGURATION` | Failure semantics của policy binding |
| Filesystem confinement | Workspace + observations | Pydantic FileSystemToolset | Access/containment logic hoặc tool adapter | `ADAPT_THINLY` | Không bao phủ shell/custom host code |
| Operation/path rules | Tool invocation boundary | Deep Agents permission helpers | Chuyển evaluator và kiểm tra liên quan | `ADAPT_THINLY` | Rule precedence/path normalization khi ghép |
| Shell sandbox | Session lifecycle | Microsoft DockerShellTool | Executor-level adapter | `ADAPT_THINLY` | Không tự sandbox các tool khác; cancel contract |
| Fine filesystem/network process policy | Session lifecycle | Sandbox-runtime | Component boundary | `ADAPT_THINLY` | Coverage toàn session và nhiều profile độc lập |
| Dispatcher API authentication | Agent Server | OpenHands key authentication | Cấu hình hiện có | `REUSE_WITH_CONFIGURATION` | Worker không tiếp cận quyền control qua execution khác |
| Skill content không ngầm thực thi | Skills/resources hiện có | Microsoft content/runner separation; Pydantic metadata loader | Chuyển phần interface/parser phù hợp | `ADAPT_THINLY` | Dynamic command equivalence và mọi rendering path |
| Không block event loop khi chờ thread | Event order/resource locks | Deep Agents bounded executor logic + AnyIO | Thích nghi phần chờ/admission/executor lifetime | `ADAPT_THINLY` | Không giết thread; saturation/late effects vẫn cần contract |
| Dừng process tree | Terminal semantics | Microsoft killtree | Helper có sẵn + handle adaptation | `ADAPT_THINLY` | Backend khác asyncio process, descendant races |
| Browser cancellation cleanup | Browser action API | Pydantic browser-use cleanup | Helper/protocol chọn lọc | `ADAPT_THINLY` | Portal cancellation propagation, version semantics |
| Sync/async bridge | OpenHands tool interface | OpenHands AsyncExecutor/AnyIO | Reuse primitive | `REUSE_DIRECT` | Operation future phải được nối đúng interrupt |
| ToolGuardrail nguyên class | Không thêm agent loop | Pydantic ToolGuardrail | Đối chiếu contract | `PATTERN_ONLY` | Quá gắn Pydantic flow để coi là drop-in |
| Microsoft background-session owner | OpenHands giữ owner | Không lấy | Không thay session manager vì cần shell helper | `REJECT` | Handles in-memory không cung cấp phần lợi ích cần đổi owner |
| Deep Agents async lifecycle | OpenHands giữ owner | Không lấy cho các gap này | Giữ làm tham khảo | `PATTERN_ONLY` | Middleware client không chứng minh server effect cancellation |

Ma trận không yêu cầu nhập tất cả donor vào một stack. Một nhu cầu có nhiều donor nghĩa là có lựa chọn tái sử dụng; không có nghĩa để cả hai cùng sở hữu policy hoặc execution.

## 10. Điều chỉnh kết luận blocker và phần còn lại

### Những kết luận được sửa

1. **“OpenHands chỉ có confirmation/tool filtering” là chưa đầy đủ.** Đã có PreToolUse action rejection. Phần cần hoàn thiện là policy binding và coverage, không phải tự xây interception framework.
2. **“Không có hướng OSS cho thread cleanup” không được bằng chứng hỗ trợ.** Deep Agents có implementation giải quyết cùng loại per-call shutdown trap; AnyIO/Pydantic có primitive liên quan.
3. **“Browser interrupt phải tự viết lại browser manager” không đúng.** Pydantic Harness có cleanup trên cancel, timeout và retry; OpenHands cũng đã có cleanup/portal primitives để giữ lại.
4. **“Repo khác chỉ là tham khảo vì không có session server” là đánh giá sai mức component.** Microsoft không phù hợp làm session owner nhưng vẫn có Docker executor/process helper đáng lấy.
5. **Không nên coi việc thiếu một profile engine thống nhất là gap tự thân.** Yêu cầu cho phép enforcement ở nhiều boundary, miễn profile của orchestrator vẫn là quyền tối đa có hiệu lực.

### Điều vẫn chưa được chứng minh

- Một cách ghép cụ thể bảo đảm mọi executable path, gồm shell, skill render, hooks, browser, MCP/custom tools và credential exposure, đều ở đúng authority boundary.
- Dynamic skill syntax được giữ đầy đủ mà không quay lại thực thi với quyền server ngoài profile.
- Async donor executor nhận cancellation đúng từ sync/threaded interface của OpenHands, giữ event ordering và không mở lại resource đã đóng.
- Hành vi root-only dispatch với broad worker authority đã được xác nhận cho toàn composition.

Đây là các câu hỏi tương thích và ownership của integration có donor, không phải bằng chứng cần invent subsystem. Không bổ sung benchmark hoặc live 9Router vào danh sách này.

### Kết luận cho hướng làm việc tiếp theo

**Có đủ bằng chứng để tiếp tục hướng OpenHands làm repo nền và tái sử dụng chọn lọc từ các repo khác.** Chưa có cơ sở chốt mọi adapter là đơn giản, nhưng cũng không còn cơ sở dùng lỗi riêng của OpenHands để bác bỏ hướng tái sử dụng tổng thể.

Verdict `BLOCKED_BY_CONFIRMED_GAPS` trong Pass 2 phải được hiểu là đánh giá integration OpenHands nguyên trạng đã kiểm tra, không phải kết luận “không thể đáp ứng yêu cầu bằng OSS”. Nếu đánh giá khả năng tái sử dụng liên-repo, phần chưa hoàn tất hiện nằm ở bằng chứng về composition, không phải sự vắng mặt của các implementation nền tảng.

Tài liệu này không chốt final stack, không đưa sơ đồ module, không định nghĩa CLI/schema và không tạo implementation backlog. Không có production code hoặc upstream source nào được sửa trong lần nghiên cứu.
