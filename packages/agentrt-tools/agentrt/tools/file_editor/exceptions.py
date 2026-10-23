class ToolError(Exception):
    """Raised when a tool encounters an error."""

    message: str

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)

    def __str__(self):
        return self.message


class EditorToolParameterMissingError(ToolError):
    """Raised when a required parameter is missing for a tool command."""

    command: str
    parameter: str

    def __init__(self, command: str, parameter: str):
        self.command = command
        self.parameter = parameter
        self.message: str = (
            f"Parameter `{parameter}` is required for command: {command}."
        )
        if parameter in _LONG_TEXT_PARAMETERS:
            # AgentRT fork (#33): a long value can be lost before it reaches
            # the tool, and the model then repeated the same call until stuck.
            self.message += (
                f" If you did send `{parameter}`, it was probably too long and"
                " was cut off on the way: split the change into several smaller"
                " edits (a few KB each), or write the content to a file with the"
                " terminal instead of repeating this call."
            )


_LONG_TEXT_PARAMETERS = frozenset({"new_str", "file_text"})


class EditorToolParameterInvalidError(ToolError):
    """Raised when a parameter is invalid for a tool command."""

    parameter: str
    value: str

    def __init__(self, parameter: str, value: str, hint: str | None = None):
        self.parameter = parameter
        self.value = value
        self.message: str = (
            f"Invalid `{parameter}` parameter: {value}. {hint}"
            if hint
            else f"Invalid `{parameter}` parameter: {value}."
        )


class FileValidationError(ToolError):
    """Raised when a file fails validation checks (size, type, etc.)."""

    path: str
    reason: str

    def __init__(self, path: str, reason: str):
        self.path = path
        self.reason = reason
        self.message: str = f"File validation failed for {path}: {reason}"
        super().__init__(self.message)
