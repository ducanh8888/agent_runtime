"""Machine-facing CLI for the agentrt runtime.

The primary consumers of this interface are schedulers and operators that
need predictable stdout. All machine-oriented output is JSON; ``--text``
exists only to make the same commands tolerable when a human is attached.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import deque

from agentrt.runtime import bootstrap, client as client_mod, config, daemon


def _print_json(value: object) -> None:
    """Indent JSON for interactive agents that may be reading the stream."""
    sys.stdout.write(
        json.dumps(value, indent=2, default=str, ensure_ascii=False) + "\n"
    )


def _text_of(value: object) -> str:
    """Render a parsed runtime object as one concise human line or line list."""
    if isinstance(value, list):
        return "\n".join(_text_of(item) for item in value)
    if isinstance(value, dict):
        return " ".join(f"{key}={_text_of(item)}" for key, item in value.items())
    return str(value)


def _emit(args: argparse.Namespace, value: object | None) -> None:
    """Print command output in JSON unless text mode was requested."""
    if value is None:
        return
    if getattr(args, "text", False):
        sys.stdout.write(_text_of(value) + "\n")
    else:
        _print_json(value)


def _report_error(code: int, exc: Exception) -> None:
    """Put an error on stderr only, so stdout can remain clean for callers."""
    payload = {"error": type(exc).__name__, "message": str(exc)}
    json.dump(payload, sys.stderr, indent=2, default=str, ensure_ascii=False)
    sys.stderr.write("\n")


def _cmd_dispatch(args: argparse.Namespace) -> dict:
    """Dispatch a task through the same client core used by the MCP server."""
    client = client_mod.Client()
    return client.dispatch(
        task=args.task,
        workspace=args.workspace,
        title=args.title,
    )


def _cmd_list(args: argparse.Namespace) -> list:
    """Return recent sessions in reverse chronological order."""
    client = client_mod.Client()
    return client.list_sessions(limit=args.limit)


def _cmd_status(args: argparse.Namespace) -> dict:
    """Return the last known status for a session."""
    client = client_mod.Client()
    return client.status(args.session)


def _cmd_result(args: argparse.Namespace) -> dict:
    """Return the final result produced by a session."""
    client = client_mod.Client()
    return client.result(args.session)


def _cmd_transcript(args: argparse.Namespace) -> dict:
    """Return a condensed transcript, one page at a time."""
    client = client_mod.Client()
    return client.transcript(args.session, limit=args.limit, cursor=args.cursor)


def _cmd_send(args: argparse.Namespace) -> dict:
    """Append an instruction and let the agent act on it."""
    client = client_mod.Client()
    return client.send(args.session, args.message)


def _cmd_interrupt(args: argparse.Namespace) -> dict:
    """Cancel whatever the session is executing right now."""
    client = client_mod.Client()
    return client.interrupt(args.session)


def _cmd_stop(args: argparse.Namespace) -> dict:
    """Suspend a session, leaving it resumable."""
    client = client_mod.Client()
    return client.stop(args.session)


def _cmd_resume(args: argparse.Namespace) -> dict:
    """Continue a paused session."""
    client = client_mod.Client()
    return client.resume(args.session)


def _cmd_delete(args: argparse.Namespace) -> dict:
    """Permanently remove a session.

    Requires ``--yes``. This is the only destructive command, and a scheduler
    invoking the CLI has no way to answer a prompt, so refusal is the only safe
    default when confirmation is absent.
    """
    if not args.yes:
        raise client_mod.ClientError(
            "refusing to delete without --yes; this cannot be undone"
        )
    client = client_mod.Client()
    return client.delete(args.session)


def _cmd_artifacts(args: argparse.Namespace) -> dict:
    """List the session's workspace, or read one file from it."""
    client = client_mod.Client()
    return client.artifacts(args.session, path=args.path)


def _cmd_daemon_start(_args: argparse.Namespace) -> dict:
    """Start the daemon if it is not already running, then report its state."""
    daemon.ensure_running()
    return daemon.status()


def _cmd_daemon_stop(_args: argparse.Namespace) -> dict:
    """Stop the daemon if it is running, then report its state."""
    daemon.stop()
    return daemon.status()


def _cmd_daemon_status(_args: argparse.Namespace) -> dict:
    """Return daemon process state without touching the daemon itself."""
    return daemon.status()


def _cmd_daemon_logs(args: argparse.Namespace) -> None:
    """Print raw daemon log lines so debugging survives a broken daemon.

    This deliberately bypasses the JSON emitter and the error handler; a
    missing log file is a normal condition, not a failure.
    """
    path = config.log_file()
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as log:
            tail = deque(log, maxlen=args.lines)
    except FileNotFoundError:
        return None
    sys.stdout.write("".join(tail))
    return None


