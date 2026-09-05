"""Tests for GitHub utility functions."""

from agentrt.sdk.utils.github import ZWJ, sanitize_openhands_mentions


def test_sanitize_basic_mention():
    """Test basic @Agentrt mention is sanitized."""
    text = "Thanks @Agentrt for the help!"
    expected = f"Thanks @{ZWJ}Agentrt for the help!"
    assert sanitize_openhands_mentions(text) == expected


def test_sanitize_case_insensitive():
    """Test that mentions are sanitized regardless of case."""
    test_cases = [
        ("Check @Agentrt here", f"Check @{ZWJ}Agentrt here"),
        ("Check @openhands here", f"Check @{ZWJ}openhands here"),
        ("Check @AGENTRT here", f"Check @{ZWJ}AGENTRT here"),
        ("Check @oPeNhAnDs here", f"Check @{ZWJ}oPeNhAnDs here"),
    ]
    for input_text, expected in test_cases:
        assert sanitize_openhands_mentions(input_text) == expected


def test_sanitize_multiple_mentions():
    """Test multiple mentions in the same text."""
    text = "Both @Agentrt and @openhands should be sanitized"
    expected = f"Both @{ZWJ}Agentrt and @{ZWJ}openhands should be sanitized"
    assert sanitize_openhands_mentions(text) == expected


def test_sanitize_with_punctuation():
    """Test mentions followed by punctuation."""
    test_cases = [
        ("Thanks @Agentrt!", f"Thanks @{ZWJ}Agentrt!"),
        ("Hello @Agentrt.", f"Hello @{ZWJ}Agentrt."),
        ("See @Agentrt,", f"See @{ZWJ}Agentrt,"),
        ("By @Agentrt:", f"By @{ZWJ}Agentrt:"),
        ("From @Agentrt;", f"From @{ZWJ}Agentrt;"),
        ("Hi @Agentrt?", f"Hi @{ZWJ}Agentrt?"),
        ("Use @Agentrt)", f"Use @{ZWJ}Agentrt)"),
        ("Try (@Agentrt)", f"Try (@{ZWJ}Agentrt)"),
    ]
    for input_text, expected in test_cases:
        assert sanitize_openhands_mentions(input_text) == expected


def test_no_sanitize_partial_words():
    """Test that partial word matches are NOT sanitized."""
    test_cases = [
        "OpenHandsTeam",
        "MyOpenHands",
        "OpenHandsBot",
        "#Agentrt",
    ]
    for text in test_cases:
        # Partial words without @ should remain unchanged
        assert sanitize_openhands_mentions(text) == text


def test_no_op_cases():
    """Test cases where no sanitization should occur."""
    test_cases = [
        "",
        "No mentions here",
        "Just some text",
        "@GitHub",
        "@Other",
        "Agentrt without @",
    ]
    for text in test_cases:
        assert sanitize_openhands_mentions(text) == text


def test_sanitize_at_line_boundaries():
    """Test mentions at the start and end of lines."""
    test_cases = [
        ("@Agentrt at start", f"@{ZWJ}Agentrt at start"),
        ("at end @Agentrt", f"at end @{ZWJ}Agentrt"),
        ("@Agentrt", f"@{ZWJ}Agentrt"),
    ]
    for input_text, expected in test_cases:
        assert sanitize_openhands_mentions(input_text) == expected


def test_sanitize_multiline_text():
    """Test sanitization in multiline text."""
    text = """Hello @Agentrt!

This is a test with @openhands mentioned.

Thanks @AGENTRT for everything!"""

    expected = f"""Hello @{ZWJ}Agentrt!

This is a test with @{ZWJ}openhands mentioned.

Thanks @{ZWJ}AGENTRT for everything!"""

    assert sanitize_openhands_mentions(text) == expected


def test_sanitize_with_urls():
    """Test that URLs containing Agentrt are handled correctly."""
    test_cases = [
        # URL should not be sanitized
        ("Visit https://github.com/OpenHands", "Visit https://github.com/OpenHands"),
        # But mention should be sanitized
        (
            "See @Agentrt at https://github.com/OpenHands",
            f"See @{ZWJ}Agentrt at https://github.com/OpenHands",
        ),
    ]
    for input_text, expected in test_cases:
        assert sanitize_openhands_mentions(input_text) == expected


def test_sanitize_preserves_whitespace():
    """Test that whitespace is preserved correctly."""
    text = "  @Agentrt  \n  @openhands  "
    expected = f"  @{ZWJ}Agentrt  \n  @{ZWJ}openhands  "
    assert sanitize_openhands_mentions(text) == expected


def test_zwj_constant():
    """Test that ZWJ constant is correctly defined."""
    assert ZWJ == "\u200d"
    assert len(ZWJ) == 1
    assert ord(ZWJ) == 0x200D
