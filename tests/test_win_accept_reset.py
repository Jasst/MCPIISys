"""Регрессия CPython gh-93821: reset соединения не должен убивать listener.

До фикса один клиент, сбросивший соединение до завершения AcceptEx
(WinError 64), закрывает слушающий сокет: процесс жив, порт больше не слушает.
Запуск: python tests/test_win_accept_reset.py
"""
import asyncio
import socket
import struct
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if sys.platform != "win32":
    print("SKIP: тест только для Windows")
    sys.exit(0)

from win_asyncio_accept_fix import install  # noqa: E402


def _reset_connection(port: int) -> None:
    """Подключиться и сразу отправить RST (SO_LINGER=0)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("HH", 1, 0))
    s.settimeout(3)
    s.connect(("127.0.0.1", port))
    s.close()


async def _scenario() -> bool:
    accepted = []

    async def handle(reader, writer):
        accepted.append(writer)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    # Оба connect выполняются, пока event loop заблокирован: первый займёт
    # AcceptEx, второй будет сброшен, пока ждёт в listen queue.
    first = socket.create_connection(("127.0.0.1", port), timeout=3)
    _reset_connection(port)

    deadline = time.monotonic() + 3
    while not accepted and time.monotonic() < deadline:
        await asyncio.sleep(0.05)

    ok = False
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port), 2
        )
        ok = True
        writer.close()
        await writer.wait_closed()
    except OSError as exc:
        print(f"FAIL: listener больше не принимает соединения: {exc!r}")

    first.close()
    for writer in accepted:
        writer.close()
    server.close()
    await server.wait_closed()
    return ok


def main() -> int:
    print(f"patch installed: {install()}")
    ok = asyncio.run(_scenario(), loop_factory=asyncio.ProactorEventLoop)
    print("OK: listener продолжает принимать соединения" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
