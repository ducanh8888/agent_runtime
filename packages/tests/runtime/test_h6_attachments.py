"""H6: image attachments at dispatch, and the guards on them."""

from __future__ import annotations

import base64
import json
import types
from collections.abc import Callable

import httpx
import pytest

from agentrt.runtime import bootstrap, client as client_mod, config


CREATED = "77777777-7777-7777-7777-777777777777"
#: One pixel PNG, so the sniffing path is exercised on real image bytes.
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
Handler = Callable[[httpx.Request], httpx.Response]


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


def _capturing_client() -> tuple[client_mod.Client, dict]:
    seen: dict = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            201,
            json={"id": CREATED, "execution_status": "idle", "tags": {}},
        )

    return _mock_client(handle), seen


def test_attachment_is_sent_as_a_typed_image_block(tmp_path) -> None:
    image = tmp_path / "shot.png"
    image.write_bytes(PNG_BYTES)
    client, seen = _capturing_client()

    client.dispatch("describe this", str(tmp_path), attachments=[str(image)])

    content = seen["body"]["initial_message"]["content"]
    assert content[0] == {"type": "text", "text": "describe this"}
    assert content[1]["type"] == "image"
    url = content[1]["image_urls"][0]
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == PNG_BYTES


def test_attachment_outside_the_workspace_is_refused(tmp_path) -> None:
    outside = tmp_path.parent / "outside.png"
    outside.write_bytes(PNG_BYTES)
    client, seen = _capturing_client()

    with pytest.raises(
        client_mod.ClientError, match="not readable from this workspace"
    ):
        client.dispatch("t", str(tmp_path), attachments=[str(outside)])

    assert "body" not in seen  # nothing was submitted


def test_a_non_image_is_refused_by_its_bytes_not_its_name(tmp_path) -> None:
    impostor = tmp_path / "shot.png"
    impostor.write_text("not an image at all")
    client, _ = _capturing_client()

    with pytest.raises(client_mod.ClientError, match="not a PNG, JPEG, GIF or WebP"):
        client.dispatch("t", str(tmp_path), attachments=[str(impostor)])


def test_an_oversized_attachment_is_refused(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(client_mod, "MAX_ATTACHMENT_BYTES", 4)
    image = tmp_path / "shot.png"
    image.write_bytes(PNG_BYTES)
    client, _ = _capturing_client()

    with pytest.raises(client_mod.ClientError, match="above the 4 byte cap"):
        client.dispatch("t", str(tmp_path), attachments=[str(image)])


def test_usage_is_read_through(tmp_path) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/usage")
        return httpx.Response(
            200,
            json={
                "conversation_id": CREATED,
                "services": [
                    {
                        "service_id": "worker",
                        "raw": {"prompt_tokens": 10, "reasoning_tokens": 0},
                    }
                ],
            },
        )

    client = _mock_client(handle)

    payload = client.usage(CREATED)

    assert payload["services"][0]["service_id"] == "worker"
    # Zero is reported as zero; an absent field is not the same thing.
    assert payload["services"][0]["raw"]["reasoning_tokens"] == 0


def test_broad_permission_does_not_widen_the_attachment_root(tmp_path) -> None:
    """A broad session still may not attach a file from outside its workspace."""
    outside = tmp_path.parent / "elsewhere.png"
    outside.write_bytes(PNG_BYTES)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    client, seen = _capturing_client()

    with pytest.raises(
        client_mod.ClientError, match="not readable from this workspace"
    ):
        client.dispatch(
            "t", str(workspace), permission="broad", attachments=[str(outside)]
        )

    assert "body" not in seen


def test_dispatch_from_forks_then_sends() -> None:
    seen: list[tuple[str, str]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path.endswith("/fork"):
            return httpx.Response(
                200,
                json={"id": CREATED, "execution_status": "idle", "title": "forked"},
            )
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json={"success": True})
        return httpx.Response(
            200, json={"id": CREATED, "execution_status": "idle", "tags": {}}
        )

    client = _mock_client(handle)
    out = client.dispatch_from(
        "11111111-1111-1111-1111-111111111111", "review the change"
    )

    assert (
        "POST",
        "/api/conversations/11111111-1111-1111-1111-111111111111/fork",
    ) in seen
    assert ("POST", f"/api/conversations/{CREATED}/events") in seen
    assert out["id"] == CREATED
    assert out["forked_from"] == "11111111-1111-1111-1111-111111111111"
