# Agent Runtime — Quyết định trước khi dựng plan

Ngày: 2026-09-05. Nguồn: hỏi–đáp trực tiếp với người dùng.
Tài liệu nền: [Pass 1](RESEARCH_PASS_1.md), [Pass 2](RESEARCH_PASS_2.md), [OSS Gap Reassessment](RESEARCH_OSS_GAP_REASSESSMENT.md).

File này chỉ ghi quyết định đã chốt. Không phải plan, không phải architecture.

## Vòng 1

| Câu hỏi | Quyết định |
|---|---|
| Frozen requirements nằm ở đâu | Suy ra từ 3 báo cáo research là đủ; không cần file requirements riêng để duyệt |
| Nền tảng chạy | Runtime local, chạy được cả Linux và Windows. Không dùng máy chủ riêng/VPS |
| Orchestrator gọi vào bằng gì | MCP server. Ưu tiên phương án Claude Code và Codex dùng dễ nhất. Human không phải người dùng |
| Cách dùng OpenHands | Copy về `agent_runtime`, làm việc trực tiếp trên bản copy |

## Vòng 2

| Câu hỏi | Quyết định |
|---|---|
| Hình dạng process nền | Daemon local tự động bật: MCP lần đầu tự spawn agent-server nghe trên loopback, lần sau attach. Session sống tiếp khi client thoát |
| MCP transport | stdio, daemon tách riêng. MCP process là client mỏng gọi sang daemon |
| Phạm vi copy | Copy tất cả những gì cần thiết từ `repos/` — không giới hạn ở `software-agent-sdk`, gồm cả các donor repo khác |
| Quan hệ upstream | Hard fork, đổi namespace. Không merge upstream về sau |

## Hệ quả đã thấy từ các quyết định trên

- "Không server" không loại trừ daemon local; nó loại trừ máy chủ riêng. Discovery/steering/resume của agent-server được giữ.
- Hard fork + đổi namespace nghĩa là mọi `openhands.*` phải đổi tên, gồm cả entry point, config path và tên package trong `pyproject.toml`. Tên namespace mới chưa chốt.
- Copy từ nhiều repo nghĩa là các donor trong Reassessment §4/§6/§7 được vendor thành source, không chỉ tham chiếu.
- Chạy được cả Windows lẫn Linux nghĩa là các donor sandbox (Docker, systemd, sandbox-runtime) không thể là điều kiện bắt buộc để chạy.

## Vòng 3

| Câu hỏi | Quyết định |
|---|---|
| Enforce permission profile ở đâu | Tool boundary + PreToolUse hook, chạy được mọi OS. Sandbox/container là lớp tùy chọn khi máy có Docker. Ghi rõ: không có sandbox thì không phải security boundary thật |
| Blocker ①: skill inline command | Giữ nguyên cú pháp, định tuyến lệnh qua cùng execution boundary đã bị profile ràng buộc. Không tắt tính năng |
| Blocker ②: root-only dispatch | Best-effort nhiều lớp: không cấp token/địa chỉ daemon cho worker, sanitize env, không cấp delegate tool, chặn sub-conversation. Ghi rõ caveat với worker có quyền admin |
| Donor từ repo khác | Port source sang namespace mới, giữ LICENSE/notice. Không thêm langchain/agent-framework/pydantic-ai làm dependency |

## Vòng 4

| Câu hỏi | Quyết định |
|---|---|
| Namespace mới | `agentrt` — `agentrt.sdk`, `agentrt.server`, `agentrt.tools` |
| Phạm vi copy | Copy nguyên, cắt phần thừa sau khi đã chạy được |
| Blocker ③ (cancellation chặn event loop) | Hoãn khỏi v1. Chỉ ghi lại rủi ro, không đụng parallel executor |
| Ngôn ngữ plan | Tiếng Anh |

## Vòng 5

