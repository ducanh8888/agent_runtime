"""Register a file editor that honours the session's permission preset.

The vendored `FileEditorExecutor` takes a `workspace_root` that reads like a
sandbox and is not one: `FileEditor` uses it only to suggest paths in error
messages, and `allowed_edits_files` is skipped entirely for `view`. So nothing
confines the editor today.

The guard wraps the executor rather than editing it, and closes the gap that
normally makes a wrapper unsafe by handing the editor the path it checked rather
than the string it was given. An earlier version passed the original string and
justified it by noting that `validate_path` passes paths through unchanged --
true, and not sufficient: the string checked and the file opened could still
differ through a relative name or a component the OS normalises later.

What remains outside its reach is aliasing that no path carries. A hard link is
a second name for one file, so a workspace containing one is a workspace
containing that file. The guard compares device and inode against the runtime's
own credential files, which closes that leak and only that one. A symlink
retargeted between the check and the open is a race this cannot win, and needs
a sandbox rather than a better guard.

A refusal comes back as an observation, not an exception. The agent can read an
observation and choose differently; an exception ends the session, which turns a
denied path into a crash.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from agentrt.runtime import (
    inspect_tools,  # noqa: F401 -- importing this module registers `inspect`
    permissions,
)
from agentrt.sdk.tool import ToolExecutor
from agentrt.sdk.tool.registry import register_tool
from agentrt.tools.file_editor import FileEditorTool
from agentrt.tools.file_editor.definition import (
    FileEditorAction,
    FileEditorObservation,
)


if TYPE_CHECKING:
    from agentrt.sdk.conversation.state import ConversationState


class GuardedFileEditorExecutor(ToolExecutor):
    """Check a path against the preset, then delegate to the real executor."""

    def __init__(
        self,
        inner: ToolExecutor,
        workspace_root: str,
        permission: permissions.Permission,
    ) -> None:
        self._inner = inner
        self._root = workspace_root
        self._permission = permission

    def __call__(
        self,
        action: FileEditorAction,
        conversation: Any = None,
    ) -> FileEditorObservation:
        # `view` is the only command that does not change anything. Everything
        # else -- create, str_replace, insert, undo_edit -- is a write.
        writing = action.command != "view"
        try:
            approved = permissions.check_path(
                action.path,
                root=self._root,
                permission=self._permission,
                writing=writing,
            )
            # Hand the editor the path that was checked, not the string that
            # was asked for. They can differ -- a relative name, a trailing
            # separator, a component the OS normalises later -- and every
            # difference is a chance to approve one file and open another.
            action = action.model_copy(update={"path": str(approved)})
        except permissions.PermissionDenied as denied:
            # is_error must be set. The observation's own rendering computes
            # `change_applied = command != "view" and not is_error`, so a
            # refusal without it is displayed as a change that went through --
            # the agent is told its write succeeded and carries on.
            return FileEditorObservation.from_text(
                text=(
                    f"Refused: {denied}. This session runs under the "
                    f"'{self._permission}' permission preset."
                ),
                command=action.command,
                path=str(action.path),
                is_error=True,
            )
        return self._inner(action, conversation)

    def close(self) -> None:
        close = getattr(self._inner, "close", None)
        if callable(close):
            close()


class GuardedFileEditorTool(FileEditorTool):
    """`file_editor`, with the workspace actually enforced."""

    @classmethod
    def create(
        cls,
        conv_state: ConversationState,
        permission: str | None = None,
    ) -> Sequence[GuardedFileEditorTool]:
        preset = permissions.normalise(permission)
        root = conv_state.workspace.working_dir

        built = super().create(conv_state)
        guarded = []
        for tool in built:
            note = (
                f"\n\nThis session runs under the '{preset}' permission preset. "
                + permissions.DESCRIPTIONS[preset]
            )
            guarded.append(
                tool.model_copy(
                    update={
                        "executor": GuardedFileEditorExecutor(
                            tool.executor, root, preset
                        ),
                        "description": (tool.description or "") + note,
                    }
                )
            )
        return guarded


# ToolDefinition.__init_subclass__ derives `name` from the class name, so this
# subclass would introduce itself to the model as `guarded_file_editor` while
# every profile, description and system prompt says `file_editor`. Found by
# reading a session's transcript, not by any test: the guard worked, and the
# tool it guarded had quietly been renamed.
GuardedFileEditorTool.name = FileEditorTool.name


def install() -> None:
    """Replace the registered `file_editor` with the guarded one.

    `register_tool` overwrites an existing name and `resolve_tool` consults the
    registry before its built-in fallback, so this takes effect for every
    conversation created afterwards. It must run after
    `agentrt.tools.file_editor` is imported, since that module registers the
    unguarded tool at import time.
    """
    register_tool(FileEditorTool.name, GuardedFileEditorTool)


# Registered on import, the way the vendored tools register themselves. The
# server imports the modules named in a request's `tool_module_qualnames`
# specifically "to trigger tool auto-registration", so importing this module is
# what installs the guard inside the daemon process.
install()
