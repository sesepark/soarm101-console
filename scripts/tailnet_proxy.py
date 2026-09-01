#!/usr/bin/env python3
"""콘솔을 **tailnet에만** 내어 준다.

콘솔 자신은 `127.0.0.1`에만 bind되어 있다. 그것이 기본 방어선이고, 맥 앱은 SSH 터널로
그 loopback에 닿는다. 그런데 아이폰에는 터널이 없다 — 폰에서 쓰려면 tailnet 주소로 닿는
길이 하나 필요하다.

`0.0.0.0`으로 여는 대신 **Tailscale 주소 하나에만** 듣는다. 집 Wi-Fi에 붙은 다른 기기는
여전히 닿지 못하고, loopback도 그대로라 맥의 터널은 아무것도 바뀌지 않는다.

Tailscale 주소를 찾지 못하면 **시작하지 않는다.** 못 찾았을 때 아무 주소에나 붙는 것은
이 파일이 존재하는 이유와 정반대다.

`tailscale serve`를 쓰지 않는 이유는 그쪽이 root(또는 `tailscale set --operator`)를
요구하기 때문이다. 이것은 사용자 권한으로 돈다.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys

TARGET_HOST = "127.0.0.1"
TARGET_PORT = 8088
LISTEN_PORT = 8088


def tailnet_address() -> str:
    try:
        out = subprocess.run(
            ["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=10
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        sys.exit(f"tailscale 주소를 묻지 못했습니다: {exc}")
    address = out.splitlines()[0].strip() if out else ""
    if not address.startswith("100."):
        sys.exit(f"tailnet 주소가 아닙니다: {address!r} — 열지 않고 끝냅니다")
    return address


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while chunk := await reader.read(65536):
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        writer.close()


async def handle(client_reader, client_writer) -> None:
    try:
        server_reader, server_writer = await asyncio.open_connection(TARGET_HOST, TARGET_PORT)
    except OSError:
        client_writer.close()
        return
    await asyncio.gather(
        pipe(client_reader, server_writer),
        pipe(server_reader, client_writer),
        return_exceptions=True,
    )


async def main() -> None:
    address = tailnet_address()
    server = await asyncio.start_server(handle, address, LISTEN_PORT)
    print(f"tailnet에만 열림: http://{address}:{LISTEN_PORT}/viewer/", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
