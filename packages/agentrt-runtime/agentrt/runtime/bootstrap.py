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

from agentrt.runtime import config

LLM_PROFILE_NAME = "default"
AGENT_PROFILE_NAME = "default"


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
    """The model id is prefixed with ``openai/`` so LiteLLM routes it to the
    OpenAI-compatible path instead of trying to infer a provider from a name it
    has never seen. 9Router speaks that protocol for every model it serves.
    """
    from agentrt.sdk.llm import LLM

    return LLM(
        model=f"openai/{router.model}",
        base_url=router.base_url,
        api_key=SecretStr(router.api_key),
        service_id="agent",
    )


def ensure_profiles(*, force: bool = False) -> UUID:
    """Create the LLM and agent profiles if absent; return the agent profile id.

    Existing profiles are left alone unless ``force`` is set, so restarting the
    daemon never silently discards configuration.
    """
    from agentrt.agent_server.persistence import (
        get_agent_profile_store,
        get_llm_profile_store,
    )
    from agentrt.sdk.profiles.agent_profile import OpenHandsAgentProfile
    from agentrt.tools.preset.default import get_default_tools

    state = _use_state_dir()

    agent_store = get_agent_profile_store()
    if not force:
        # ``list()`` returns file names, not objects, so the profile has to be
        # loaded to learn its id.
        try:
            return agent_store.load(AGENT_PROFILE_NAME).id
        except Exception:
            pass

    router = config.load_router_config()

    # Use the server's own factory rather than constructing the store with a
    # base_dir. The factory puts profiles under state/profiles/, and a store
    # built by hand lands them one directory up, where the daemon never looks.
    llm_store = get_llm_profile_store()
    llm_store.save(LLM_PROFILE_NAME, _build_llm(router), include_secrets=True)
    _tighten(state / "profiles" / f"{LLM_PROFILE_NAME}.json")

    profile = OpenHandsAgentProfile(
        name=AGENT_PROFILE_NAME,
        llm_profile_ref=LLM_PROFILE_NAME,
        # Round 7: terminal, file editor and task tracker only. Browser drags in
        # Playwright and has the weakest cancellation story of any executor;
        # sub-agents are attached per dispatch, never by default.
        tools=get_default_tools(enable_browser=False, enable_sub_agents=False),
        enable_sub_agents=False,
    )
    agent_store.save(profile)
    return profile.id


def _tighten(path: Path) -> None:
    """Owner-only permissions on anything holding the credential.

    A no-op on Windows, where the POSIX mode bits carry no meaning; the file is
    inside the user profile there and inherits its ACL.
    """
    if os.name != "nt" and path.exists():
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def summary() -> dict:
    """Report what is configured without revealing the credential."""
    from agentrt.agent_server.persistence import (
        get_agent_profile_store,
        get_llm_profile_store,
    )

    state = _use_state_dir()
    out: dict = {"state_dir": str(state)}
    try:
        llm = get_llm_profile_store().load(LLM_PROFILE_NAME)
        out["llm_profile"] = LLM_PROFILE_NAME
        out["model"] = llm.model
        out["base_url"] = llm.base_url
        out["api_key_present"] = llm.api_key is not None
    except Exception:
        out["llm_profile"] = None
    try:
        profile = get_agent_profile_store().load(AGENT_PROFILE_NAME)
        out["agent_profile"] = profile.name
        out["agent_profile_id"] = str(profile.id)
        out["llm_profile_ref"] = profile.llm_profile_ref
        out["tools"] = [t.name for t in (profile.tools or [])]
    except Exception:
        out["agent_profile"] = None
    return out
