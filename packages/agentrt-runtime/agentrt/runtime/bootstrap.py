"""Create the daemon's named profiles from the operator's `.env`.

This is deliberately not delegated to a model and deliberately small: it is the
one place in the runtime that puts the provider credential on disk, and the one
place where a subtle mistake leaks it.

The shape matters. `POST /api/conversations` requires one of `agent`,
`agent_settings` or `agent_profile_id`. The first two would carry the LLM
configuration -- and therefore the key -- from whoever dispatches. Since the
dispatcher is an MCP process spawned by Claude Code, that would put the
credential a process boundary closer to the model than it needs to be. A named
profile keeps it here: dispatch sends a UUID, and the daemon resolves it against
files only it reads.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from uuid import UUID

from pydantic import SecretStr

from agentrt.runtime import config, permissions


LLM_PROFILE_NAME = "default"
AGENT_PROFILE_NAME = "default"

#: Fixed direct-DeepSeek policy for every AgentRT LLM service. The deployment
#: is DeepSeek-only with thinking enabled and effort "high"; there is no
#: operator effort selector and no silent downgrade to a weaker request. These
#: are written into the saved profile so the contract is inspectable, not just
#: implied by the SDK's detection of the model name. See H0 in
#: docs/DEEPSEEK_HARDENING_PLAN.md.
DIRECT_LLM_USAGE_ID = "agent"
DIRECT_LLM_API_MODE = "chat"
DIRECT_LLM_REASONING_EFFORT = "high"
DIRECT_LLM_CAPABILITY_OVERRIDES: dict[str, bool | str] = {
    "supports_reasoning_effort": True,
    "thinking_mode": "enabled",
    # Chat Completions only. Responses mode has no contract tests in this pass.
    "supports_responses_api": False,
}
DIRECT_LLM_EXTRA_BODY: dict[str, object] = {"thinking": {"type": "enabled"}}


class ProviderLinkedProfileError(RuntimeError):
    """Refuse to edit an LLM profile whose endpoint and key come from elsewhere.

    A profile with ``provider_connection_id`` stores no endpoint or key of its
    own; both are resolved from the shared provider connection every time the
    profile is loaded. Rewriting the model or policy while leaving that link in
    place would let apply report a direct endpoint while the request still
    routes through the linked provider. Attaching the direct endpoint and key
    would instead mean detaching the connection, which must be its own explicit,
    credential-safe operation, not a side effect of applying a policy. So this
    runtime refuses both preview and apply for such a target rather than making
    a claim it cannot honour.
    """


def _linked_provider_error(name: str, connection_id: str) -> ProviderLinkedProfileError:
    return ProviderLinkedProfileError(
        f"LLM profile {name!r} is linked to provider connection "
        f"{connection_id!r}; its endpoint and key are resolved from that "
        "connection, so the direct DeepSeek endpoint and key in the resolved "
        "configuration cannot be applied without detaching it. Refusing rather "
        "than reporting a change that would not take effect. Detach the "
        "provider connection explicitly first if direct access is intended."
    )


def _use_state_dir() -> Path:
    """Point the vendored stores at our state directory.

    The SDK defaults to ``~/.agentrt`` on every platform while
    ``config.state_dir()`` follows the Windows convention, so without this the
    client would write profiles somewhere the daemon never reads.
    """
    state = config.state_dir()
    os.environ["AGENTRT_PERSISTENCE_DIR"] = str(state)
    return state


def _build_llm(router: config.RouterConfig):
    """Build the saved LLM profile with the fixed direct-DeepSeek policy.

    The model id is prefixed with ``openai/`` so LiteLLM routes it to the
    OpenAI-compatible Chat Completions path instead of trying to infer a
    provider from a name it has never seen. ``api_mode`` and the capability
    overrides pin that path and the thinking/effort policy explicitly, so the
    profile does not depend on an alias being recognized by name detection.
    """
    from agentrt.sdk.llm import LLM

    return LLM(
        model=f"openai/{router.model}",
        base_url=router.base_url,
        api_key=SecretStr(router.api_key),
        usage_id=DIRECT_LLM_USAGE_ID,
        api_mode=DIRECT_LLM_API_MODE,
        reasoning_effort=DIRECT_LLM_REASONING_EFFORT,
        capability_overrides=dict(DIRECT_LLM_CAPABILITY_OVERRIDES),
        litellm_extra_body=dict(DIRECT_LLM_EXTRA_BODY),
    )


GUARD_MODULE = "agentrt.runtime.guarded_tools"


def _tools_for(preset: permissions.Permission):
    """Build the tool specs a preset grants.

    Round 7 fixed the set at terminal, file editor and task tracker: browser
    drags in Playwright and has the weakest cancellation story of any executor,
    and sub-agents are attached per dispatch, never by default. A preset narrows
    that set; it never widens it.

    The permission travels as a tool parameter rather than as daemon-wide state
    so two sessions with different presets can run at once.
    """
    from agentrt.sdk.tool import Tool
    from agentrt.tools.preset.default import register_default_tools

    # Registers terminal / file_editor / task_tracker under their own names.
    # The guarded file editor replaces one of them, so this has to run first.
    register_default_tools(enable_browser=False)

    specs = []
    for name in permissions.tools_for(preset):
        if name == "file_editor":
            specs.append(Tool(name=name, params={"permission": preset}))
        else:
            specs.append(Tool(name=name))
    return specs


def _constrain_switch_llm(store, profile):
    """Force the worker's own LLM-profile switch off for an AgentRT preset.

    A preset that can switch its own LLM profile can point a worker at any
    saved profile, which is exactly the bypass the fixed direct/high policy is
    meant to prevent. Only instances that carry the setting are touched, and
    only when it is currently on, so a restart does not rewrite unchanged
    profiles. ``model_copy`` carries over the id, tools, permissions and every
    unrelated field; already-created sessions hold their own resolved policy
    and are never retargeted here.
    """
    from agentrt.sdk.profiles.agent_profile import OpenHandsAgentProfile

    if not isinstance(profile, OpenHandsAgentProfile):
        return profile
    if profile.enable_switch_llm_tool is False:
        return profile
    constrained = profile.model_copy(update={"enable_switch_llm_tool": False})
    store.save(constrained)
    return constrained


def ensure_profiles(*, force: bool = False) -> dict[str, UUID]:
    """Create the LLM and agent profiles if absent; return id per preset.

    One agent profile per permission preset, named for it. Existing profiles are
    left alone unless ``force`` is set, so restarting the daemon never silently
    discards configuration. Both paths also migrate the three AgentRT presets to
    ``enable_switch_llm_tool=False``: a profile created before that constraint
    existed must not let new workers switch to an unapproved saved profile.
    """
    from agentrt.agent_server.persistence import (
        get_agent_profile_store,
        get_llm_profile_store,
    )
    from agentrt.sdk.profiles.agent_profile import OpenHandsAgentProfile

    state = _use_state_dir()
    agent_store = get_agent_profile_store()

    if not force:
        # ``list()`` returns file names, not objects, so each profile has to be
        # loaded to learn its id. All or nothing: a partial set means a preset
        # added since the last run is missing, and dispatching to it would fail
        # at the point of use rather than here.
        try:
            loaded = {
                preset: agent_store.load(preset) for preset in permissions.PRESETS
            }
        except Exception:
            pass
        else:
            return {
                preset: _constrain_switch_llm(agent_store, profile).id
                for preset, profile in loaded.items()
            }

    router = config.load_router_config()

    # Use the server's own factory rather than constructing the store with a
    # base_dir. The factory puts profiles under state/profiles/, and a store
    # built by hand lands them one directory up, where the daemon never looks.
    llm_store = get_llm_profile_store()
    llm_store.save(LLM_PROFILE_NAME, _build_llm(router), include_secrets=True)
    _tighten(state / "profiles" / f"{LLM_PROFILE_NAME}.json")

    # The credential is in the state directory's .env as well as in the profile
    # the daemon writes, and only the latter was being tightened. On POSIX the
    # .env kept the default umask, so the file the runtime actually reads its
    # key from stayed group- and world-readable while the copy beside it was
    # locked to owner-only.
    _tighten(config.config_file())

    ids: dict[str, UUID] = {}
    for preset in permissions.PRESETS:
        # Reuse the existing id. `OpenHandsAgentProfile.id` is documented as a
        # "stable provenance handle ... it never changes", conversations record
        # it, and an orchestrator may be holding one from an earlier `profiles`
        # call. Constructing a fresh profile mints a new UUID, so rebuilding
        # because *one* preset was missing silently re-identified the other two.
        #
        # Reuse the existing id, but never its switch_llm setting: a rebuild is
        # the migration path for a profile that predates the constraint, so it
        # must end with the tool off rather than carrying the bypass forward.
        kwargs: dict = {}
        try:
            existing = agent_store.load(preset)
        except Exception:
            pass  # Genuinely new; let the default factory mint one.
        else:
            kwargs["id"] = existing.id

        profile = OpenHandsAgentProfile(
            name=preset,
            llm_profile_ref=LLM_PROFILE_NAME,
            tools=_tools_for(preset),
            enable_sub_agents=False,
            enable_switch_llm_tool=False,
            **kwargs,
        )
        agent_store.save(profile)
        ids[preset] = profile.id
    return ids


def _tighten(path: Path) -> None:
    """Owner-only permissions on a file holding the credential.

    Called for every such file, which is the point: an earlier version named
    only the profile the daemon writes and left the operator's own .env at the
    default umask.

    A no-op on Windows, where the POSIX mode bits carry no meaning; the file is
    inside the user profile there and inherits its ACL.
    """
    if os.name != "nt" and path.exists():
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def allowed_llm_profiles() -> list[str]:
    """Names of the LLM profiles the runtime's agent profiles reference.

    Dispatch may select one of these and refuses anything else rather than
    silently falling back. Names only: no model, endpoint or key is returned.
    """
    from agentrt.agent_server.persistence import get_agent_profile_store
    from agentrt.sdk.profiles.agent_profile import OpenHandsAgentProfile

    _use_state_dir()
    store = get_agent_profile_store()
    refs: set[str] = set()
    for preset in permissions.PRESETS:
        try:
            profile = store.load(preset)
        except Exception:
            continue
        if isinstance(profile, OpenHandsAgentProfile):
            refs.add(profile.llm_profile_ref)
    return sorted(refs or {LLM_PROFILE_NAME})


def agent_profile_llm_ref(permission: permissions.Permission) -> str | None:
    """The LLM profile reference a permission preset's agent profile uses."""
    from agentrt.agent_server.persistence import get_agent_profile_store
    from agentrt.sdk.profiles.agent_profile import OpenHandsAgentProfile

    _use_state_dir()
    try:
        profile = get_agent_profile_store().load(permission)
    except Exception:
        return None
    if isinstance(profile, OpenHandsAgentProfile):
        return profile.llm_profile_ref
    return None


