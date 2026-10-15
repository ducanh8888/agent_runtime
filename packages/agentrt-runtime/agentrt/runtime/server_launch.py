"""Start the vendored agent server with every workspace kind registered.

``BaseWorkspace`` is a discriminated union, and a member of that union only
exists once the module defining it has been imported. The server imports the
local and remote kinds and nothing else, so a request naming
``kind: "DockerWorkspace"`` was rejected during validation -- before docker was
ever consulted -- with an assertion carrying an empty message. The daemon log
showed a validation error and no docker output at all, which is the whole
diagnosis: nothing had gone wrong with the container, because nothing had tried
to start one.

Importing here rather than patching the vendored server keeps the fork's edit
surface small, and it is the only place that knows which kinds this runtime
means to support.
"""

from __future__ import annotations

import faulthandler
import logging
import runpy
import signal
import sys


_TRANSIENT_ACCEPT_ERRORS = {64, 1236, 10053, 10054}


def _install_windows_accept_loop_resilience() -> None:
    if sys.platform != "win32":
        return

    # CPython closes the listener for any accept OSError; keep this private-API
    # override local to the runtime rather than changing the vendored server.
    from asyncio import exceptions, trsock
    from asyncio.proactor_events import BaseProactorEventLoop

    if getattr(BaseProactorEventLoop, "_agentrt_accept_resilience", False):
        return

    logger = logging.getLogger("asyncio")

    def start_serving(
        self,
        protocol_factory,
        sock,
        sslcontext=None,
        server=None,
        backlog=100,  # noqa: ARG001
        ssl_handshake_timeout=None,
        ssl_shutdown_timeout=None,
    ):
        def report_failure(exc):
            if sock.fileno() != -1:
                self.call_exception_handler(
                    {
                        "message": "Accept failed on a socket",
                        "exception": exc,
                        "socket": trsock.TransportSocket(sock),
                    }
                )
                sock.close()
            elif self._debug:
                logger.debug("Accept failed on socket %r", sock, exc_info=exc)

        def loop(f=None):
            if f is not None:
                try:
                    conn, addr = f.result()
                except OSError as exc:
                    if getattr(exc, "winerror", None) not in _TRANSIENT_ACCEPT_ERRORS:
                        report_failure(exc)
                        return
                    logger.debug(
                        "Dropping aborted incoming connection (WinError %s)",
                        exc.winerror,
                    )
                else:
                    if self._debug:
                        logger.debug(
                            "%r got a new connection from %r: %r", server, addr, conn
                        )
                    try:
                        protocol = protocol_factory()
                        if sslcontext is not None:
                            self._make_ssl_transport(
                                conn,
                                protocol,
                                sslcontext,
                                server_side=True,
                                extra={"peername": addr},
                                server=server,
                                ssl_handshake_timeout=ssl_handshake_timeout,
                                ssl_shutdown_timeout=ssl_shutdown_timeout,
                            )
                        else:
                            self._make_socket_transport(
                                conn,
                                protocol,
                                extra={"peername": addr},
                                server=server,
                            )
                    except OSError as exc:
                        report_failure(exc)
                        return
            if self.is_closed():
                return
            try:
                f = self._proactor.accept(sock)
            except OSError as exc:
                if getattr(exc, "winerror", None) not in _TRANSIENT_ACCEPT_ERRORS:
                    report_failure(exc)
                    return
                logger.debug(
                    "Retrying accept after transient WinError %s", exc.winerror
                )
                self.call_soon(loop)
                return
            except exceptions.CancelledError:
                sock.close()
                return
            self._accept_futures[sock.fileno()] = f
            f.add_done_callback(loop)

        self.call_soon(loop)

    setattr(BaseProactorEventLoop, "_start_serving", start_serving)
    setattr(BaseProactorEventLoop, "_agentrt_accept_resilience", True)


def _register_workspace_kinds() -> list[str]:
    """Import the workspace classes, returning the names that registered.

    Failures are swallowed on purpose. ``agentrt-workspace`` brings a container
    stack that an installation may not want, and a runtime that cannot use
    Docker should still start and run local sessions rather than refusing to
    boot over a workspace kind nobody asked for.
    """
    registered: list[str] = []
    try:
        from agentrt.workspace.docker.workspace import DockerWorkspace  # noqa: F401

        registered.append("DockerWorkspace")
    except Exception:  # pragma: no cover - depends on what is installed
        pass
    try:
        from agentrt.workspace.docker.dev_workspace import (  # noqa: F401
            DockerDevWorkspace,
        )

        registered.append("DockerDevWorkspace")
    except Exception:  # pragma: no cover - depends on what is installed
        pass
    return registered


def _dump_tasks(_signum: int, _frame: object) -> None:
    """Every asyncio task's full await chain, then each thread by name.

    `Task.print_stack` shows one frame for a suspended coroutine, which hides
    where a request is actually waiting; walking `cr_await` does not.
    """
    import asyncio
    import collections
    import sys
    import threading

    out = sys.stderr
    try:
        tasks = asyncio.all_tasks(asyncio.get_running_loop())
    except RuntimeError:
        return
    print(f"--- {len(tasks)} asyncio tasks ---", file=out)
    for task in tasks:
        chain = []
        coro = task.get_coro()
        while coro is not None:
            frame = getattr(coro, "cr_frame", None) or getattr(coro, "gi_frame", None)
            if frame is not None:
                chain.append(f"{frame.f_code.co_name}:{frame.f_lineno}")
            coro = getattr(coro, "cr_await", None) or getattr(
                coro, "gi_yieldfrom", None
            )
        print("TASK " + " > ".join(chain), file=out)
    frames = sys._current_frames()
    names: collections.Counter[str] = collections.Counter()
    for thread in threading.enumerate():
        frame = frames.get(thread.ident or 0)
        where = f"{frame.f_code.co_name}:{frame.f_lineno}" if frame else "?"
        names[f"{thread.name.rstrip('0123456789_')} @ {where}"] += 1
    for name, count in names.most_common():
        print(f"THREADS {count} {name}", file=out)
    out.flush()


def main() -> None:
    # `kill -USR1 <daemon pid>` writes every thread's stack to the daemon log
    # (stderr): the only way to see where a hung daemon is waiting, since the
    # usual Linux ptrace restrictions keep py-spy from attaching to it.
    if hasattr(signal, "SIGUSR1"):
        faulthandler.register(signal.SIGUSR1, all_threads=True)
        # Threads are half the picture: a request awaiting a lock or a queued
        # executor job is a coroutine, so `kill -USR2` dumps asyncio tasks too.
        signal.signal(signal.SIGUSR2, _dump_tasks)
    _install_windows_accept_loop_resilience()
    _register_workspace_kinds()
    # run_name="__main__" so the server's own entry point runs exactly as it
    # does under `python -m agentrt.agent_server`; argv is untouched, so its
    # argument parsing is unaffected.
    runpy.run_module("agentrt.agent_server", run_name="__main__")


if __name__ == "__main__":
    main()