| Câu hỏi | Quyết định |
|---|---|
| Hình dạng permission profile | Preset có tên + cho phép override từng chiều |
| Workspace mặc định | Thư mục do orchestrator chỉ định, chia sẻ giữa các session. Worktree/thư mục riêng chỉ khi được yêu cầu |
| Model/provider và API key | LLM profile có tên (tái dùng `profiles_router`), key nằm trong config của runtime. MCP chỉ truyền tên profile; key không đi qua Claude Code hay workspace của worker |
| Hạ tầng repo | `git init` với branch `main` trong `agent_runtime` TRƯỚC khi copy, commit bản copy nguyên gốc làm commit đầu |

## Vòng 6

| Câu hỏi | Quyết định |
|---|---|
| MCP tool surface | Mỗi thao tác một tool (~10 tool: dispatch, list, status, transcript, send, interrupt, stop, resume, result, artifacts) |
| Vòng đời daemon | Daemon sống mãi, không tự tắt. Session giữ vô thời hạn, chỉ mất khi bị xóa |
| Kiểm chứng sau khi đổi namespace | Cả hai: chạy test suite đã copy (trước và sau khi đổi tên) + smoke test end-to-end qua MCP |

## Vòng 7

| Câu hỏi | Quyết định |
|---|---|
| Tool mặc định của session | Terminal + file editor + task tracker. Browser và MCP chỉ bật khi session yêu cầu |
| Nguồn skills | Thư mục riêng của runtime + skill trong workspace. Không đọc `~/.claude/skills` |
| Sub-agent / đệ quy | Cho phép sub-agent chạy trong process như một tool bình thường. Nó không tạo Agent Session mới nên không vi phạm root-only dispatch |
| Scheduling | Có trong v1, ở mức tối thiểu |

Ghi chú kiểm chứng: `openhands-tools/openhands/tools/delegate/impl.py` tạo `LocalConversation` trong process, không gọi Agent Server, và có `_max_children`. Quyết định trên khớp với implementation.

## Vòng 8

| Câu hỏi | Quyết định |
|---|---|
| Sub-agent tự khai tools/skills/hooks trong file markdown | Sub-agent chỉ nhận tập con của profile cha (giao nhau). Khai báo vượt quá bị bỏ qua. Worker ghi file cũng không nâng được quyền |
| Scheduling tối thiểu | Cron chạy trong daemon, lịch và lịch sử run lưu file cạnh state. Không thêm database. Không catch-up |
| Daemon crash khi có session đang chạy | Để session ở trạng thái error kèm nguyên lịch sử; orchestrator đọc status rồi tự quyết resume. Không tự chạy lại |
| Định dạng transcript qua MCP | Mặc định trả N event gần nhất đã rút gọn thành text, kèm cursor để lấy tiếp hoặc lấy raw |

## Vòng 9

| Câu hỏi | Quyết định |
|---|---|
| Đường provider qua 9Router | Anthropic-compatible cho model Claude, OpenAI-compatible cho phần còn lại. Giữ đúng native protocol theo từng model |
| Cách xác định artifact | Agent tự khai file quan trọng trong kết quả cuối; tool `artifacts` trả danh sách đó kèm API đọc nội dung |
| Observability/cost trong v1 | Không thêm gì. Giữ nguyên như upstream |
| Nơi lưu state | Thư mục home theo OS: `~/.agentrt` trên Linux, `%LOCALAPPDATA%\agentrt` trên Windows. Tách khỏi workspace để worker không đọc được credential qua file tool |

Ghi chú kiểm chứng: `openhands-tools/.../terminal/terminal/windows_terminal.py` đã có sẵn; factory tự chọn PowerShell trên Windows và tmux trên Unix. Yêu cầu chạy hai OS không cần tự viết terminal backend.

Rủi ro đã ghi nhận từ vòng 9: chọn "không thêm observability" trong khi daemon tự spawn ngầm nghĩa là khi daemon không lên được sẽ không có gì để đọc. Plan sẽ nêu lại điểm này.

## Vòng 10

| Câu hỏi | Quyết định |
|---|---|
| Python tooling | Giữ uv và uv.lock của upstream |
| Payload của `dispatch` | Bắt buộc: task + workspace. Tùy chọn có mặc định: permission profile, llm profile, tools, skills, title |
| Trần session đồng thời | Không giới hạn |
| Definition of done cho v1 | Chạy trọn vòng đời thật từ Claude Code (dispatch → thoát → mở lại → list → transcript → send → interrupt → resume → result + artifacts), test suite sau đổi namespace còn xanh, VÀ chứng minh Codex dùng được cùng MCP server |