def _describe_llm(llm) -> dict:
    """Secret-free description of one LLM profile."""
    return {
        "model": llm.model,
        "base_url": llm.base_url,
        "reasoning_effort": llm.reasoning_effort,
        "usage_id": llm.usage_id,
        "api_mode": llm.api_mode,
        "capability_overrides": llm.capability_overrides,
        "litellm_extra_body": llm.litellm_extra_body,
        "api_key_present": llm.api_key is not None,
        "provider_connection_id": llm.provider_connection_id,
    }


#: Fields the direct/high contract owns; changed together so apply cannot leave
#: a stale endpoint beside a new policy.
_POLICY_FIELDS = (
    "model",
    "reasoning_effort",
    "usage_id",
    "api_mode",
    "capability_overrides",
    "litellm_extra_body",
)


def _profile_changes(current, proposed) -> list[dict]:
    """Endpoint/model/policy differences, with the key reported as presence."""
    changes: list[dict] = []
    fields = ("base_url", *_POLICY_FIELDS)
    for field in fields:
        before = getattr(current, field, None) if current is not None else None
        after = getattr(proposed, field)
        if before != after:
            changes.append({"field": field, "from": before, "to": after})
    before_key = current is not None and current.api_key is not None
    after_key = proposed.api_key is not None
    if before_key != after_key:
        changes.append(
            {
                "field": "api_key",
                "from": "present" if before_key else "absent",
                "to": "present" if after_key else "absent",
            }
        )
    return changes


