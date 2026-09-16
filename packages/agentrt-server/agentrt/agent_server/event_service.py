import asyncio
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

from pydantic import ValidationError

from agentrt.agent_server import config as server_config
from agentrt.agent_server.config import finalize_summary_enabled
from agentrt.agent_server.conversation_lease import (
    DEFAULT_LEASE_TTL_SECONDS,
    ConversationLease,
    ConversationOwnershipLostError,
)
from agentrt.agent_server.models import (
    AgentResponseResult,
    ConfirmationResponseRequest,
    ConversationErrorInfo,
    EventPage,
    EventSortOrder,
    StoredConversation,
)
from agentrt.agent_server.pub_sub import PubSub, Subscriber
from agentrt.agent_server.run_scope import (
    AgentResponseState,
    derive_result_state,
    iterations_remaining,
)
from agentrt.agent_server.server_details_router import update_last_execution_time
from agentrt.sdk import LLM, AgentBase, Event, Message, TextContent, get_logger
from agentrt.sdk.agent import ACPAgent
from agentrt.sdk.agent.acp_agent import ACTIVITY_SIGNAL_INTERVAL
from agentrt.sdk.agent.acp_file_credentials import (
    CODEX_AUTH_SECRET_NAME,
    is_valid_codex_auth,
)
from agentrt.sdk.conversation.base import BaseConversation
from agentrt.sdk.conversation.events_list_base import EventsListBase
from agentrt.sdk.conversation.exceptions import ConversationRunError
from agentrt.sdk.conversation.goal import (
    GoalController,
    GoalDone,
    GoalOutcome,
    GoalStatus,
    GoalStatusName,
    GoalStep,
    GoalVerdict,
)
from agentrt.sdk.conversation.goal.prompts import RESUME_PROMPT
from agentrt.sdk.conversation.impl.local_conversation import (
    ACP_INFLIGHT_PROMPT_USER_MESSAGE_ID,
    ACP_SUPERSEDE_INFLIGHT_PROMPT,
    LocalConversation,
)
from agentrt.sdk.conversation.persistence_const import BASE_STATE
from agentrt.sdk.conversation.response_utils import (
    get_agent_final_response,
    index_of_event,
)
from agentrt.sdk.conversation.secret_registry import SecretValue
from agentrt.sdk.conversation.state import (
    ConversationExecutionStatus,
    ConversationState,
)
from agentrt.sdk.credential import (
    CredentialBindingError,
    CredentialNeedsReauthentication,
    HttpVersionedCredentialBinding,
    VersionedCredentialBinding,
)
from agentrt.sdk.event import (
    ActionEvent,
    AgentErrorEvent,
    ObservationBaseEvent,
    ObservationEvent,
    StreamingDeltaEvent,
)
from agentrt.sdk.event.conversation_error import ConversationErrorEvent
from agentrt.sdk.event.conversation_state import ConversationStateUpdateEvent
from agentrt.sdk.event.error_classification import (
    ErrorClassification,
    FailureKind,
    classify_error,
)
from agentrt.sdk.event.llm_completion_log import LLMCompletionLogEvent
from agentrt.sdk.git.exceptions import GitCommandError, GitRepositoryError
from agentrt.sdk.git.utils import (
    resolve_local_commit,
    run_git_command,
    validate_git_repository,
)
from agentrt.sdk.llm.streaming import LLMStreamChunk
from agentrt.sdk.mcp.utils import MCPToolProvider
from agentrt.sdk.security.analyzer import SecurityAnalyzerBase
from agentrt.sdk.security.confirmation_policy import ConfirmationPolicyBase
from agentrt.sdk.utils import utc_now
from agentrt.sdk.utils.async_utils import AsyncCallbackWrapper
from agentrt.sdk.utils.cipher import Cipher
from agentrt.sdk.utils.files import atomic_write_text
from agentrt.sdk.workspace import LocalWorkspace


#: Prompt for the opt-in finalize summary. It asks for a report of work already
#: recorded, not for more work, and says so, because the summary runs with tools
#: disabled and must not claim anything the transcript does not support.
FINALIZE_SUMMARY_PROMPT = (
    "A task was stopped before it finished. Using only the work already "
    "recorded as the answer below, write a short report for the caller: what "
    "was completed, what remains, and anything a follow-up run needs to know. "
    "Do not claim anything the recorded work does not support.\n\n"
    "Recorded answer:\n{answer}"
)


def _progress_age_seconds(last_progress_at: str | None) -> float | None:
    """Seconds since the newest persisted event, or None if it is unknown.

    Reported, never acted on. The orchestration notes measured a session that
    read its inputs and then produced nothing for five minutes while composing
    one response: silence is not stalled-ness, and a watchdog that killed on it
    would throw away turns that were about to land.
    """
    if not last_progress_at:
        return None
    try:
        parsed = datetime.fromisoformat(last_progress_at)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - parsed).total_seconds())


def _finalize_completion(llm, prompt: str) -> str:
    """One tools-disabled completion for the finalize summary.

    No tools are passed, so none can be offered or executed. Streaming is off
    because the text is consumed whole with no token callback.
    """
    if getattr(llm, "stream", False):
        llm = llm.model_copy(update={"stream": False})
    messages = [Message(role="user", content=[TextContent(text=prompt)])]
    response = llm.completion(messages)
    blocks = getattr(getattr(response, "message", None), "content", None) or []
    return "".join(
        block.text for block in blocks if isinstance(block, TextContent)
    ).strip()


LEASE_RENEW_INTERVAL_SECONDS = 15.0
# Bounds initial-state push so subscribe_to_events does not stall on a
# subscriber whose __call__ blocks (e.g. WS with a full TCP send buffer).
INITIAL_STATE_PUSH_TIMEOUT_SECONDS = 0.5

#: H8 item 12. Every other terminal state is a bare `error`; a run that never
#: started gets a code that says so, so an orchestrator can tell "the provider
#: never answered" from "the agent failed" without reading the transcript.
#: Deliberately *not* a new ConversationExecutionStatus value: the status enum
#: is persisted and read by clients that would meet an unknown value, and the
#: closed failure vocabulary already carries the distinction (see H9 item 1,
#: which is what puts `classification` in front of the caller).
START_DEADLINE_ERROR_CODE = "RunStartDeadlineExceeded"

#: How often the watchdog re-checks. Coarse on purpose: the question is whether
#: anything has happened in the last few minutes, not in the last second.
_START_DEADLINE_POLL_SECONDS = 15.0


logger = get_logger(__name__)


class CredentialBindingActivationTooLate(RuntimeError):
    pass


def _without_agent_context_secret(
    agent: AgentBase,
    secret_name: str,
) -> AgentBase:
    context = agent.agent_context
    if context is None or not context.secrets or secret_name not in context.secrets:
        return agent
    secrets = dict(context.secrets)
    secrets.pop(secret_name, None)
    return agent.model_copy(
        update={"agent_context": context.model_copy(update={"secrets": secrets})}
    )


