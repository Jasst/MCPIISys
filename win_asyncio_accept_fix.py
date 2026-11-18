"""Backport of the CPython fix for gh-93821 (Windows ProactorEventLoop).

On Windows, if a client resets a connection while it is still waiting in the
listen queue, ``AcceptEx`` fails with ``ERROR_NETNAME_DELETED`` (WinError 64).
Before the CPython fix, ``BaseProactorEventLoop._start_serving()`` treated any
``OSError`` as fatal for the *listening* socket, closed it, and the process kept
running while port 8000 stopped accepting connections.

This module installs the same two changes that CPython merged for gh-93821:

* ``IocpProactor.accept()`` closes the failed connection socket and converts
  ``ERROR_NETNAME_DELETED`` into ``ConnectionResetError``;
* ``BaseProactorEventLoop._start_serving()`` retries on
  ``ConnectionResetError`` / ``ConnectionAbortedError`` instead of closing the
  listening socket.

Call :func:`install` before the event loop creates its server.  The function is
idempotent and does nothing on non-Windows platforms or on Python versions that
already contain the fix.
"""
from __future__ import annotations

import asyncio
import inspect
import socket
import struct
import sys

_installed = False


def _already_fixed() -> bool:
    """Return True when the running CPython already has the gh-93821 fix."""
    from asyncio import proactor_events, windows_events

    try:
        start_serving = inspect.getsource(
            proactor_events.BaseProactorEventLoop._start_serving
        )
        accept = inspect.getsource(windows_events.IocpProactor.accept)
    except (OSError, TypeError):  # frozen/minimized stdlib
        return False
    return "ConnectionResetError" in start_serving and "ConnectionResetError" in accept


def _accept(self, listener):
    """Replacement for ``IocpProactor.accept`` with WinError 64 handling."""
    from asyncio import windows_events as we

    self._register_with_iocp(listener)
    conn = self._get_accept_socket(listener.family)
    ov = we._overlapped.Overlapped(we.NULL)
    ov.AcceptEx(listener.fileno(), conn.fileno())

    def finish_accept(trans, key, ov):
        try:
            ov.getresult()
        except OSError as exc:
            conn.close()
            # The peer reset the connection before AcceptEx completed.  This
            # says nothing about the listening socket, so report it as a
            # per-connection reset instead of a fatal accept error.
            if exc.winerror == we._overlapped.ERROR_NETNAME_DELETED:
                raise ConnectionResetError(*exc.args) from exc
            raise
        # Use SO_UPDATE_ACCEPT_CONTEXT so getsockname() etc work.
        buf = struct.pack('@P', listener.fileno())
        conn.setsockopt(
            socket.SOL_SOCKET,
            we._overlapped.SO_UPDATE_ACCEPT_CONTEXT,
            buf,
        )
        conn.settimeout(listener.gettimeout())
        return conn, conn.getpeername()

    async def accept_coro(future, conn):
        try:
            await future
        except asyncio.CancelledError:
            conn.close()
            raise
        except ConnectionResetError:
            # Reported to the caller through the future; nothing to do here.
            pass

    future = self._register(ov, listener, finish_accept)
    coro = accept_coro(future, conn)
    we.tasks.ensure_future(coro, loop=self._loop)
    return future


def _start_serving(self, protocol_factory, sock, sslcontext=None, server=None,
                   backlog=100, ssl_handshake_timeout=None,
                   ssl_shutdown_timeout=None):
    """Replacement for ``BaseProactorEventLoop._start_serving``."""
    from asyncio import proactor_events as pe

    def loop(f=None):
        try:
            if f is not None:
                conn, addr = f.result()
                if self._debug:
                    pe.logger.debug(
                        "%r got a new connection from %r: %r",
                        server, addr, conn,
                    )
                protocol = protocol_factory()
                if sslcontext is not None:
                    self._make_ssl_transport(
                        conn, protocol, sslcontext, server_side=True,
                        extra={'peername': addr}, server=server,
                        ssl_handshake_timeout=ssl_handshake_timeout,
                        ssl_shutdown_timeout=ssl_shutdown_timeout,
                    )
                else:
                    self._make_socket_transport(
                        conn, protocol,
                        extra={'peername': addr}, server=server,
                    )
            if self.is_closed():
                return
            f = self._proactor.accept(sock)
        except (ConnectionResetError, ConnectionAbortedError):
            # The peer closed the connection before it was accepted.  The
            # listening socket is still usable: keep accepting.
            if self._debug:
                pe.logger.debug("Accept failed on socket %r", sock, exc_info=True)
            self.call_soon(loop)
        except OSError as exc:
            if sock.fileno() != -1:
                self.call_exception_handler({
                    'message': 'Accept failed on a socket',
                    'exception': exc,
                    'socket': pe.trsock.TransportSocket(sock),
                })
                sock.close()
            elif self._debug:
                pe.logger.debug("Accept failed on socket %r", sock, exc_info=True)
        except pe.exceptions.CancelledError:
            sock.close()
        else:
            self._accept_futures[sock.fileno()] = f
            f.add_done_callback(loop)

    self.call_soon(loop)


def install() -> bool:
    """Install the workaround.  Returns True only if this call patched asyncio."""
    global _installed
    if _installed:
        return False
    if sys.platform != 'win32':
        _installed = True
        return False

    from asyncio import proactor_events, windows_events

    if _already_fixed():
        _installed = True
        return False

    windows_events.IocpProactor.accept = _accept
    proactor_events.BaseProactorEventLoop._start_serving = _start_serving
    _installed = True
    return True