def _cmd_config(_args: argparse.Namespace) -> dict:
    """Return a snapshot of runtime configuration for diagnostics."""
    return bootstrap.summary()


def _add_subparser(
    subparsers: argparse._SubParsersAction,
    name: str,
    *,
    help: str,
) -> argparse.ArgumentParser:
    """A tiny wrapper to keep same-style subparsers together."""
    return subparsers.add_parser(name, help=help)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentrt-runtime",
        description="Machine-facing CLI for the agentrt runtime.",
    )
    parser.add_argument(
        "--text",
        action="store_true",
        help="use a short human-readable line instead of JSON",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    dispatch_parser = _add_subparser(
        subparsers, "dispatch", help="dispatch a task to the runtime"
    )
    dispatch_parser.add_argument("task")
    dispatch_parser.add_argument("--workspace", required=True)
    dispatch_parser.add_argument("--title")
    dispatch_parser.set_defaults(func=_cmd_dispatch)

    list_parser = _add_subparser(subparsers, "list", help="list known sessions")
    list_parser.add_argument("--limit", type=int, default=20)
    list_parser.set_defaults(func=_cmd_list)

    status_parser = _add_subparser(
        subparsers, "status", help="show session status"
    )
    status_parser.add_argument("session")
    status_parser.set_defaults(func=_cmd_status)

    result_parser = _add_subparser(
        subparsers, "result", help="show session result"
    )
    result_parser.add_argument("session")
    result_parser.set_defaults(func=_cmd_result)

    transcript_parser = _add_subparser(
        subparsers, "transcript", help="show a condensed session transcript"
    )
    transcript_parser.add_argument("session")
    transcript_parser.add_argument("--limit", type=int, default=30)
    transcript_parser.add_argument(
        "--cursor", help="next_cursor from a previous page, for older events"
    )
    transcript_parser.set_defaults(func=_cmd_transcript)

    send_parser = _add_subparser(
        subparsers, "send", help="send an instruction to a session"
    )
    send_parser.add_argument("session")
    send_parser.add_argument("message")
    send_parser.set_defaults(func=_cmd_send)

    interrupt_parser = _add_subparser(
        subparsers, "interrupt", help="cancel what a session is doing now"
    )
    interrupt_parser.add_argument("session")
    interrupt_parser.set_defaults(func=_cmd_interrupt)

    stop_parser = _add_subparser(
        subparsers, "stop", help="suspend a session, keeping it resumable"
    )
    stop_parser.add_argument("session")
    stop_parser.set_defaults(func=_cmd_stop)

    resume_parser = _add_subparser(
        subparsers, "resume", help="continue a paused session"
    )
    resume_parser.add_argument("session")
    resume_parser.set_defaults(func=_cmd_resume)

    delete_parser = _add_subparser(
        subparsers, "delete", help="permanently remove a session"
    )
    delete_parser.add_argument("session")
    delete_parser.add_argument(
        "--yes", action="store_true", help="confirm this cannot be undone"
    )
    delete_parser.set_defaults(func=_cmd_delete)

    artifacts_parser = _add_subparser(
        subparsers, "artifacts", help="list or read a session's workspace files"
    )
    artifacts_parser.add_argument("session")
    artifacts_parser.add_argument(
        "--path", help="file to read, relative to the workspace root"
    )
    artifacts_parser.set_defaults(func=_cmd_artifacts)

    daemon_parser = _add_subparser(subparsers, "daemon", help="manage the daemon")
    daemon_subparsers = daemon_parser.add_subparsers(
        dest="daemon_command", required=True
    )

    start_parser = _add_subparser(
        daemon_subparsers, "start", help="start the daemon if needed"
    )
    start_parser.set_defaults(func=_cmd_daemon_start)

    stop_parser = _add_subparser(
        daemon_subparsers, "stop", help="stop the daemon if running"
    )
    stop_parser.set_defaults(func=_cmd_daemon_stop)

    status_daemon_parser = _add_subparser(
        daemon_subparsers, "status", help="report daemon status"
    )
    status_daemon_parser.set_defaults(func=_cmd_daemon_status)

    logs_parser = _add_subparser(
        daemon_subparsers, "logs", help="print raw daemon log tail"
    )
    logs_parser.add_argument("--lines", type=int, default=50)
    logs_parser.set_defaults(func=_cmd_daemon_logs)

    config_parser = _add_subparser(
        subparsers, "config", help="print runtime configuration summary"
    )
    config_parser.set_defaults(func=_cmd_config)

    return parser


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        output = args.func(args)
    except client_mod.ClientError as exc:
        _report_error(1, exc)
        return 1
    except Exception as exc:  # noqa: BLE001 - daemon diagnostics must stay quiet
        _report_error(2, exc)
        return 2

    _emit(args, output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