def _merge_llm(current, proposed):
    """Update the endpoint, model and full policy, preserving unrelated fields.

    Everything in ``_POLICY_FIELDS`` is refreshed together: a partial update
    would leave an old effort or api_mode next to a new model, which is exactly
    the inconsistency that reads as "the policy is applied" while the request
    still goes out weaker. ``base_url`` and ``api_key`` are always written,
    because callers refuse a provider-linked target up front -- there is no
    path here that silently keeps another endpoint.
    """
    update: dict = {field: getattr(proposed, field) for field in _POLICY_FIELDS}
    update["base_url"] = proposed.base_url
    update["api_key"] = proposed.api_key
    return current.model_copy(update=update)


def preview_llm_profile(
    router: config.RouterConfig | None = None,
    *,
    name: str = LLM_PROFILE_NAME,
) -> dict:
    """Describe what applying the resolved config would change, writing nothing."""
    from agentrt.agent_server.persistence import get_llm_profile_store

    router = router or config.load_router_config()
    _use_state_dir()
    store = get_llm_profile_store()
    proposed = _build_llm(router)
    try:
        current = store.load(name, resolve_provider=False)
    except FileNotFoundError:
        current = None
    if current is not None and current.provider_connection_id:
        # Do not describe endpoint/key changes that apply would not make.
        raise _linked_provider_error(name, current.provider_connection_id)
    return {
        "profile": name,
        "exists": current is not None,
        "current": _describe_llm(current) if current is not None else None,
        "proposed": _describe_llm(proposed),
        "changes": _profile_changes(current, proposed),
        "applies_to": "new sessions only",
        "note": (
            "Preview only; nothing was written. Applying updates the saved LLM "
            "profile for sessions created afterwards. Editing .env alone does "
            "not change a saved profile or an already-created session."
        ),
    }


