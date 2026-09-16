"""Tests for conversation tags validation and integration."""

import pytest
from pydantic import ValidationError

from agentrt.sdk.conversation.types import (
    TAG_VALUE_MAX_LENGTH,
    ConversationObservabilityMetadata,
    ConversationObservabilityTags,
    ConversationTags,
    _validate_observability_metadata,
    _validate_tags,
)


def test_validate_tags_valid():
    tags = {"env": "production", "team": "backend", "priority": "high"}
    result = _validate_tags(tags)
    assert result == tags


def test_validate_tags_none_returns_empty():
    assert _validate_tags(None) == {}


def test_validate_tags_empty_dict():
    assert _validate_tags({}) == {}


def test_validate_tags_invalid_key_uppercase():
    with pytest.raises(ValueError, match="lowercase alphanumeric"):
        _validate_tags({"Env": "prod"})


def test_validate_tags_hyphenated_key_is_accepted():
    """H8 item 11 widened the charset to allow hyphens mid-key.

    This test used to assert the opposite -- that ``my-key`` raises -- which was
    the pre-H8 rule. It stayed behind when ``TAG_KEY_PATTERN`` was widened to
    ``^[a-z0-9]+(?:-[a-z0-9]+)*$`` (matching the existing PLUGIN_NAME_PATTERN
    precedent), so it was the one test asserting a rule the shipped code had
    deliberately stopped enforcing. The plan doc's note that it had checked
    "the existing invalid-key fixture" pointed at
    ``test_conversation_tags.py``, which fails on uppercase and so was
    unaffected -- this file's fixture was not looked at.
    """
    assert _validate_tags({"my-key": "value"}) == {"my-key": "value"}
    assert _validate_tags({"a-b-c": "value"}) == {"a-b-c": "value"}


@pytest.mark.parametrize(
    "key",
    ["-leading", "trailing-", "doubled--hyphen", "-"],
)
def test_validate_tags_malformed_hyphen_key_is_still_rejected(key):
    """Widening the charset kept the separators well-formed: a hyphen must sit
    between alphanumeric runs, so these are still invalid."""
    with pytest.raises(ValueError, match="lowercase alphanumeric"):
        _validate_tags({key: "value"})


def test_validate_tags_invalid_key_with_underscore():
    with pytest.raises(ValueError, match="lowercase alphanumeric"):
        _validate_tags({"my_key": "value"})


def test_validate_tags_invalid_key_with_spaces():
    with pytest.raises(ValueError, match="lowercase alphanumeric"):
        _validate_tags({"my key": "value"})


def test_validate_tags_value_max_length():
    long_value = "x" * TAG_VALUE_MAX_LENGTH
    result = _validate_tags({"key": long_value})
    assert result["key"] == long_value


def test_validate_tags_value_exceeds_max_length():
    long_value = "x" * (TAG_VALUE_MAX_LENGTH + 1)
    with pytest.raises(ValueError, match="exceeds maximum length"):
        _validate_tags({"key": long_value})


def test_validate_tags_numeric_key():
    result = _validate_tags({"123": "value"})
    assert result == {"123": "value"}


def test_validate_tags_alphanumeric_key():
    result = _validate_tags({"abc123": "value"})
    assert result == {"abc123": "value"}


def test_tags_in_pydantic_model():
    """Test that ConversationTags works as a Pydantic field type."""
    from pydantic import BaseModel

    class TestModel(BaseModel):
        tags: ConversationTags = {}

    # Valid tags
    m = TestModel(tags={"env": "prod"})
    assert m.tags == {"env": "prod"}

    # None coerced to empty dict by the BeforeValidator
    m = TestModel.model_validate({"tags": None})
    assert m.tags == {}

    # Invalid key rejected
    with pytest.raises(ValidationError):
        TestModel(tags={"BAD": "value"})


def test_validate_observability_metadata_valid():
    metadata = {
        "repo_name": "OpenHands/software-agent-sdk",
        "private": True,
        "retry_count": 3,
        "cost": 1.5,
        "labels": ["repo", "cloud"],
        "flags": [True, False],
        "counts": [1, 2],
        "scores": [0.1, 0.2],
    }

    assert _validate_observability_metadata(metadata) == metadata


def test_validate_observability_metadata_rejects_non_dict():
    with pytest.raises(ValueError, match="must be a dictionary"):
        _validate_observability_metadata([])


def test_validate_observability_metadata_rejects_nested_values():
    with pytest.raises(ValueError, match="must be a scalar"):
        _validate_observability_metadata({"repo": {"name": "openhands"}})  # type: ignore[dict-item]


def test_validate_observability_metadata_rejects_mixed_numeric_list():
    with pytest.raises(ValueError, match="mixed numeric types"):
        _validate_observability_metadata({"scores": [1, 1.5]})  # type: ignore[dict-item]


def test_observability_metadata_in_pydantic_model():
    from pydantic import BaseModel

    class TestModel(BaseModel):
        observability_metadata: ConversationObservabilityMetadata = {}

    m = TestModel(observability_metadata={"repo_name": "OpenHands/OpenHands"})
    assert m.observability_metadata == {"repo_name": "OpenHands/OpenHands"}

    m = TestModel.model_validate({"observability_metadata": None})
    assert m.observability_metadata == {}

    with pytest.raises(ValidationError):
        TestModel.model_validate({"observability_metadata": {"nested": {"bad": True}}})


def test_observability_tags_in_pydantic_model():
    from pydantic import BaseModel

    class TestModel(BaseModel):
        observability_tags: ConversationObservabilityTags = []

    m = TestModel(observability_tags=["repo", "cloud"])
    assert m.observability_tags == ["repo", "cloud"]

    m = TestModel.model_validate({"observability_tags": None})
    assert m.observability_tags == []

    with pytest.raises(ValidationError):
        TestModel.model_validate({"observability_tags": [1, 2]})

    with pytest.raises(ValidationError):
        TestModel.model_validate({"observability_tags": [""]})