## Vòng 11

| Câu hỏi | Quyết định |
|---|---|
| Permission preset | Ba mức: `readonly` / `workspace` / `broad`. Mặc định là `workspace`. Cho phép override từng chiều |
| `stop` vs `interrupt` | `interrupt` = cắt để đổi hướng, gửi chỉ thị mới ngay. `stop` = dừng hẳn, giữ nguyên lịch sử, vẫn đọc transcript và resume được. Không thao tác nào xóa dữ liệu |
| ID session | Sinh mã ngắn dễ đọc (kiểu `a3f9c1`); mọi tool nhận cả mã ngắn lẫn UUID gốc |
| Ngữ nghĩa `send` | Trả về ngay, không chặn. Kết quả nói rõ chỉ thị đã xếp hàng và sẽ được đọc ở bước xử lý kế tiếp |

## Làm rõ: vì sao có daemon (không phải máy chủ)

Người dùng hỏi lại vì sơ đồ đúng là toàn bộ local trừ lời gọi provider. Làm rõ và giữ nguyên quyết định:

- "Agent Server" của OpenHands chỉ là một process nền trên `127.0.0.1`, không phải hosting hay dịch vụ mạng.
- MCP stdio process là process con của Claude Code, chết theo client. Agent đang chạy là state trong RAM (stream tới provider, phiên terminal, cancellation token), không đọc lại được từ đĩa.
- Vì vậy `list`/`send`/`interrupt` một session đang chạy bắt buộc phải với tay sang process đang giữ nó; điểm liên lạc đó chính là thứ bị gọi là "server".
- Daemon tồn tại chỉ vì yêu cầu "chạy nền, sống tiếp khi client thoát, mở lại vẫn lái được". Bỏ yêu cầu đó thì mô hình inline một process là đủ.

**Quyết định: giữ daemon local.** Hai process, đều trên máy người dùng.

## Vòng 12

| Câu hỏi | Quyết định |
|---|---|
| Chặn worker gọi daemon | Token lưu trong `~/.agentrt` (ngoài workspace), daemon chỉ nhận request có token, token bị lọc khỏi env của terminal. Loopback + token |
| MCP server cho worker | Có, orchestrator khai báo khi dispatch. Riêng agentrt MCP bị chặn cứng ở tầng daemon, không dựa vào quy ước |
| Tool bổ sung | Thêm `delete`, `schedule`, `profiles` — tổng cộng 13 tool |
| Cách chia phase | Chạy được sớm, siết dần: P1 copy + đổi namespace + test xanh → P2 daemon + MCP + dispatch/result end-to-end → P3 đủ vòng đời → P4 permission + root-only → P5 scheduling |

## Vòng 13

| Câu hỏi | Quyết định |
|---|---|
| Thông tin 9Router cho smoke test | Người dùng sẽ cung cấp base URL + model id + key khi tới bước P2. Plan để chỗ trống |
| Phạm vi test làm mốc đổi namespace | Toàn bộ test suite, gồm cả phần cần mạng/Docker |
| Bố cục git | Commit 1 = tài liệu (chuyển vào `docs/`), commit 2 = bản copy OpenHands nguyên gốc, chưa sửa gì |
| Bước tiếp | Viết `IMPLEMENTATION_PLAN.md` |

## Vòng 14 — soát lại và đổi quyết định

Người dùng yêu cầu rà lại. Bốn quyết định được đổi:

| Quyết định cũ | Quyết định mới | Lý do đổi |
|---|---|---|
| Không thêm observability (vòng 9) | Thêm log file của daemon trong `~/.agentrt` | Daemon tự spawn ngầm; không có log thì daemon chết là không đọc được gì |
| Hoãn blocker ③ (vòng 4) + không giới hạn session (vòng 10) | Làm cả hai: sửa executor theo donor Deep Agents VÀ thêm trần session cấu hình được | Hai quyết định cũ cộng lại khiến một tool treo có thể làm chậm điều khiển mọi session |
| Enforce bằng PreToolUse hook (vòng 3) | Enforce thẳng trong tool layer, bỏ hook khỏi đường policy | Hook upstream chỉ có kiểu `command`/`prompt`/`agent`; dùng hook nghĩa là mỗi tool call spawn một subprocess (trên Windows là PowerShell) |
| MCP-only, có scheduling tối thiểu (vòng 1, 7) | Thêm CLI tối thiểu dùng chung core với MCP; bỏ scheduling khỏi v1 | OS scheduler không gọi được vào MCP stdio. Có CLI thì cron của OS gọi thẳng CLI, xóa hẳn một phase và có luôn công cụ debug daemon |

