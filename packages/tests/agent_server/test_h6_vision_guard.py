"""H6: an image attachment is refused when the model cannot see it."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentrt.agent_server.deployment_policy import (
    DeploymentPolicyError,
    refuse_unsupported_attachments,
)
from agentrt.sdk.llm import ImageContent, TextContent


IMAGE = ImageContent(image_urls=["data:image/png;base64,AAAA"])
TEXT = TextContent(text="just words")


def test_an_image_without_vision_is_refused() -> None:
    llm = SimpleNamespace(vision_is_active=lambda: False)

    with pytest.raises(DeploymentPolicyError, match="no active vision capability"):
        refuse_unsupported_attachments([TEXT, IMAGE], llm)


def test_an_image_with_vision_is_allowed() -> None:
    llm = SimpleNamespace(vision_is_active=lambda: True)

    refuse_unsupported_attachments([TEXT, IMAGE], llm)


def test_text_only_content_is_untouched() -> None:
    llm = SimpleNamespace(vision_is_active=lambda: False)

    refuse_unsupported_attachments([TEXT], llm)
    refuse_unsupported_attachments([], llm)
