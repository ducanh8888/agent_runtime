"""Tests for agentrt.tools package initialization and import handling."""


def test_submodule_imports_work():
    """Tools should be imported via explicit submodules."""
    from agentrt.tools.browser_use import BrowserToolSet
    from agentrt.tools.file_editor import FileEditorTool
    from agentrt.tools.task_tracker import TaskTrackerTool
    from agentrt.tools.terminal import TerminalTool

    assert TerminalTool is not None
    assert FileEditorTool is not None
    assert TaskTrackerTool is not None
    assert BrowserToolSet is not None


def test_tools_module_has_expected_top_level_exports():
    """Common tools/presets should be importable from the top-level package.

    Note: BrowserToolSet is intentionally NOT exported at the top level to avoid
    forcing downstream consumers to bundle browser-use and its heavy dependencies.
    See: https://github.com/OpenHands/Agentrt-CLI/pull/527
    """

    import agentrt.tools

    assert agentrt.tools.TerminalTool is not None
    assert agentrt.tools.FileEditorTool is not None
    assert agentrt.tools.TaskTrackerTool is not None

    assert agentrt.tools.get_default_agent is not None
    assert agentrt.tools.get_default_tools is not None
    assert agentrt.tools.register_default_tools is not None


def test_from_import_works():
    """`from agentrt.tools import X` should work for exported symbols."""

    from agentrt.tools import TerminalTool  # noqa: F401
