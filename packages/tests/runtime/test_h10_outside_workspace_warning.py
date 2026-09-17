"""H10 item 4: a cheap, warn-only alternative to a second read-only root.

"Compare repo A against repo B" dispatched into only one of them otherwise
surfaces as refused-path retries deep into the run; this catches the common
phrasing of that mistake at dispatch time, without blocking the dispatch or
widening what the session can actually reach.
"""

from __future__ import annotations

import json
import types
from collections.abc import Callable

import httpx
import pytest

from agentrt.runtime import bootstrap, client as client_mod, config


Handler = Callable[[httpx.Request], httpx.Response]
CREATED = "88888888-8888-8888-8888-888888888888"


@pytest.fixture(autouse=True)
def _no_client_side_cap(monkeypatch):
    monkeypatch.setattr(config, "max_running_sessions", lambda: 0)
    monkeypatch.setattr(
        bootstrap, "agent_profile_llm_ref", lambda preset: "deepseek-high"
    )


def _mock_client(handler: Handler) -> client_mod.Client:
    client = client_mod.Client()
    setattr(
        client,
        "_daemon_info",
        types.SimpleNamespace(base_url="http://daemon.test", token="test-token"),
    )
    client._profile_ref = {}
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    client._profile_id = lambda preset, llm_profile: (
        "00000000-0000-0000-0000-000000000001"
    )
    return client


def _handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    return httpx.Response(
        200,
        json={
            "id": CREATED,
            "execution_status": "idle",
            "tags": body.get("tags", {}),
        },
    )


def test_flags_a_path_outside_the_workspace(tmp_path) -> None:
    client = _mock_client(_handler)
    workspace = tmp_path / "a"
    workspace.mkdir()
    other = tmp_path / "b"

    out = client.dispatch(f"compare this against {other}", str(workspace))

    assert out["outside_workspace_paths"] == [str(other)]


def test_no_warning_when_nothing_is_mentioned(tmp_path) -> None:
    client = _mock_client(_handler)
    workspace = tmp_path / "a"
    workspace.mkdir()

    out = client.dispatch("summarise src/app.py", str(workspace))

    assert "outside_workspace_paths" not in out


def test_no_warning_for_a_path_inside_the_workspace(tmp_path) -> None:
    client = _mock_client(_handler)
    workspace = tmp_path / "a"
    workspace.mkdir()
    (workspace / "src").mkdir()

    out = client.dispatch(f"look at {workspace / 'src' / 'app.py'}", str(workspace))

    assert "outside_workspace_paths" not in out


def test_does_not_flag_a_url_path(tmp_path) -> None:
    """A GitHub URL's path segment (`/org/repo`) is not a filesystem path,
    and must not be resolved and flagged as one outside the workspace."""
    client = _mock_client(_handler)
    workspace = tmp_path / "a"
    workspace.mkdir()

    out = client.dispatch(
        "read the readme at https://github.com/org/repo/blob/main/README.md",
        str(workspace),
    )

    assert "outside_workspace_paths" not in out


def test_dispatch_never_blocks_on_the_warning(tmp_path) -> None:
    """The scan is advisory only -- the dispatch still goes through."""
    client = _mock_client(_handler)
    workspace = tmp_path / "a"
    workspace.mkdir()

    out = client.dispatch("compare against /some/other/repo", str(workspace))

    assert out["id"] == CREATED
    assert out["outside_workspace_paths"] == ["/some/other/repo"]