def apply_llm_profile(
    router: config.RouterConfig | None = None,
    *,
    name: str = LLM_PROFILE_NAME,
) -> dict:
    """Update one saved LLM profile from the resolved config.

    Only that profile is rewritten: agent profiles and their stable ids,
    permission settings and unrelated LLM fields are preserved. Existing
    sessions are never retargeted.
    """
    from agentrt.agent_server.persistence import get_llm_profile_store

    router = router or config.load_router_config()
    state = _use_state_dir()
    store = get_llm_profile_store()
    proposed = _build_llm(router)
    try:
        current = store.load(name, resolve_provider=False)
    except FileNotFoundError:
        current = None
    if current is not None and current.provider_connection_id:
        raise _linked_provider_error(name, current.provider_connection_id)

    store.save(
        name,
        proposed if current is None else _merge_llm(current, proposed),
        include_secrets=True,
    )
    _tighten(state / "profiles" / f"{name}.json")

    return {
        "profile": name,
        "applied": True,
        "changes": _profile_changes(current, proposed),
        "agent_profiles_unchanged": True,
        "applies_to": "new sessions only",
        "note": (
            "Saved profile updated. Already-created sessions keep the policy "
            "they launched with and are not retargeted on resume."
        ),
    }


def summary() -> dict:
    """Report what is configured without revealing the credential."""
    from agentrt.agent_server.persistence import (
        get_agent_profile_store,
        get_llm_profile_store,
    )

    state = _use_state_dir()
    out: dict = {
        "state_dir": str(state),
        "runtime_version": config.runtime_version(),
    }
    try:
        out["settings"] = config.describe_settings()
    except config.ConfigConflictError as exc:
        out["settings"] = None
        out["settings_error"] = str(exc)
    out["allowed_llm_profiles"] = allowed_llm_profiles()
    try:
        llm = get_llm_profile_store().load(LLM_PROFILE_NAME)
        out["llm_profile"] = LLM_PROFILE_NAME
        out["model"] = llm.model
        out["base_url"] = llm.base_url
        out["api_key_present"] = llm.api_key is not None
    except Exception:
        out["llm_profile"] = None
    store = get_agent_profile_store()
    out["permissions"] = {}
    for preset in permissions.PRESETS:
        try:
            profile = store.load(preset)
        except Exception:
            out["permissions"][preset] = None
            continue
        out["permissions"][preset] = {
            "id": str(profile.id),
            "tools": [t.name for t in (getattr(profile, "tools", None) or [])],
            "description": permissions.DESCRIPTIONS[preset],
        }
    out["default_permission"] = permissions.DEFAULT_PERMISSION
    return out