@dataclass
class EventService:
    """
    Event service for a conversation running locally, analogous to a conversation
    in the SDK. Async mostly for forward compatibility
    """

    stored: StoredConversation
    conversations_dir: Path
    # Agent for a NEW conversation. meta.json (``stored``) no longer carries the
    # agent — base_state.json is its single source of truth — so the creating
    # caller passes it here. On resume this is ``None`` and the agent is loaded
    # from base_state.json.
    agent: AgentBase | None = None
    cipher: Cipher | None = None
    mcp_tool_provider: MCPToolProvider | None = None
    credential_bindings: dict[str, VersionedCredentialBinding] = field(
        default_factory=dict
    )
    owner_instance_id: str = field(default_factory=lambda: uuid4().hex)
    lease_ttl_seconds: float = DEFAULT_LEASE_TTL_SECONDS
    _conversation: LocalConversation | None = field(default=None, init=False)
    _pub_sub: PubSub[Event] = field(
        default_factory=lambda: PubSub[Event](max_subscribers=50), init=False
    )
    _run_task: asyncio.Task | None = field(default=None, init=False)
    # Set when a send_message(run=True) is rejected because a run is still
    # wrapping up; consumed by _run_and_publish to re-run the stranded message.
    _rerun_requested: bool = field(default=False, init=False)
    # Set only for the internal ACP interrupt/restart path triggered by a new
    # send_message(run=True). Explicit user pause/interrupt clears it so user
    # stop intent wins over an earlier automatic restart request.
    _acp_internal_rerun_requested: bool = field(default=False, init=False)
    # Incremented for explicit user pause/interrupt requests. Internal ACP
    # supersede restarts compare this generation after their interrupt drains
    # so a later Stop/Pause cannot be overwritten by an automatic restart.
    _explicit_interrupt_generation: int = field(default=0, init=False)
    _closing: bool = field(default=False, init=False)
    _run_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _callback_wrapper: AsyncCallbackWrapper | None = field(default=None, init=False)
    _lease: ConversationLease | None = field(default=None, init=False)
    _lease_generation: int | None = field(default=None, init=False)
    _lease_task: asyncio.Task | None = field(default=None, init=False)
    _external_lease_renewal: bool = field(default=False, init=False)
    _run_executor: ThreadPoolExecutor | None = field(default=None, init=False)
    # Background task for a /goal loop that is running inside this conversation.
    _goal_loop_task: asyncio.Task | None = field(default=None, init=False)
    _goal_loop_outcome: GoalOutcome | None = field(default=None, init=False)
    # Monotonic clock of the last activity, used for idle eviction.
    _last_active_monotonic: float = field(default_factory=time.monotonic, init=False)
    # Monotonic clock of the last throttled streaming heartbeat.
    _last_stream_activity_signal: float = field(default=float("-inf"), init=False)
    # Subscribers attached at startup; later ones (e.g. websockets) are external.
    _internal_subscriber_ids: set[UUID] = field(default_factory=set, init=False)

    @property
    def conversation_dir(self):
        return self.conversations_dir / self.stored.id.hex

    async def load_meta(self):
        meta_file = self.conversation_dir / "meta.json"
        self.stored = StoredConversation.model_validate_json(
            meta_file.read_text(encoding="utf-8"),
            context={
                "cipher": self.cipher,
            },
        )

    async def save_meta(self):
        with self._write_guard():
            meta_file = self.conversation_dir / "meta.json"
            meta_file.write_text(
                self.stored.model_dump_json(
                    context={
                        "cipher": self.cipher,
                    }
                ),
                encoding="utf-8",
            )

    def _verify_pinned_workspace(self, working_dir: Path, pinned_sha: str) -> None:
        """Refuse to resume a pinned workspace that is gone or has moved.

        The failure this prevents is silent: `start` otherwise creates the
        missing directory and `git init`s it, so a session whose snapshot was
        cleaned up would run against a fresh empty repository and report work
        against a revision nobody reviewed.
        """
        if not working_dir.is_dir():
            raise ValueError(
                f"pinned workspace {working_dir} is missing; "
                f"{self.stored.workspace_mode} mode does not re-create it from "
                f"{pinned_sha[:12]}"
            )
        try:
            validate_git_repository(str(working_dir))
        except (GitCommandError, GitRepositoryError) as exc:
            raise ValueError(
                f"pinned workspace {working_dir} is not a git repository: {exc}"
            ) from exc
        if self.stored.workspace_mode != "snapshot":
            # A writer's own worktree moves its branch on purpose; only the
            # immutable review tree has a revision that must not change.
            return
        head = resolve_local_commit(working_dir)
        if head is None:
            # An unresolvable HEAD is not evidence of a match: a worktree whose
            # pinned object was pruned looks exactly like one replaced by a
            # fresh empty repository, and `validate_git_repository` accepts both.
            raise ValueError(
                f"snapshot workspace {working_dir} has no resolvable HEAD; "
                f"cannot confirm the pinned {pinned_sha[:12]}"
            )
        if head != pinned_sha:
            raise ValueError(
                f"snapshot workspace {working_dir} is at {head[:12]}, not the "
                f"pinned {pinned_sha[:12]}; refusing to run against a "
                "different revision"
            )

    def _without_stored_secret(self, secret_name: str) -> StoredConversation:
        # meta.json (StoredConversation) no longer carries the agent, so there is
        # no agent_context secret to scrub here — only the stored secrets map.
        # The agent's own secret scrub happens on base_state.json (see
        # _scrub_persisted_credentials).
        secrets = dict(self.stored.secrets)
        secrets.pop(secret_name, None)
        return self.stored.model_copy(update={"secrets": secrets})

    async def _scrub_persisted_credentials(
        self,
        credential_bindings: Mapping[str, VersionedCredentialBinding] | None = None,
    ) -> None:
        bindings = (
            self.credential_bindings
            if credential_bindings is None
            else credential_bindings
        )
        if not bindings:
            return

        required_bindings = {
            name
            for name, binding in bindings.items()
            if isinstance(binding, HttpVersionedCredentialBinding)
        }
        if required_bindings:
            # Scrubbed durable credentials must never become a fallback again.
            self.stored = self.stored.model_copy(
                update={
                    "required_runtime_credential_bindings": (
                        self.stored.required_runtime_credential_bindings
                        | required_bindings
                    )
                }
            )

        context = {"cipher": self.cipher}
        base_state_file = self.conversation_dir / BASE_STATE
        meta_file = self.conversation_dir / "meta.json"
        legacy_auth_file = self.conversation_dir / "acp" / "codex" / "auth.json"
        codex_binding = bindings.get(CODEX_AUTH_SECRET_NAME)
        if codex_binding is not None and legacy_auth_file.exists():
            resolved = await codex_binding.load()
            if not is_valid_codex_auth(resolved.value):
                raise CredentialNeedsReauthentication(
                    "ChatGPT authentication is invalid. Please sign in again."
                )
        for secret_name in bindings:
            self.stored = self._without_stored_secret(secret_name)

        if (
            not base_state_file.exists()
            and not meta_file.exists()
            and not legacy_auth_file.exists()
        ):
            return

        with self._write_guard():
            if base_state_file.exists():
                state = ConversationState.model_validate_json(
                    base_state_file.read_text(encoding="utf-8"),
                    context=context,
                )
                sources = dict(state.secret_registry.secret_sources)
                for secret_name in bindings:
                    sources.pop(secret_name, None)
                    state.agent = _without_agent_context_secret(
                        state.agent,
                        secret_name,
                    )
                state.secret_registry = state.secret_registry.model_copy(
                    update={"secret_sources": sources}
                )
                atomic_write_text(
                    base_state_file,
                    state.model_dump_json(exclude_none=True, context=context),
                )

            if meta_file.exists():
                atomic_write_text(
                    meta_file,
                    self.stored.model_dump_json(context=context),
                )
            if codex_binding is not None:
                legacy_auth_file.unlink(missing_ok=True)

    async def activate_credential_binding(
        self,
        secret_name: str,
        binding: VersionedCredentialBinding,
    ) -> None:
        existing = self.credential_bindings.get(secret_name)
        if isinstance(existing, HttpVersionedCredentialBinding) and isinstance(
            binding, HttpVersionedCredentialBinding
        ):
            if existing.url != binding.url:
                raise CredentialBindingActivationTooLate
            await self._scrub_persisted_credentials(
                {**self.credential_bindings, secret_name: binding}
            )
            existing.reauthorize(binding)
            return
        if existing is not None:
            raise CredentialBindingActivationTooLate

        conversation = self._conversation
        if conversation is None:
            raise CredentialBindingActivationTooLate

        state = conversation._state
        with state:
            if not isinstance(conversation.agent, ACPAgent):
                raise CredentialBindingActivationTooLate
            agent = cast(
                ACPAgent,
                _without_agent_context_secret(conversation.agent, secret_name),
            )
            try:
                agent.activate_file_credential_binding(secret_name, binding)
            except RuntimeError as exc:
                raise CredentialBindingActivationTooLate from exc

            self.credential_bindings[secret_name] = binding
            sources = dict(state.secret_registry.secret_sources)
            sources.pop(secret_name, None)
            state.secret_registry = state.secret_registry.model_copy(
                update={"secret_sources": sources}
            )
            state.agent = agent
            conversation.agent = agent
            self.stored = self._without_stored_secret(secret_name)
        await self._scrub_persisted_credentials()

    async def apply_resume_secrets(
        self,
        secrets: dict[str, SecretValue],
    ) -> None:
        conversation = self._conversation
        if conversation is None:
            raise ValueError("inactive_service")
        secrets = {
            name: value
            for name, value in secrets.items()
            if name not in self.credential_bindings
        }
        if not secrets:
            return

        def _update() -> None:
            state = conversation._state
            with state:
                registry = state.secret_registry.model_copy(
                    update={
                        "secret_sources": dict(state.secret_registry.secret_sources)
                    }
                )
                registry.update_secrets(secrets)
                state.secret_registry = registry
                agent = conversation.agent
                if isinstance(agent, ACPAgent):
                    agent.restart_for_updated_credentials(secrets)

        await asyncio.to_thread(_update)
        self.stored = self.stored.model_copy(
            update={"secrets": {**self.stored.secrets, **secrets}}
        )
        await self.save_meta()

    def _write_guard(self):
        if self._lease is None or self._lease_generation is None:
            return nullcontext()
        return self._lease.guarded_write(self._lease_generation)

    def renew_lease(self) -> None:
        """Renew this service's conversation lease.

        Called by a centralized renewal loop (when ``_external_lease_renewal``
        is True) or by the per-service ``_renew_lease_loop`` background task.
        """
        if self._lease is None or self._lease_generation is None:
            return
        try:
            self._lease.renew(self._lease_generation)
        except ConversationOwnershipLostError:
            logger.warning(
                "Conversation lease lost while renewing: %s",
                self.stored.id,
            )
        except Exception:
            logger.exception(
                "Failed to renew conversation lease for %s",
                self.stored.id,
            )

    async def _renew_lease_loop(self) -> None:
        if self._lease is None or self._lease_generation is None:
            return
        try:
            while True:
                await asyncio.sleep(LEASE_RENEW_INTERVAL_SECONDS)
                self.renew_lease()
        except asyncio.CancelledError:
            raise

    def get_conversation(self):
        if not self._conversation:
            raise ValueError("inactive_service")
        return self._conversation

    def _get_event_sync(self, event_id: str) -> Event | None:
        """Private sync function to get a single event.

        Reads directly from the EventLog without acquiring the state lock.
        EventLog reads are safe without the FIFOLock because events are
        append-only and immutable once written.
        """
        if not self._conversation:
            raise ValueError("inactive_service")
        events = self._conversation._state.events
        index = events.get_index(event_id)
        return events[index]

    async def get_event(self, event_id: str) -> Event | None:
        if not self._conversation:
            raise ValueError("inactive_service")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._get_event_sync, event_id)

    def _event_matches_filters(
        self,
        event: Event,
        kind: str | None,
        source: str | None,
        body: str | None,
        timestamp_gte_str: str | None,
        timestamp_lt_str: str | None,
    ) -> bool:
        """Return True if ``event`` matches all of the provided filters."""
        if (
            kind is not None
            and f"{event.__class__.__module__}.{event.__class__.__name__}" != kind
        ):
            return False
        if source is not None and event.source != source:
            return False
        if timestamp_gte_str is not None and event.timestamp < timestamp_gte_str:
            return False
        if timestamp_lt_str is not None and event.timestamp >= timestamp_lt_str:
            return False
        # ``body`` is the most expensive filter (deserializes message content),
        # so evaluate it last.
        if body is not None and not self._event_matches_body(event, body):
            return False
        return True

    def _get_searchable_event(self, events: EventsListBase, index: int) -> Event | None:
        try:
            return events[index]
        except (FileNotFoundError, UnicodeDecodeError, ValidationError) as exc:
            logger.warning(
                "Skipping unreadable event at index %d for conversation %s (%s)",
                index,
                self.stored.id,
                type(exc).__name__,
            )
            return None

    def _search_events_sync(
        self,
        page_id: str | None = None,
        limit: int = 100,
        kind: str | None = None,
        source: str | None = None,
        body: str | None = None,
        sort_order: EventSortOrder = EventSortOrder.TIMESTAMP,
        timestamp__gte: datetime | None = None,
        timestamp__lt: datetime | None = None,
    ) -> EventPage:
        """Private sync function to search events.

        Reads directly from the EventLog without acquiring the state lock.
        EventLog reads are safe without the FIFOLock because events are
        append-only and immutable once written.

        Performance:
            Events are appended in chronological order and never reordered,
            so the on-disk index order matches the timestamp sort order.
            We exploit that by iterating the underlying ``Sequence`` lazily
            by index (forward for TIMESTAMP, backward for TIMESTAMP_DESC),
            stopping as soon as we have ``limit + 1`` filter matches.

            This turns ``search_events`` from O(N) disk reads + O(N log N)
            sort into O(limit + skipped) reads with no sort, which is the
            difference between "loads instantly" and "blocks for seconds"
            for long conversations.
        """
        if not self._conversation:
            raise ValueError("inactive_service")

        events = self._conversation._state.events
        total = len(events)

        # Convert datetime to ISO string for comparison (ISO strings are comparable)
        timestamp_gte_str = timestamp__gte.isoformat() if timestamp__gte else None
        timestamp_lt_str = timestamp__lt.isoformat() if timestamp__lt else None

        reverse = sort_order == EventSortOrder.TIMESTAMP_DESC

        # Resolve page_id to a starting index. Prefer the EventLog's O(1)
        # id-to-index map; fall back to a linear scan for plain sequences
        # (e.g. in tests). An unknown page_id falls back to the natural
        # start of the iteration order, matching prior behavior.
        start_index: int | None = None
        if page_id:
            get_index = getattr(events, "get_index", None)
            if get_index is not None:
                try:
                    start_index = get_index(page_id)
                except KeyError:
                    start_index = None
            else:
                for i in range(total):
                    event = self._get_searchable_event(events, i)
                    if event is not None and event.id == page_id:
                        start_index = i
                        break
        if start_index is None:
            start_index = total - 1 if reverse else 0

        if reverse:
            indices: range = range(start_index, -1, -1)
        else:
            indices = range(start_index, total)

        items: list[Event] = []
        next_page_id: str | None = None
        for i in indices:
            event = self._get_searchable_event(events, i)
            if event is None:
                continue
            if not self._event_matches_filters(
                event, kind, source, body, timestamp_gte_str, timestamp_lt_str
            ):
                continue
            if len(items) >= limit:
                next_page_id = event.id
                break
            items.append(event)

        return EventPage(items=items, next_page_id=next_page_id)

    async def search_events(
        self,
        page_id: str | None = None,
        limit: int = 100,
        kind: str | None = None,
        source: str | None = None,
        body: str | None = None,
        sort_order: EventSortOrder = EventSortOrder.TIMESTAMP,
        timestamp__gte: datetime | None = None,
        timestamp__lt: datetime | None = None,
    ) -> EventPage:
        if not self._conversation:
            raise ValueError("inactive_service")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            self._search_events_sync,
            page_id,
            limit,
            kind,
            source,
            body,
            sort_order,
            timestamp__gte,
            timestamp__lt,
        )

    def _count_events_sync(
        self,
        kind: str | None = None,
        source: str | None = None,
        body: str | None = None,
        timestamp__gte: datetime | None = None,
        timestamp__lt: datetime | None = None,
    ) -> int:
        """Private sync function to count events.

        Reads directly from the EventLog without acquiring the state lock.
        EventLog reads are safe without the FIFOLock because events are
        append-only and immutable once written.
        """
        if not self._conversation:
            raise ValueError("inactive_service")

        events = self._conversation._state.events

        # Fast path: with no filters, the count is just the sequence length
        # and we can avoid reading any event payloads from disk.
        if (
            kind is None
            and source is None
            and body is None
            and timestamp__gte is None
            and timestamp__lt is None
        ):
            return len(events)

        # Convert datetime to ISO string for comparison (ISO strings are comparable)
        timestamp_gte_str = timestamp__gte.isoformat() if timestamp__gte else None
        timestamp_lt_str = timestamp__lt.isoformat() if timestamp__lt else None

        count = 0
        for i in range(len(events)):
            event = self._get_searchable_event(events, i)
            if event is None:
                continue
            if self._event_matches_filters(
                event, kind, source, body, timestamp_gte_str, timestamp_lt_str
            ):
                count += 1
        return count

    async def count_events(
        self,
        kind: str | None = None,
        source: str | None = None,
        body: str | None = None,
        timestamp__gte: datetime | None = None,
        timestamp__lt: datetime | None = None,
    ) -> int:
        """Count events matching the given filters."""
        if not self._conversation:
            raise ValueError("inactive_service")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            self._count_events_sync,
            kind,
            source,
            body,
            timestamp__gte,
            timestamp__lt,
        )

    def _get_execution_status_sync(self) -> ConversationExecutionStatus:
        if not self._conversation:
            raise ValueError("inactive_service")
        with self._conversation._state as state:
            return state.execution_status

    async def _get_execution_status(self) -> ConversationExecutionStatus:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._get_execution_status_sync)

    # -- H8 item 12: a run that never starts -------------------------------

    def _record_start_deadline_sync(self, deadline: float) -> None:
        """Explain a run that produced nothing, and stop calling it `error`.

        Emits the same kind of event the run-loop backstop does, with a code
        that names this outcome rather than leaving a bare `error`, and a
        classification from the shared vocabulary. Best-effort: never raises.
        """
        if not self._conversation:
            return
        try:
            event = ConversationErrorEvent(
                source="environment",
                code=START_DEADLINE_ERROR_CODE,
                detail=(
                    f"The run produced no action and no observation within "
                    f"{deadline:.0f}s of starting, so it was stopped. This is "
                    "the shape of a provider call that never returned rather "
                    "than a slow answer: no step completed and no tool was "
                    "called, so nothing was discarded. The provider's own "
                    "retry counter is not visible from here, so the number of "
                    "attempts it made is not reported."
                ),
                classification=classify_error(START_DEADLINE_ERROR_CODE),
            )
            with self._conversation._state:
                self._conversation._on_event(event)
            self._mark_error_status_sync()
        except Exception:  # noqa: BLE001 -- an error handler must not raise
            logger.warning("could not record the start-deadline outcome", exc_info=True)

    def _run_has_started(self, baseline_events: int, baseline_iterations: int) -> bool:
        """Has this run produced its first action, observation or step?

        Deliberately *this* run: the baselines are taken when it starts, so a
        resumed conversation whose earlier runs called tools does not look
        started on arrival. The iteration counter is checked as well as the
        events because a step can complete an LLM call without yet having an
        action to show for it.
        """
        conversation = self._conversation
        state = getattr(conversation, "_state", None) if conversation else None
        if state is None:
            return True  # nothing to watch
        if getattr(state, "iterations_used", 0) > baseline_iterations:
            return True
        for event in state.events[baseline_events:]:
            if isinstance(event, (ActionEvent, ObservationEvent)):
                return True
        return False

    async def _watch_start_deadline(
        self, baseline_events: int, baseline_iterations: int
    ) -> None:
        """Stop a run that has produced nothing by the deadline.

        The deadline is derived from the provider's own retry budget (see
        `agent_server.config.start_deadline_seconds`), not chosen: a legitimate
        first call can still be running long after a round number, and killing
        one would discard real work. What makes this safe where H7 rejected a
        general stall watchdog is that a run with no step completed and no
        action persisted has nothing to discard.

        Ordering matters: the run is interrupted first, because the interrupt
        leaves the conversation PAUSED, and the typed error is recorded after
        it so ERROR is the status that stands.
        """
        deadline = server_config.start_deadline_seconds(self.conversations_dir)
        if deadline <= 0:
            return
        loop = asyncio.get_running_loop()
        deadline_at = loop.time() + deadline
        while True:
            remaining = deadline_at - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(_START_DEADLINE_POLL_SECONDS, remaining))
            if self._closing or self._run_has_started(
                baseline_events, baseline_iterations
            ):
                return
        if self._closing or self._run_has_started(baseline_events, baseline_iterations):
            return
        logger.warning(
            "run produced no action or observation within %.0fs; stopping it",
            deadline,
        )
        await self.interrupt()
        await loop.run_in_executor(None, self._record_start_deadline_sync, deadline)

    def _mark_error_status_sync(self) -> None:
        """Force the conversation into ERROR status (idempotent backstop).

        Called when a run task raised before the conversation could set its own
        ERROR status — e.g. an exception in ``init_state``, which executes
        outside ``run()``/``arun()``'s try-block (via ``_ensure_agent_ready()``).
        Without this, the run's finally would publish a stale non-error status
        (IDLE/RUNNING) and the failure would look like a clean stop. No-op once
        the status is already ERROR. Best-effort: never raises (the caller is an
        error handler).
        """
        if not self._conversation:
            return
        with self._conversation._state as state:
            if state.execution_status != ConversationExecutionStatus.ERROR:
                state.execution_status = ConversationExecutionStatus.ERROR

    def _publish_error_event_sync(self, exc: BaseException) -> None:
        """Emit a ConversationErrorEvent so the UI sees the failure detail.

        For LLM/runtime failures that would otherwise only reach the logs — the
        run-loop backstop and auto-title generation (issue #16686). Best-effort:
        never raises (the caller is an error handler).
        """
        if not self._conversation:
            return
        try:
            error_event = ConversationErrorEvent(
                source="environment",
                code=type(exc).__name__,
                detail=str(exc),
            )
            with self._conversation._state:
                self._conversation._on_event(error_event)
        except Exception:
            logger.exception("Failed to publish backstop ConversationErrorEvent")

    def _create_state_update_event_sync(self) -> ConversationStateUpdateEvent:
        if not self._conversation:
            raise ValueError("inactive_service")
        state = self._conversation._state
        with state:
            return ConversationStateUpdateEvent.from_conversation_state(state)

    async def _create_state_update_event(self) -> ConversationStateUpdateEvent:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._create_state_update_event_sync)

    def _event_matches_body(self, event: Event, body: str) -> bool:
        """Check if event's message content matches body filter (case-insensitive)."""
        # Import here to avoid circular imports
        from agentrt.sdk.event.llm_convertible.message import MessageEvent
        from agentrt.sdk.llm.message import content_to_str

        # Only check MessageEvent instances for body content
        if not isinstance(event, MessageEvent):
            return False

        # Extract text content from the message
        text_parts = content_to_str(event.llm_message.content)

        # Also check extended content if present
        if event.extended_content:
            extended_text_parts = content_to_str(event.extended_content)
            text_parts.extend(extended_text_parts)

        # Also check reasoning content if present
        if event.reasoning_content:
            text_parts.append(event.reasoning_content)

        # Combine all text content and perform case-insensitive substring match
        full_text = " ".join(text_parts).lower()
        return body.lower() in full_text

    async def batch_get_events(self, event_ids: list[str]) -> list[Event | None]:
        """Given a list of ids, get events (Or none for any which were not found)"""
        results = await asyncio.gather(
            *[self.get_event(event_id) for event_id in event_ids]
        )
        return results

    async def send_message(
        self, message: Message, run: bool = False, _from_goal_loop: bool = False
    ):
        if not self._conversation:
            raise ValueError("inactive_service")
        # A normal user message supersedes any active /goal loop in this
        # conversation. The goal loop's own messages pass _from_goal_loop=True.
        if not _from_goal_loop:
            await self.stop_goal_loop()
        explicit_interrupt_generation = self._explicit_interrupt_generation
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._conversation.send_message, message)
        if run:
            if self._explicit_interrupt_generation != explicit_interrupt_generation:
                return
            (
                did_mark_acp_prompt_superseded,
                active_acp_prompt_has_latest_message,
            ) = await self._mark_running_acp_prompt_superseded()
            interrupted_acp = False
            if did_mark_acp_prompt_superseded:
                self._acp_internal_rerun_requested = True
                interrupted_acp = True
                await self.interrupt(internal_acp_rerun=True)
                if self._explicit_interrupt_generation != explicit_interrupt_generation:
                    return
            try:
                await self.run(
                    acp_internal_rerun_generation=explicit_interrupt_generation
                )
                self._acp_internal_rerun_requested = False
            except ValueError as e:
                # run() refused. If a run is still wrapping up (its
                # wait_for_pending tail), the message we just appended won't be
                # picked up by it, so record explicit run intent for
                # _run_and_publish to honor once that task clears. Tracking the
                # request — rather than inferring it later from an IDLE status —
                # is what keeps a deliberate run=False append, or an IDLE reached
                # via another path, from triggering an unwanted run.
                # "inactive_service" is terminal and must not re-arm.
                if (
                    str(e) == "conversation_already_running"
                    and not active_acp_prompt_has_latest_message
                ):
                    self._rerun_requested = True
                    if interrupted_acp:
                        self._acp_internal_rerun_requested = True

    def _mark_running_acp_prompt_superseded_sync(self) -> tuple[bool, bool]:
        """Mark the currently running ACP prompt superseded if needed.

        The tuple is ``(did_mark_superseded, active_prompt_has_latest_message)``.
        If the running ACP prompt has already advanced to the newly appended
        user message, interrupting it would cancel the replacement prompt and
        strand that message behind the persisted cursor.
        """
        if not self._conversation:
            return (False, False)
        if self._run_task is None or self._run_task.done():
            return (False, False)
        if not isinstance(self._conversation.agent, ACPAgent):
            return (False, False)
        with self._conversation._state as state:
            if state.execution_status != ConversationExecutionStatus.RUNNING:
                return (False, False)
            inflight_prompt_user_message_id = state.agent_state.get(
                ACP_INFLIGHT_PROMPT_USER_MESSAGE_ID
            )
            last_user_message_id = state.last_user_message_id
            if inflight_prompt_user_message_id is None or last_user_message_id is None:
                return (False, False)
            active_prompt_has_latest_message = (
                inflight_prompt_user_message_id == last_user_message_id
            )
            if active_prompt_has_latest_message:
                return (False, True)
            state.agent_state = {
                **state.agent_state,
                ACP_SUPERSEDE_INFLIGHT_PROMPT: True,
            }
            return (True, False)

    async def _mark_running_acp_prompt_superseded(self) -> tuple[bool, bool]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._mark_running_acp_prompt_superseded_sync
        )

    async def subscribe_to_events(self, subscriber: Subscriber[Event]) -> UUID:
        subscriber_id = self._pub_sub.subscribe(subscriber)

        # Send current state to the new subscriber immediately.
        # The snapshot is created in a worker thread so waiting on the
        # conversation's synchronous FIFOLock cannot block the server event loop.
        if self._conversation:
            state_update_event = await self._create_state_update_event()
        else:
            state_update_event = ConversationStateUpdateEvent(
                key="execution_status",
                value=ConversationExecutionStatus.IDLE,
            )

        try:
            await asyncio.wait_for(
                subscriber(state_update_event),
                timeout=INITIAL_STATE_PUSH_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            # Subscriber stays registered; only the initial-state push is
            # dropped. Subsequent publishes go through pub_sub and may
            # still block there if the subscriber remains wedged.
            logger.warning(
                f"Initial state push to subscriber {subscriber_id} timed "
                f"out after {INITIAL_STATE_PUSH_TIMEOUT_SECONDS}s."
            )
        # Non-timeout errors propagate to caller (e.g. webhook failures).

        return subscriber_id

    async def unsubscribe_from_events(self, subscriber_id: UUID) -> bool:
        return self._pub_sub.unsubscribe(subscriber_id)

    def _emit_event_from_thread(self, event: Event) -> None:
        """Helper to safely emit events from non-async contexts (e.g., callbacks).

        This schedules event emission in the main event loop, making it safe to call
        from callbacks that may run in different threads. Events are emitted through
        the conversation's normal event flow to ensure they are persisted.
        """
        main_loop = self._main_loop
        conversation = self._conversation
        if main_loop and main_loop.is_running() and conversation:
            # Wrap _on_event with lock acquisition to ensure thread-safe access
            # to conversation state and event log during concurrent operations
            def locked_on_event():
                with conversation._state:
                    conversation._on_event(event)

            # Run the locked callback in an executor to ensure the event is
            # both persisted and sent to WebSocket subscribers
            main_loop.run_in_executor(None, locked_on_event)

    def _setup_llm_log_streaming(self, agent: AgentBase) -> None:
        """Configure LLM log callbacks to stream logs via events."""
        for llm in agent.get_all_llms():
            if not llm.log_completions:
                continue

            # Capture variables for closure
            usage_id = llm.usage_id
            model_name = llm.model

            def log_callback(
                filename: str, log_data: str, uid=usage_id, model=model_name
            ) -> None:
                """Callback to emit LLM completion logs as events."""
                try:
                    event = LLMCompletionLogEvent(
                        filename=filename,
                        log_data=log_data,
                        model_name=model,
                        usage_id=uid,
                    )
                    self._emit_event_from_thread(event)
                except Exception:
                    logger.exception("Failed to emit LLM completion log event")

            llm.telemetry.set_log_completions_callback(log_callback)

    def _setup_acp_activity_heartbeat(self, agent: AgentBase) -> None:
        """Wire ACP activity heartbeat to the idle timer.

        ACP agents delegate to an external subprocess (e.g. gemini-cli,
        claude-agent-acp).  Tool calls run inside that subprocess and never
        hit the agent-server's HTTP endpoints, so update_last_execution_time()
        is never called during conn.prompt().  Without a heartbeat the
        runtime-api sees growing idle_time and kills the pod (~20 min).

        This method checks if the agent is an ACPAgent and, if so, injects a
        callback that resets the idle timer whenever the ACP bridge receives
        a streaming update (throttled to every 30 s by the bridge).
        """
        from agentrt.sdk.agent import ACPAgent

        if isinstance(agent, ACPAgent):
            agent._on_activity = update_last_execution_time

    def _signal_stream_activity(self) -> None:
        """Refresh the runtime idle timer while a completion streams.

        Deltas are never persisted, so the durable-event path that calls
        update_last_execution_time() is silent for the length of a stream.
        Signalled from the producer so it survives deltas leaving the shared
        bus; throttled like the ACP bridge's _maybe_signal_activity.
        """
        now = time.monotonic()
        if now - self._last_stream_activity_signal < ACTIVITY_SIGNAL_INTERVAL:
            return
        self._last_stream_activity_signal = now
        update_last_execution_time()

    def _setup_stats_streaming(self, agent: AgentBase) -> None:
        """Configure stats update callbacks to stream stats changes via events."""

        def stats_callback() -> None:
            """Callback to emit stats updates.

            Invoked synchronously by ``Telemetry.on_response`` (regular
            Agent path) and ``ACPAgent._record_usage`` (ACP path) — both
            run inside ``LocalConversation.run()``'s ``with self._state:``
            block, so the caller already owns the conversation state lock.

            DO NOT re-acquire the state lock here (``with state:``). It
            looks safe — ``FIFOLock`` documents itself as reentrant — but
            on the ACP code path it deadlocks (silently) before the rest
            of ``step()`` can emit the assistant's FinishAction +
            ObservationEvent, leaving every conversation hung in
            ``running`` status forever. ``_emit_event_from_thread`` below
            already acquires the lock on the executor thread before
            persisting the event; that's the only place serialization
            needs the lock anyway.
            """
            # Publish only the stats field to avoid sending entire state
            if not self._conversation:
                return
            event = ConversationStateUpdateEvent(
                key="stats", value=self._conversation._state.stats
            )
            self._emit_event_from_thread(event)

        for llm in agent.get_all_llms():
            llm.telemetry.set_stats_update_callback(stats_callback)

    @staticmethod
    def _ensure_workspace_is_git_repo(working_dir: Path) -> None:
        """Initialize the workspace as a git repo if it isn't already one.

        The /api/git/changes endpoint expects a real repository to compute
        changes against; without this, agent-created files never appear in
        the Changes tab. We only run `git init` (no commit) — empty repos
        are handled by `get_valid_ref()` via GIT_EMPTY_TREE_HASH, and
        untracked files surface through `git ls-files --others`.
        """
        try:
            validate_git_repository(working_dir)
            return  # already a repo
        except GitRepositoryError:
            logger.debug(
                "Workspace %s is not a git repository; running `git init`",
                working_dir,
            )

        try:
            run_git_command(["git", "init"], working_dir)
        except GitCommandError as e:
            # Don't block conversation startup if git is missing or init
            # fails — the git router is defensive and will return [] anyway.
            logger.warning(
                "Failed to initialize git repository at %s: %s", working_dir, e
            )

    async def start(self):
        # Store the main event loop for cross-thread communication
        self._main_loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()

        # self.stored contains an Agent configuration we can instantiate
        self.conversation_dir.mkdir(parents=True, exist_ok=True)
        # lease_ttl_seconds=0 disables leasing for single-instance deployments
        # where shared-storage stale leases would otherwise block pod restarts.
        if self.lease_ttl_seconds > 0:
            self._lease = ConversationLease(
                conversation_dir=self.conversation_dir,
                owner_instance_id=self.owner_instance_id,
                ttl_seconds=self.lease_ttl_seconds,
            )
            lease_claim = self._lease.claim()
            self._lease_generation = lease_claim.generation
        await self._scrub_persisted_credentials()
        workspace = self.stored.workspace
        assert isinstance(workspace, LocalWorkspace)
        working_dir = Path(workspace.working_dir)
        pinned_sha = self.stored.workspace_resolved_sha
        if self.stored.workspace_mode != "shared" and pinned_sha:
            self._verify_pinned_workspace(working_dir, pinned_sha)
        else:
            working_dir.mkdir(parents=True, exist_ok=True)
            self._ensure_workspace_is_git_repo(working_dir)
        # base_state.json is the single source of truth for the agent. On resume
        # (base_state exists) pass ``agent=None`` so LocalConversation keeps the
        # persisted agent. On a new conversation the creating caller supplied the
        # agent via ``self.agent``; deep-copy it (expose_secrets) so the running
        # agent is independent of the caller's object.
        base_state_exists = await asyncio.to_thread(
            (self.conversation_dir / BASE_STATE).exists
        )
        if base_state_exists:
            agent: AgentBase | None = None
        else:
            if self.agent is None:
                raise ValueError(
                    "Cannot start a new conversation without an agent: no "
                    "base_state.json to resume and no agent was provided."
                )
            agent_cls = type(self.agent)
            agent = agent_cls.model_validate(
                self.agent.model_dump(context={"expose_secrets": True}),
            )

        # Create LocalConversation with plugins and hook_config.
        # Plugins are loaded lazily on first run()/send_message() call.
        # Hook execution semantics: OpenHands runs hooks sequentially with early-exit
        # on block (PreToolUse), unlike Claude Code's parallel execution model.

        # Create and store callback wrapper to allow flushing pending events
        self._callback_wrapper = AsyncCallbackWrapper(
            self._pub_sub, loop=asyncio.get_running_loop()
        )

        # Token streaming is wired only for agents that can actually emit token
        # callbacks (SDK LLM agents with stream=True, or ACP agents). For a NEW
        # conversation the agent is known here, so decide now. On RESUME the
        # agent is loaded from base_state.json during construction, so defer the
        # decision until after (see the post-construction block below).
        def _agent_can_stream(a: AgentBase) -> bool:
            return isinstance(a, ACPAgent) or any(
                llm.stream for llm in a.get_all_llms()
            )

        streaming_enabled = _agent_can_stream(agent) if agent is not None else True
        streaming_decided = agent is not None

        def _publish_stream_delta(
            content: str | None = None,
            reasoning_content: str | None = None,
        ) -> None:
            # Published directly to _pub_sub (not via _callback_wrapper) so
            # deltas reach subscribers but are NOT persisted to
            # ConversationState.events. See StreamingDeltaEvent docstring.
            if not self._main_loop or not self._main_loop.is_running():
                return
            # Use `is not None` rather than truthiness: some providers
            # emit legitimate empty-string chunks at stream boundaries
            # (e.g. after a tool call) that we still want to forward.
            if content is None and reasoning_content is None:
                return
            event = StreamingDeltaEvent(
                content=content,
                reasoning_content=reasoning_content,
            )
            self._signal_stream_activity()
            with suppress(RuntimeError):  # main loop already closed during teardown
                asyncio.run_coroutine_threadsafe(self._pub_sub(event), self._main_loop)

        def _token_streaming_callback(chunk: LLMStreamChunk | str) -> None:
            if isinstance(chunk, str):
                _publish_stream_delta(content=chunk)
                return

            for choice in chunk.choices or ():
                delta = choice.delta
                if delta is None:
                    continue
                content = getattr(delta, "content", None)
                reasoning = getattr(delta, "reasoning_content", None)
                _publish_stream_delta(
                    content=content if isinstance(content, str) else None,
                    reasoning_content=reasoning if isinstance(reasoning, str) else None,
                )

        conversation = LocalConversation(
            agent=agent,
            workspace=workspace,
            plugins=self.stored.plugins,
            persistence_dir=str(self.conversations_dir),
            conversation_id=self.stored.id,
            callbacks=[self._callback_wrapper],
            token_callbacks=([_token_streaming_callback] if streaming_enabled else []),
            max_iteration_per_run=self.stored.max_iterations,
            stuck_detection=self.stored.stuck_detection,
            visualizer=None,
            secrets=self.stored.secrets,
            cipher=self.cipher,
            hook_config=self.stored.hook_config,
            tags=self.stored.tags,
            user_id=self.stored.user_id,
            observability_metadata=self.stored.observability_metadata,
            observability_tags=self.stored.observability_tags,
            observability_span_name=self.stored.observability_span_name,
            mcp_tool_provider=self.mcp_tool_provider,
        )

        conversation.set_confirmation_policy(self.stored.confirmation_policy)
        conversation.set_security_analyzer(self.stored.security_analyzer)
        # On resume the agent was unknown at construction time (loaded from
        # base_state.json), so decide token streaming now and disable it when the
        # resolved agent can't emit token callbacks.
        if not streaming_decided:
            streaming_enabled = _agent_can_stream(conversation.agent)
            logger.debug(
                "Token streaming: %s",
                "enabled" if streaming_enabled else "disabled (no LLM has stream=True)",
            )
            if not streaming_enabled:
                conversation.set_token_callbacks(None)
        self._conversation = conversation
        if isinstance(conversation.agent, ACPAgent):
            for secret_name, binding in self.credential_bindings.items():
                conversation.agent.activate_file_credential_binding(
                    secret_name,
                    binding,
                )
        self._conversation._state.set_write_guard(self._write_guard)
        if not self._external_lease_renewal:
            self._lease_task = asyncio.create_task(self._renew_lease_loop())

        # Register state change callback to automatically publish updates
        self._conversation._state.set_on_state_change(self._conversation._on_event)

        # Setup LLM log streaming for remote execution
        self._setup_llm_log_streaming(self._conversation.agent)

        # Setup stats streaming for remote execution
        self._setup_stats_streaming(self._conversation.agent)

        # Wire ACP activity heartbeat so ACP tool calls (which run inside
        # the subprocess and never hit HTTP endpoints) still reset the
        # agent-server's idle timer and prevent runtime-api from killing
        # the pod during long conn.prompt() calls.
        self._setup_acp_activity_heartbeat(self._conversation.agent)

        # Any conversation loaded from disk with RUNNING status is stale. Active
        # split-brain resumes are prevented earlier by the lease claim itself, so if
        # we made it this far there is no live owner and the interrupted tool call
        # should be surfaced back to the agent.
        state = self._conversation.state
        if state.execution_status == ConversationExecutionStatus.RUNNING:
            state.execution_status = ConversationExecutionStatus.ERROR
            # Crash recovery scans the full log, not the active branch: the
            # process may have died between writing an event file and persisting
            # the advanced HEAD, so the leaf can lag the on-disk events. (Remote
            # branching is unsupported — #3749 — so there are no abandoned
            # branches to exclude here anyway.)
            unmatched_actions = ConversationState.get_unmatched_actions(state.events)
            if unmatched_actions:
                first_action = unmatched_actions[0]
                # Skip if any observation-like event already exists for this
                # tool_call_id, to avoid duplicate observations when an
                # observation matches by tool_call_id but not action_id.
                already_observed = any(
                    isinstance(e, ObservationBaseEvent)
                    and e.tool_call_id == first_action.tool_call_id
                    for e in state.events
                )
                if not already_observed:
                    # The persisted HEAD can lag this action when the process
                    # dies after writing the event file but before autosaving
                    # leaf_event_id. Parent the recovery result to the action
                    # explicitly; otherwise normal tree stamping attaches it to
                    # the stale HEAD, making the action and result siblings and
                    # leaving an orphan tool result on the active branch.
                    error_event = AgentErrorEvent(
                        parent_id=first_action.id,
                        tool_name=first_action.tool_name,
                        tool_call_id=first_action.tool_call_id,
                        error=(
                            "A restart occurred while this tool was in progress. "
                            "This may indicate a fatal memory error or system crash. "
                            "The tool execution was interrupted and did not complete."
                        ),
                        classification=ErrorClassification(
                            kind=FailureKind.INTERNAL, retryable=False
                        ),
                    )
                    self._conversation._on_event(error_event)

        # Publish initial state update
        await self._publish_state_update()

    async def run(self, acp_internal_rerun_generation: int | None = None):
        """Run the conversation asynchronously in the background.

        This method starts the conversation run in a background task and returns
        immediately.  When possible, the conversation is driven via its native
        ``arun()`` coroutine so LLM I/O does not tie up a thread-pool worker.
        For conversations that do not expose ``arun()`` (e.g., custom
        subclasses) or whose agent only implements sync ``step()`` (no
        ``astep()`` override), the synchronous ``run()`` is executed
        in the thread pool as before.

        Raises:
            ValueError: If the service is inactive or conversation is already running.
        """
        if not self._conversation or self._closing:
            raise ValueError("inactive_service")

        # Use lock to make check-and-set atomic, preventing race conditions
        async with self._run_lock:
            if (
                await self._get_execution_status()
                == ConversationExecutionStatus.RUNNING
            ):
                raise ValueError("conversation_already_running")
            if self._closing:
                raise ValueError("inactive_service")
            if (
                acp_internal_rerun_generation is not None
                and self._explicit_interrupt_generation != acp_internal_rerun_generation
            ):
                return

            # Check if there's already a running task
            if self._run_task is not None and not self._run_task.done():
                raise ValueError("conversation_already_running")

            # Capture conversation reference for the closure
            conversation = self._conversation

            # Start run in background
            loop = asyncio.get_running_loop()

            async def _run_and_publish():
                # H8 item 12. Baselines are taken before the run so the watchdog
                # judges *this* run, not the history a resumed conversation
                # already carries.
                _state = getattr(conversation, "_state", None)
                baseline_events = len(_state.events) if _state is not None else 0
                baseline_iterations = (
                    getattr(_state, "iterations_used", 0) if _state is not None else 0
                )
                deadline_watch = asyncio.create_task(
                    self._watch_start_deadline(baseline_events, baseline_iterations)
                )
                try:
                    # Prefer the native async path when available so the event
                    # loop is free during LLM I/O.  Fall back to thread-pool
                    # execution for backward compatibility.
                    #
                    # All guards are required:
                    #  • iscoroutinefunction – filters out non-async objects
                    #    (e.g. MagicMock in tests).
                    #  • conversation override – BaseConversation's default
                    #    ``arun()`` delegates to sync ``run()``, so we require an
                    #    *actual* override to avoid running a sync-only subclass
                    #    on the event loop.
                    #  • agent override – ``LocalConversation`` always overrides
                    #    ``arun()``, but an agent without an ``astep()`` override
                    #    runs sync ``step()`` in a worker thread; route it
                    #    through sync ``run()`` instead.
                    arun = getattr(conversation, "arun", None)
                    has_native_arun = (
                        arun is not None
                        and asyncio.iscoroutinefunction(arun)
                        and type(conversation).arun is not BaseConversation.arun
                        and type(conversation.agent).astep is not AgentBase.astep
                    )
                    if has_native_arun:
                        await conversation.arun()
                    else:
                        await loop.run_in_executor(self._run_executor, conversation.run)
                except Exception as exc:
                    logger.exception("Error during conversation run")
                    # Backstop: a run that raised before reaching its own error
                    # handling (e.g. an ACP cold-start failure in init_state,
                    # which runs outside run()/arun()'s try-block) can leave the
                    # status at IDLE/RUNNING. Force ERROR so the finally's
                    # _publish_state_update() surfaces the failure instead of a
                    # misleading non-error state.
                    #
                    # Also surface the detail to the UI (issue #16686). A
                    # ConversationRunError means run()/arun() already emitted its
                    # own event, so skip it there to avoid duplicating the error.
                    if not isinstance(exc, ConversationRunError):
                        await loop.run_in_executor(
                            None, self._publish_error_event_sync, exc
                        )
                    await loop.run_in_executor(None, self._mark_error_status_sync)
                finally:
                    # The run is over either way, so the watchdog has nothing
                    # left to judge; leaving it running would fire against the
                    # next run's baselines.
                    deadline_watch.cancel()
                    with suppress(asyncio.CancelledError):
                        await deadline_watch
                    # Wait for all pending events to be published via
                    # AsyncCallbackWrapper before publishing the final state update.
                    # This prevents a race condition where the conversation status
                    # becomes FINISHED before agent events (MessageEvent, ActionEvent,
                    # etc.) are published to WebSocket subscribers.
                    if self._callback_wrapper:
                        await loop.run_in_executor(
                            None, self._callback_wrapper.wait_for_pending, 30.0
                        )

                    # Clear task reference and publish state update
                    self._run_task = None
                    await self._publish_state_update()

                    # Re-arm a run for input stranded while this task was
                    # wrapping up. A send_message(run=True) that arrived during
                    # the wait_for_pending() tail above had its run() rejected as
                    # "conversation_already_running" and suppressed, setting
                    # _rerun_requested. Honor it while the conversation is IDLE
                    # (pending input) or internally ACP-interrupted PAUSED (the
                    # old task finished its interrupt before the replacement run
                    # could start). Explicit user pause/interrupt clears the
                    # internal ACP flag, so user stop intent wins over an older
                    # automatic restart request. If the run loop was still alive
                    # it already absorbed the message and we are FINISHED here,
                    # so the guard avoids a redundant run. A deliberate
                    # run=False append, or an IDLE reached via another path,
                    # never sets the flag.
                    rerun_requested = self._rerun_requested
                    acp_internal_rerun_requested = self._acp_internal_rerun_requested
                    rerun_generation = self._explicit_interrupt_generation
                    self._rerun_requested = False
                    self._acp_internal_rerun_requested = False
                    if rerun_requested:
                        status = await self._get_execution_status()
                        rerun_generation_still_valid = (
                            self._explicit_interrupt_generation == rerun_generation
                        )
                        acp_internal_rerun_still_valid = (
                            acp_internal_rerun_requested
                            and rerun_generation_still_valid
                        )
                        should_restart = rerun_generation_still_valid and (
                            status == ConversationExecutionStatus.IDLE
                            or (
                                acp_internal_rerun_still_valid
                                and status == ConversationExecutionStatus.PAUSED
                                and isinstance(conversation.agent, ACPAgent)
                            )
                        )
                        if should_restart:
                            try:
                                await self.run(
                                    acp_internal_rerun_generation=rerun_generation
                                    if acp_internal_rerun_still_valid
                                    else None
                                )
                            except ValueError as e:
                                if str(e) == "conversation_already_running":
                                    self._rerun_requested = True
                                    self._acp_internal_rerun_requested = (
                                        acp_internal_rerun_requested
                                    )
                                else:
                                    raise

            # Create task but don't await it - runs in background
            self._run_task = asyncio.create_task(_run_and_publish())

    async def start_goal_loop(
        self,
        objective: str,
        *,
        judge_llm: LLM | None = None,
        max_iterations: int = 10,
    ) -> None:
        """Start a ``/goal`` loop inside this conversation.

        Sends the objective, runs the agent, and judges completion after each
        run, re-prompting until the goal is done or ``max_iterations`` is
        reached. All work stays in this conversation's event history and stream,
        exactly like a normal run; this does not create another conversation.

        Args:
            objective: The goal to pursue and audit against.
            judge_llm: LLM that grades completion. Defaults to the agent's LLM.
            max_iterations: Hard cap on audit rounds before giving up.

        Raises:
            ValueError: If the service is inactive, a goal loop is already
                running, no judge LLM is available, or the objective is empty.
        """
        if not self._conversation or self._closing:
            raise ValueError("inactive_service")
        if judge_llm is None:
            judge_llm = getattr(self._conversation.agent, "llm", None)
        if judge_llm is None:
            raise ValueError("no_judge_llm")
        # GoalController validates the objective/max_iterations (raises ValueError).
        controller = GoalController(objective, judge_llm, max_iterations=max_iterations)
        # Under _run_lock, atomically refuse a concurrent goal loop or active
        # conversation run; otherwise /goal could judge an unrelated transcript.
        async with self._run_lock:
            if self._closing:
                raise ValueError("inactive_service")
            if self._goal_loop_task is not None and not self._goal_loop_task.done():
                raise ValueError("goal_already_running")
            # _run_task first: a live run holds the state lock across its step,
            # so reading execution status would block behind it.
            if (self._run_task is not None and not self._run_task.done()) or (
                await self._get_execution_status()
                == ConversationExecutionStatus.RUNNING
            ):
                raise ValueError("conversation_already_running")
            # Re-check after the await above: close() runs without _run_lock, so
            # it may have begun teardown meanwhile (mirrors run()'s post-status
            # _closing re-check) -- avoid spawning a task close() won't cancel.
            if self._closing:
                raise ValueError("inactive_service")
            self._goal_loop_outcome = None
            self._goal_loop_task = asyncio.create_task(self._run_goal_loop(controller))

    async def _run_goal_loop(
        self, controller: GoalController, *, resume: bool = False
    ) -> None:
        """Drive one active ``/goal`` loop inside this conversation.

        Reuses the SDK's transport-agnostic ``GoalController`` for decisions;
        this method owns only I/O: sending messages, awaiting each run, judging
        off the event loop, and publishing goal-status updates.
        """
        conversation = self._conversation
        if conversation is None:
            return
        loop = asyncio.get_running_loop()

        def _snapshot_and_judge() -> GoalStep:
            # Snapshot events under the conversation lock, then judge (an LLM
            # call) with the lock released -- both on this worker thread.
            with conversation._state:
                events = list(conversation._state.events)
            return controller.on_run_finished(events)

        def _user(text: str) -> Message:
            return Message(role="user", content=[TextContent(text=text)])

        async def _emit_status(
            *,
            active: bool,
            status: GoalStatusName,
            verdict: GoalVerdict | None = None,
        ) -> None:
            # Persist + publish a goal-status update so a UI can render a chip.
            # ConversationStateUpdateEvent is not LLM-convertible, so it never
            # enters the agent's or the judge's context.
            event = ConversationStateUpdateEvent(
                key="goal",
                value=GoalStatus(
                    active=active,
                    status=status,
                    iteration=controller.iteration,
                    max_iterations=controller.max_iterations,
                    objective=controller.objective,
                    verdict=verdict,
                ).model_dump(),
            )

            def _persist() -> None:
                with conversation._state:
                    conversation._on_event(event)

            await loop.run_in_executor(None, _persist)

        try:
            await _emit_status(active=True, status="running")
            nudge = RESUME_PROMPT if resume else controller.start()
            await self.send_message(_user(nudge), run=False, _from_goal_loop=True)
            while True:
                try:
                    await self.run()
                except ValueError as e:
                    if str(e) != "conversation_already_running":
                        raise
                run_task = self._run_task
                if run_task is not None:
                    await asyncio.wait({run_task})
                status = await self._get_execution_status()
                if status in (
                    ConversationExecutionStatus.PAUSED,
                    ConversationExecutionStatus.ERROR,
                ):
                    logger.info("Goal loop halted early: status=%s", status)
                    await _emit_status(active=False, status="interrupted")
                    return
                if status == ConversationExecutionStatus.STUCK:
                    # The stuck detector is a heuristic that often fires during
                    # legitimate iteration (re-running a test, retrying an edit).
                    # The goal loop already has an authoritative judge that
                    # audits completion each round, so a STUCK run is not a
                    # reason to halt the whole goal -- proceed to the judge and
                    # let it decide continue-vs-stop (sending a followup nudge
                    # that breaks the agent out of any genuine loop). Only
                    # PAUSED/ERROR (real stop signals) terminate the goal.
                    logger.info("Goal loop continuing past stuck run")
                step = await loop.run_in_executor(None, _snapshot_and_judge)
                if isinstance(step, GoalDone):
                    self._goal_loop_outcome = step.outcome
                    await _emit_status(
                        active=False,
                        status=step.outcome.status,
                        verdict=step.outcome.verdict,
                    )
                    logger.info(
                        "Goal %s after %d round(s)",
                        step.outcome.status,
                        step.outcome.iterations,
                    )
                    return
                # Carry the round's verdict so a UI can show per-round judge
                # feedback (score + what's missing), not just the final one.
                await _emit_status(active=True, status="running", verdict=step.verdict)
                await self.send_message(
                    _user(step.followup), run=False, _from_goal_loop=True
                )
        except asyncio.CancelledError:
            logger.info("Goal loop cancelled")
            # Explicit stop or user interjection: record a resumable
            # interrupted status, except during service teardown.
            if not self._closing:
                with suppress(Exception):
                    await _emit_status(active=False, status="interrupted")
            raise
        except Exception:
            logger.exception("Goal loop failed")
            # An unexpected failure (judge LLM error, controller bug, ...) leaves
            # the loop dead: record an interrupted status (resumable) so the UI
            # doesn't show it running. Skip during close(), like the cancel path.
            if not self._closing:
                with suppress(Exception):
                    await _emit_status(active=False, status="interrupted")
        finally:
            self._goal_loop_task = None

    async def stop_goal_loop(self) -> bool:
        """Cancel the active ``/goal`` loop inside this conversation.

        Returns True if a loop was active. Unlike ``interrupt()``, this targets
        the background goal loop itself and records an ``interrupted`` status so
        :meth:`resume_goal_loop` can continue it later.
        """
        task = self._goal_loop_task
        if task is None or task.done():
            return False
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        return True

    def _last_goal_loop_status(self) -> dict | None:
        """Return the most recent goal-status payload, or None if there is none."""
        conversation = self._conversation
        if conversation is None:
            return None
        with conversation._state:
            for event in reversed(list(conversation._state.events)):
                if (
                    isinstance(event, ConversationStateUpdateEvent)
                    and event.key == "goal"
                ):
                    return event.value if isinstance(event.value, dict) else None
        return None

    async def resume_goal_loop(
        self, *, judge_llm: LLM | None = None, max_iterations: int | None = None
    ) -> None:
        """Resume the last interrupted ``/goal`` loop in this conversation.

        Reconstructs the loop from the last persisted goal-status event and
        continues from the iteration it had reached. This works within a session
        and across a server restart because goal-status events are persisted.

        Raises:
            ValueError: If the service is inactive, a goal loop is already
                running, no judge LLM is available, or there is no resumable goal
                loop because none was started or it already completed/capped.
        """
        if not self._conversation or self._closing:
            raise ValueError("inactive_service")
        loop = asyncio.get_running_loop()
        last = await loop.run_in_executor(None, self._last_goal_loop_status)
        if last is None or last.get("status") in ("complete", "capped"):
            raise ValueError("no_resumable_goal")
        if judge_llm is None:
            judge_llm = getattr(self._conversation.agent, "llm", None)
        if judge_llm is None:
            raise ValueError("no_judge_llm")
        controller = GoalController(
            last["objective"],
            judge_llm,
            max_iterations=max_iterations or int(last["max_iterations"]),
        )
        controller.iteration = int(last["iteration"])
        # Same busy guard as start_goal_loop: refuse a goal loop or active run.
        async with self._run_lock:
            if self._closing:
                raise ValueError("inactive_service")
            if self._goal_loop_task is not None and not self._goal_loop_task.done():
                raise ValueError("goal_already_running")
            if (self._run_task is not None and not self._run_task.done()) or (
                await self._get_execution_status()
                == ConversationExecutionStatus.RUNNING
            ):
                raise ValueError("conversation_already_running")
            if self._closing:  # see start_goal_loop: close() may have begun teardown
                raise ValueError("inactive_service")
            self._goal_loop_outcome = None
            self._goal_loop_task = asyncio.create_task(
                self._run_goal_loop(controller, resume=True)
            )

    async def respond_to_confirmation(self, request: ConfirmationResponseRequest):
        if request.accept:
            try:
                await self.run()
            except ValueError as e:
                # Treat "already running" as a no-op success
                if str(e) == "conversation_already_running":
                    logger.debug(
                        "Confirmation accepted but conversation already running"
                    )
                else:
                    raise
        else:
            await self.reject_pending_actions(request.reason)

    async def reject_pending_actions(self, reason: str):
        """Reject all pending actions and publish updated state."""
        if not self._conversation:
            raise ValueError("inactive_service")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, self._conversation.reject_pending_actions, reason
        )

    async def pause(self):
        if self._conversation:
            self._explicit_interrupt_generation += 1
            self._rerun_requested = False
            self._acp_internal_rerun_requested = False
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._conversation.pause)
            # Publish state update after pause to ensure stats are updated
            await self._publish_state_update()

    async def interrupt(self, *, internal_acp_rerun: bool = False):
        """Immediately cancel an in-flight async LLM call.

        Delegates to :meth:`LocalConversation.interrupt` which cancels the
        ``arun()`` task.  If no async run is in progress the call falls
        back to :meth:`pause`.
        """
        if self._conversation:
            if not internal_acp_rerun:
                self._explicit_interrupt_generation += 1
                self._rerun_requested = False
                self._acp_internal_rerun_requested = False
            self._conversation.interrupt()
            # Wait for the run task to finish so we can publish the final
            # state update (PAUSED + InterruptEvent) cleanly. The shield keeps
            # the 5s timeout from force-cancelling a cleanup that still needs
            # to drain its ACP prompt/cancel handshake.
            if self._run_task is not None and not self._run_task.done():
                with suppress(Exception):
                    await asyncio.wait_for(asyncio.shield(self._run_task), timeout=5.0)
                # Only clear _run_task if it actually finished; if
                # wait_for timed out the task may still be running and
                # clearing prematurely would allow a second run() to
                # start while the first is still in progress.
                if self._run_task is not None and self._run_task.done():
                    self._run_task = None
            await self._publish_state_update()

    async def update_secrets(self, secrets: dict[str, SecretValue]):
        """Update secrets in the conversation."""
        if not self._conversation:
            raise ValueError("inactive_service")
        if CODEX_AUTH_SECRET_NAME in self.credential_bindings:
            secrets = dict(secrets)
            secrets.pop(CODEX_AUTH_SECRET_NAME, None)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._conversation.update_secrets, secrets)

    async def set_confirmation_policy(self, policy: ConfirmationPolicyBase):
        """Set the confirmation policy for the conversation."""
        if not self._conversation:
            raise ValueError("inactive_service")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, self._conversation.set_confirmation_policy, policy
        )

    async def set_security_analyzer(
        self, security_analyzer: SecurityAnalyzerBase | None
    ):
        """Set the security analyzer for the conversation."""
        if not self._conversation:
            raise ValueError("inactive_service")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, self._conversation.set_security_analyzer, security_analyzer
        )

    async def load_plugin(self, plugin_ref: str) -> None:
        """Load a marketplace plugin into the active conversation."""
        if self._conversation is None:
            raise ValueError("inactive_service")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._conversation.load_plugin, plugin_ref)

    async def switch_acp_model(self, model: str) -> None:
        """Switch the model on an ACP conversation.

        For a conversation that has already started, runs the (blocking)
        protocol-level ``session/set_model`` round-trip in a worker thread; for
        one not yet run, the SDK defers the switch (persist-only). Either way the
        switched model is persisted as the authoritative value in
        ``base_state.json``: ``LocalConversation.switch_acp_model`` sets
        ``state.agent`` to an agent copy carrying the new ``acp_model``, which the
        autosave path writes to base_state. On resume the agent is rebuilt from
        base_state (the single source of truth), so no ``meta.json`` mirror is
        needed.
        """
        if self._conversation is None:
            # Match the inactive-service convention of the other event-service
            # methods (the conversation router maps it to 400). The SDK no
            # longer raises for a created-but-not-yet-run conversation, so a
            # pre-first-run switch is a normal 200 deferral, not an error.
            raise ValueError("inactive_service")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._conversation.switch_acp_model, model)

    async def close(self):
        self._closing = True
        self._explicit_interrupt_generation += 1
        self._rerun_requested = False
        self._acp_internal_rerun_requested = False

        # Cancel any in-progress /goal loop first so it cannot start a new run
        # while we drain the current one below.
        if self._goal_loop_task is not None and not self._goal_loop_task.done():
            self._goal_loop_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._goal_loop_task
        self._goal_loop_task = None

        if self._lease_task is not None:
            self._lease_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._lease_task
            self._lease_task = None

        # Drain in-flight run before teardown so MCP close doesn't race
        # with a tool call mid-step.
        if self._run_task is not None and not self._run_task.done():
            if self._conversation is not None:
                loop = asyncio.get_running_loop()
                try:
                    await loop.run_in_executor(None, self._conversation.pause)
                except Exception:
                    logger.warning(
                        "Failed to pause conversation during close", exc_info=True
                    )
            # Cancel the run task so arun()'s CancelledError handler can
            # transition to PAUSED cleanly.  For the legacy thread-pool
            # path the underlying thread keeps running but the wrapper
            # task still settles, unblocking the wait below.
            self._run_task.cancel()
            try:
                await asyncio.wait_for(self._run_task, timeout=10.0)
            except asyncio.CancelledError:
                pass  # Expected after cancel()
            except Exception as exc:
                logger.warning("Run task did not exit cleanly during close: %s", exc)
            self._run_task = None

        await self._pub_sub.close()
        if self._conversation:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._conversation.close)
            self._conversation = None
        self.credential_bindings = {}

        if self._lease is not None and self._lease_generation is not None:
            self._lease.release(self._lease_generation)
        self._lease_generation = None
        self._lease = None

    async def generate_title(
        self, llm: "LLM | None" = None, max_length: int = 50
    ) -> str:
        """Generate a title for the conversation.

        Resolves the provided LLM via the conversation's registry if a usage_id is
        present, registering it if needed. Then delegates to LocalConversation in an
        executor to avoid blocking the event loop.
        """
        if not self._conversation:
            raise ValueError("inactive_service")

        resolved_llm = llm
        if llm is not None:
            usage_id = llm.usage_id
            try:
                resolved_llm = self._conversation.llm_registry.get(usage_id)
            except KeyError:
                self._conversation.llm_registry.add(llm)
                resolved_llm = llm

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._conversation.generate_title, resolved_llm, max_length
        )

    async def ask_agent(self, question: str) -> str:
        """Ask the agent a simple question without affecting conversation state.

        Delegates to LocalConversation in an executor to avoid blocking the event loop.
        """
        if not self._conversation:
            raise ValueError("inactive_service")

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._conversation.ask_agent, question)

    async def condense(self) -> None:
        """Force condensation of the conversation history.

        Delegates to LocalConversation in an executor to avoid blocking the event loop.
        """
        if not self._conversation:
            raise ValueError("inactive_service")

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._conversation.condense)

    async def navigate_to(self, event_id: str | None) -> None:
        """Move the conversation HEAD to an existing event (in-place re-root).

        Delegates to LocalConversation in an executor to avoid blocking the event loop.

        Raises:
            ValueError: If ``event_id`` is not ``None`` and not in the conversation.
        """
        if not self._conversation:
            raise ValueError("inactive_service")

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._conversation.navigate_to, event_id
        )

    def _get_agent_final_response_sync(self) -> str:
        """Extract the agent's final response from the conversation events.

        Reads directly from the EventLog without acquiring the state lock.
        EventLog reads are safe without the FIFOLock because events are
        append-only and immutable once written.
        """
        if not self._conversation:
            raise ValueError("inactive_service")
        return get_agent_final_response(self._conversation._state.events)

    async def get_agent_final_response(self) -> str:
        """Extract the agent's final response from the conversation events.

        Returns the text from the last FinishAction or agent MessageEvent,
        or empty string if no final response is found.
        """
        if not self._conversation:
            raise ValueError("inactive_service")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._get_agent_final_response_sync)

    @staticmethod
    def _scan_request_tail(
        events, boundary_index: int | None
    ) -> tuple[str | None, str | None, ConversationErrorInfo | None]:
        """Newest completed tool, newest event time and newest error after the
        consumption boundary. Stops at the boundary so a previous request's
        observation or error is not attributed to this one."""
        last_tool: str | None = None
        last_progress_at: str | None = None
        error: ConversationErrorInfo | None = None
        total = len(events)
        for position, event in enumerate(reversed(events)):
            index = total - 1 - position
            if boundary_index is not None and index <= boundary_index:
                break
            if last_progress_at is None:
                timestamp = getattr(event, "timestamp", None)
                if timestamp is not None:
                    last_progress_at = str(timestamp)
            # Any observation completes a tool attempt, including an error or
            # rejection: the last completed tool must not look like an older
            # successful one.
            if last_tool is None and isinstance(event, ObservationBaseEvent):
                last_tool = event.tool_name
            if error is None and isinstance(event, ConversationErrorEvent):
                error = ConversationErrorInfo(
                    code=str(event.code),
                    detail=str(event.detail)[:500],
                    # Carried through rather than dropped: the event has held
                    # this since it was added, and every caller downstream was
                    # reconstructing "is this retryable, is it a rate limit"
                    # from the code string instead. H9 item 1.
                    classification=event.classification,
                )
            if (
                last_tool is not None
                and error is not None
                and last_progress_at is not None
            ):
                break
        return last_tool, last_progress_at, error

    def _get_agent_response_result_sync(self) -> AgentResponseResult:
        """Build the request-scoped answer with its provenance and progress."""
        if not self._conversation:
            raise ValueError("inactive_service")
        state = self._conversation._state
        events = state.events
        result_state = derive_result_state(state)
        boundary = state.consumed_user_message_id
        # A recorded boundary that is not in the event log cannot scope
        # anything: the autosaved state and the event files disagree. Report
        # unavailable rather than a final "" that looks like a real answer.
        boundary_index = (
            index_of_event(events, boundary) if boundary is not None else None
        )
        if boundary is not None and boundary_index is None:
            result_state = AgentResponseState.UNAVAILABLE
        scoped = result_state in (
            AgentResponseState.FINAL,
            AgentResponseState.PARTIAL,
        )
        response: str | None = None
        if scoped or (
            result_state is AgentResponseState.UNAVAILABLE and boundary is None
        ):
            # Answered (scoped to the boundary), or a legacy session with no
            # boundary at all -- whose last answer is all there is. A pending
            # request gets null: extracting here would return the previous
            # request's answer across the boundary, the defect this removes.
            response = get_agent_final_response(events, after_id=boundary)
        if scoped:
            last_tool, last_progress_at, error = self._scan_request_tail(
                events, boundary_index
            )
        else:
            # No answer means no provenance to report. A previous request's
            # error or tool must not be presented as this request's.
            last_tool = last_progress_at = None
            error = None
        progress_age = _progress_age_seconds(last_progress_at)
        return AgentResponseResult(
            response=response,
            state=result_state,
            # The boundary describes an answer that exists. A pending request
            # has no answer, so naming the previous boundary here would claim
            # the wrong input.
            request_message_id=(
                boundary
                if result_state
                in (AgentResponseState.FINAL, AgentResponseState.PARTIAL)
                else None
            ),
            iterations_used=state.iterations_used,
            iterations_remaining=iterations_remaining(state),
            last_completed_tool=last_tool,
            last_progress_at=last_progress_at,
            progress_age_seconds=progress_age,
            error=error,
            # The summary belongs to the input it was written for. A later
            # request that never finalized must not inherit it.
            summary=(
                state.final_summary
                if boundary is not None and state.finalized_request_id == boundary
                else None
            ),
        )

    async def get_agent_response_result(self) -> AgentResponseResult:
        """Return the current request's answer, state, error and progress."""
        if not self._conversation:
            raise ValueError("inactive_service")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._get_agent_response_result_sync)

    async def finalize(self, *, summary: bool = False) -> AgentResponseResult:
        """Stop the run at a safe boundary and return the outcome it has.

        The barrier is the existing pause: it lets the in-flight step reach a
        safe boundary and blocks further tool starts. Cancellation is not
        rollback -- an external effect already started may still be running --
        so the result reports what is known rather than claiming a clean stop.

        The request is recorded against the consumed input, so repeating it
        returns the same outcome instead of re-running anything. The summary is
        opt-in and off by default; when it runs it is a tools-disabled call
        charged to the run's remaining allowance, and no summary is claimed
        when none could run.
        """
        if not self._conversation:
            raise ValueError("inactive_service")
        # Read before pausing: whether a run was in flight is what decides if
        # the agent has anything to be told about. An idle session finalized
        # here has nothing to explain, and a notice would invent a stop.
        run_was_in_flight = self._run_task is not None and not self._run_task.done()
        await self._pause_to_boundary()
        # PAUSED afterwards, not merely "a run existed": a run that completed
        # on its own during the pause window ends FINISHED, and telling the
        # agent that finalize stopped it would be false. PAUSED is what the
        # cooperative pause actually leaving behind looks like.
        if (
            run_was_in_flight
            and self._conversation._state.execution_status
            == ConversationExecutionStatus.PAUSED
        ):
            # `pause` is invisible to the agent (PauseEvent is not an
            # LLMConvertibleEvent), so without this a session stopped by
            # finalize and resumed later had no record of why its run ended.
            # Same blind spot the interrupt notice closes, with a cause this
            # caller actually knows.
            conversation = self._conversation
            with conversation._state:
                conversation.note_external_stop(
                    "The session was finalized, so the run stopped at a safe "
                    "boundary. Work already completed stands; give new input "
                    "to continue."
                )
        state = self._conversation._state
        boundary = state.consumed_user_message_id
        claimed = False
        if boundary is not None:
            # Claim the request under the lock and without an await in between,
            # so two concurrent finalize calls cannot both run the summary.
            with state:
                if state.finalized_request_id != boundary or state.finalized_at is None:
                    state.finalized_request_id = boundary
                    state.finalized_at = utc_now().isoformat()
                    claimed = True
        if claimed and boundary is not None and summary and finalize_summary_enabled():
            await self._run_final_summary(boundary)
        return await self.get_agent_response_result()

    async def _pause_to_boundary(self) -> None:
        """Pause until the session can no longer start new tools.

        ``pause`` is a no-op when the status already reads FINISHED, and
        FINISHED is provisional: a stop hook can deny the stop and put the run
        back to RUNNING, which would let tools start after finalize claimed to
        have stopped them. Re-pausing covers that window, bounded so a run that
        genuinely keeps going does not block the caller -- the result's own
        status then shows it is still running rather than claiming a stop.
        """
        for attempt in range(3):
            await self.pause()
            if self._run_task is None or self._run_task.done():
                return
            conversation = self._conversation
            if conversation is None:
                return
            status = conversation._state.execution_status
            if status not in (
                ConversationExecutionStatus.RUNNING,
                ConversationExecutionStatus.FINISHED,
            ):
                return
            if attempt < 2:
                await asyncio.sleep(0.2)

    async def _run_final_summary(self, boundary: str) -> None:
        """Run the opt-in tools-disabled wrap-up. Best-effort: never raises."""
        conversation = self._conversation
        if conversation is None:
            return
        state = conversation._state
        remaining = iterations_remaining(state)
        if remaining <= 0:
            logger.info(
                "finalize summary skipped for %s: no iteration allowance left",
                self.stored.id,
            )
            return
        if remaining == 1:
            logger.warning(
                "finalize summary takes the last iteration of the run for %s",
                self.stored.id,
            )
        llm = getattr(conversation.agent, "llm", None)
        if llm is None:
            return
        answer = get_agent_final_response(state.events, after_id=boundary)
        prompt = FINALIZE_SUMMARY_PROMPT.format(
            answer=(answer or "(none recorded)")[-4000:]
        )
        try:
            loop = asyncio.get_running_loop()
            text = await loop.run_in_executor(None, _finalize_completion, llm, prompt)
        except Exception:
            logger.warning(
                "finalize summary failed for %s", self.stored.id, exc_info=True
            )
            return
        if not text:
            return
        with state:
            state.final_summary = text
            # The call is charged to the same allowance the run was given.
            state.iterations_used = min(state.max_iterations, state.iterations_used + 1)

    async def get_state(self) -> ConversationState:
        if not self._conversation:
            raise ValueError("inactive_service")
        return self._conversation._state

    async def _publish_state_update(self):
        """Publish a ConversationStateUpdateEvent with the current state."""
        if not self._conversation:
            return

        state_update_event = await self._create_state_update_event()
        # Note: _pub_sub iterates through subscribers sequentially. If any subscriber
        # is slow, it will delay subsequent subscribers. For high-throughput scenarios,
        # consider using asyncio.gather() for concurrent notification in the future.
        await self._pub_sub(state_update_event)

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        save_error: BaseException | None = None
        try:
            await self.save_meta()
        except ConversationOwnershipLostError:
            logger.info(
                "Skipping meta save after ownership loss for conversation %s",
                self.stored.id,
            )
        except BaseException as exc:
            save_error = exc
        close_error: BaseException | None = None
        try:
            await self.close()
        except BaseException as exc:
            close_error = exc
        if isinstance(close_error, CredentialBindingError):
            raise close_error
        if save_error is not None:
            if close_error is not None:
                logger.warning(
                    "Event service close also failed after meta save failure",
                    exc_info=(
                        type(close_error),
                        close_error,
                        close_error.__traceback__,
                    ),
                )
            raise save_error
        if close_error is not None:
            raise close_error

    def is_open(self) -> bool:
        return bool(self._conversation)

    def touch(self) -> None:
        """Record activity so idle-eviction defers this conversation."""
        self._last_active_monotonic = time.monotonic()

    def idle_seconds(self) -> float:
        """Seconds since the last recorded activity."""
        return time.monotonic() - self._last_active_monotonic

    def mark_subscription_baseline(self) -> None:
        """Snapshot the current (internal) subscribers; later ones are external."""
        self._internal_subscriber_ids = self._pub_sub.subscriber_ids()

    def has_external_subscribers(self) -> bool:
        """True if a non-internal subscriber (e.g. a websocket) is attached."""
        return bool(self._pub_sub.subscriber_ids() - self._internal_subscriber_ids)

    def is_idle_evictable(self) -> bool:
        """Safe to evict only with no in-flight work and no external subscriber."""
        run_active = self._run_task is not None and not self._run_task.done()
        goal_active = (
            self._goal_loop_task is not None and not self._goal_loop_task.done()
        )
        if (
            self._closing
            or run_active
            or goal_active
            or self._rerun_requested
            or self._acp_internal_rerun_requested
            or self.has_external_subscribers()
        ):
            return False
        return True