Đã đo để bác một lo ngại: test suite upstream có 695 file, ~8.200 test function, chỉ 4 chỗ đánh dấu `network` và 14 file chạm Docker. Dùng toàn bộ suite làm mốc là khả thi; giữ nguyên quyết định vòng 13.

Ghi chú kéo theo: bỏ hook khỏi đường policy không có nghĩa hook biến mất khỏi fork. Định nghĩa sub-agent vẫn có field `hooks` kiểu `command`, nên hook khai trong workspace, sub-agent và skill vẫn phải bị loại bỏ.

## Vòng 15 — soát lại lần hai

| Câu hỏi | Quyết định |
|---|---|
| Thứ tự cắt module vs đổi namespace | Giữ nguyên: copy nguyên rồi đổi tên. Test suite đầy đủ mới là mốc đúng nghĩa; đổi tên là thao tác cơ học nên thêm module chỉ tốn thời gian chạy test |
| Codex cho definition of done | Đã có sẵn, thử được ngay. Giữ nguyên tiêu chí verify cả hai orchestrator |
| Hai session cùng một workspace | Không làm gì. Chia sẻ là cố ý; điều phối là việc của orchestrator. Đã ghi vào bảng rủi ro của plan |

## Vòng 16

| Câu hỏi | Quyết định |
|---|---|
| Donor repo khác | Giữ `repos/` làm tham chiếu chỉ-đọc. Chỉ vendor `software-agent-sdk`. Đến P4 mới port đoạn cần dùng, kèm ghi chú nguồn và LICENSE. Điều kiện: không xóa `repos/` |
| Bước tiếp | Bắt đầu P1. Git init và docs đã do người dùng làm xong |

## Vòng 17 — quyết định cho việc đổi tên (sau khi có kết quả khảo sát)

| Câu hỏi | Quyết định |
|---|---|
| Phạm vi đổi tên | Chỉ phần runtime thực thi: bốn package, `tests/`, `pyproject.toml`, `MANIFEST.in`, `Makefile`, `.openhands/` của chính repo. KHÔNG đụng `clients/typescript`, `.github/`, `examples/`, `scripts/`, README/AGENTS/CONTRIBUTING/DEVELOPMENT, LICENSE |
| `.openhands` và `OH_` | Đổi cả hai ngay trong P1. Không cần fallback vì máy không có dữ liệu thật |
| Giá trị ghi xuống đĩa / lên dây | Đổi hết cho nhất quán: `agent_kind`, `OpenHandsCloudWorkspace`, header `X-OpenHands-*`, telemetry `source`, enum `openhands_managed` |

Danh sách phải giữ nguyên, không được replace: tiền tố model LiteLLM `"openhands/"` và bảng `VERIFIED_MODELS["openhands"]`, mọi URL `openhands.dev` và `all-hands.dev`, `github.com/OpenHands`, `ghcr.io/openhands/agent-server`, `"originator": "openhands"` gửi cho OpenAI, dòng `Co-authored-by: openhands`, và cả hai file LICENSE.

## Vòng 18-20 — quyết định phát sinh trong lúc chạy P1

