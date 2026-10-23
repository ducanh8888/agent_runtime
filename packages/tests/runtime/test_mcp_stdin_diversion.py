"""A child spawned by the stdio MCP server must not share the protocol's stdin (#36).

A child that inherits stdin inherits the JSON-RPC pipe. On Windows a child
blocked on that pipe hung `dispatch(require="commit")` for the client's whole
1800 s idle timeout (`_git_head` spawns git), and any child that reads stdin
can swallow the next protocol message on every platform.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap


_PROBE = textwrap.dedent(
    """
    import subprocess, sys
    from agentrt.runtime import mcp_server

    mcp_server._divert_stdin()
    child = subprocess.run(
        [sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"],
        capture_output=True, text=True, timeout=30,
    )
    print("child:" + child.stdout.strip())
    print("wire:" + repr(sys.stdin.buffer.read().decode()))
    """
)


def test_child_reads_eof_and_the_wire_stays_with_the_server():
    probe = subprocess.run(
        [sys.executable, "-c", _PROBE],
        input="protocol-bytes",
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert probe.returncode == 0, probe.stderr
    lines = dict(line.split(":", 1) for line in probe.stdout.splitlines())
    assert lines["child"] == "''"
    assert lines["wire"] == "'protocol-bytes'"


def test_git_head_does_not_inherit_stdin(monkeypatch, tmp_path):
    from agentrt.runtime import client

    seen = {}

    def fake_run(*args, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout="abc\n", stderr="")

    monkeypatch.setattr(client.subprocess, "run", fake_run)
    assert client._git_head(str(tmp_path)) == "abc"
    assert seen.get("stdin") is subprocess.DEVNULL
