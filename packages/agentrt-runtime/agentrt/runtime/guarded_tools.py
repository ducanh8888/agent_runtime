"""Register a file editor that honours the session's permission preset.

The vendored `FileEditorExecutor` takes a `workspace_root` that reads like a
sandbox and is not one: `FileEditor` uses it only to suggest paths in error
messages, and `allowed_edits_files` is skipped entirely for `view`. So nothing
confines the editor today.

The guard wraps the executor rather than editing it, which is safe here for a
specific reason: `validate_path` requires an absolute path and passes it through
unchanged, so the editor opens exactly `Path(action.path)`. There is no separate
resolution step for a wrapper to diverge from. Were the editor to start deriving
paths itself, this would have to move inside it.

A refusal comes back as an observation, not an exception. The agent can read an
observation and choose differently; an exception ends the session, which turns a
denied path into a crash.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Sequence

from agentrt.runtime import permissions
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
            permissions.check_path(
                action.path,
                root=self._root,
                permission=self._permission,
                writing=writing,
            )
        except permissions.PermissionDenied as denied:
            return FileEditorObservation.from_text(
                text=(
                    f"Refused: {denied}. This session runs under the "
                    f"'{self._permission}' permission preset."
                ),
                command=action.command,
                path=str(action.path),
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
        conv_state: "ConversationState",
        permission: str | None = None,
    ) -> Sequence["GuardedFileEditorTool"]:
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