| Câu hỏi | Quyết định |
|---|---|
| litellm 1.93.0 không có wheel Windows | Nâng pin lên 1.93.1; baseline lấy từ commit đã sửa pin, không phải bản copy nguyên gốc |
| Tầng 4 (giá trị wire) làm dở dang, ~200 fail | Revert tầng 4, giữ tầng 1-3. `agent_kind`, `OpenHandsCloudWorkspace` và vài enum giữ tên cũ như định danh lịch sử nội bộ |
| Test kiểm tra `.github/scripts` fail vĩnh viễn | Xóa 7 module trong `tests/cross/` |
| Hộp thoại "Pick an app" liên tục khi chạy test | Xóa `packages/.openhands/` — hook dev của upstream trỏ tới file `.sh`, trên Windows `shell=True` rơi về ShellExecute và mở hộp thoại chặn |
| Nơi đặt credential 9Router | `.env` ở gốc repo (đã gitignore) làm nguồn lúc phát triển; daemon đọc rồi đưa vào LLM profile trong state dir. Hai base URL cho hai protocol |
| Test ghi vào state dir thật của người dùng | Mọi lần chạy pytest từ nay phải đặt `AGENTRT_PERSISTENCE_DIR` trỏ vào thư mục tạm |

## Vòng 21 — kết quả đo 9Router thật

Endpoint: một URL duy nhất, tương thích cả hai protocol. Sẽ không có model Anthropic; tính tương thích chỉ là tương thích.

**Quyết định vòng 9 bị bãi bỏ.** Không còn hai đường protocol, chỉ một base URL. Biến `AGENTRT_9ROUTER_ANTHROPIC_BASE_URL` và `AGENTRT_DEFAULT_ANTHROPIC_MODEL` đã bỏ.

Model mặc định: `ds/deepseek-v4-flash`, đã đo đủ bốn khả năng.

| Model | text sync | text stream | tool sync | tool stream | usage |
|---|---|---|---|---|---|
| `ds/deepseek-v4-flash` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `ds/deepseek-v4-flash-vision-exp` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `ds/deepseek-v4-pro-max` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `ds/deepseek-chat` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `ds/deepseek-v4-pro` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `cf/@cf/zai-org/glm-4.7-flash` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `cf/@cf/qwen/qwen2.5-coder-32b-instruct` | ✓ | ✓ | ✗ | ✗ | ✓ |
| `cf/@cf/moonshotai/kimi-k2.6` | 500 | 500 | 500 | 500 | — |
| `free-model` (alias) | ✗ | ✓ | ✓ | ✗ | ✗ |

**Không dùng `free-model`.** Nó trả text chỉ khi stream và tool call chỉ khi không stream, nên không chế độ nào phục vụ được một lượt agent. Nó cũng xoay vòng giữa các model thật (`muse-spark-1.2` rồi `1.3` giữa hai lần gọi) và không trả usage.

Ghi chú kỹ thuật cho P2: `GET /models` qua `urllib` bị Cloudflare chặn (mã 1010), nhưng SDK OpenAI đi qua được. LiteLLM cảnh báo "model isn't mapped yet" cho mọi model id của 9Router — chỉ ảnh hưởng tính chi phí, không ảnh hưởng chức năng.

## Vòng 22 — chế độ chạy tự động

| Câu hỏi | Quyết định |
|---|---|
| Mức tự chủ | Tự quyết hết, chỉ báo khi xong. Không dừng hỏi giữa chừng |
| Ngân sách model | Thoải mái, trần 1 USD tiền DeepSeek |
| Đích của "hoàn thiện" | Tới khi người dùng dùng được hằng ngày — không phải chỉ tick hết P2-P4 |
| Codex | Hoãn. Tiêu chí xong của P3 chỉ còn Claude Code; MCP vẫn giữ đúng chuẩn để cắm Codex sau không phải thiết kế lại |
| Quyền hệ thống | Được: dọn rác test trong state dir, cài dependency Python, spawn/tắt daemon local, sửa cấu hình MCP của Claude Code |
| Giới hạn | Không đụng vào RAG hoặc các công việc khác đang chạy |
| Ba test flaky | Chấp nhận, ghi tài liệu, không sửa |

Bù cho việc không dừng hỏi: mọi phán đoán trong lúc chạy được ghi vào chính file này, và mọi sai lệch so với plan đi một commit riêng kèm lý do đầy đủ. `git log` là dấu vết kiểm tra.

Rủi ro đã nêu và người dùng chấp nhận: đây đúng là chế độ mà một sai lầm kiểu tầng 4 sẽ đi rất xa mới lộ ra.
