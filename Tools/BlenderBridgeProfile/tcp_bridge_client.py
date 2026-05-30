"""TCP client mirroring Unity BlenderBridgeProcessor PING/IMPORT protocol."""
from __future__ import annotations

import json
import socket
import sys
import time


def _read_line(sock: socket.socket, max_bytes: int = 65536) -> str:
    buf = bytearray()
    while len(buf) < max_bytes:
        b = sock.recv(1)
        if not b:
            break
        if b == b"\n":
            break
        if b != b"\r":
            buf.extend(b)
    return buf.decode("utf-8", errors="replace").strip()


def _write_line(sock: socket.socket, line: str) -> None:
    sock.sendall((line + "\n").encode("utf-8"))


def bridge_ping_import(
    model_path: str,
    host: str = "127.0.0.1",
    port: int = 35971,
    connect_timeout_ms: int = 350,
    io_timeout_s: float = 120.0,
) -> dict:
    t0 = time.perf_counter()
    result: dict = {
        "model": model_path,
        "host": host,
        "port": port,
        "ok": False,
    }
    try:
        sock = socket.create_connection((host, port), timeout=connect_timeout_ms / 1000.0)
        sock.settimeout(io_timeout_s)
        result["connect_ms"] = round((time.perf_counter() - t0) * 1000, 2)

        t1 = time.perf_counter()
        _write_line(sock, "PING")
        pong = _read_line(sock)
        result["ping_ms"] = round((time.perf_counter() - t1) * 1000, 2)
        result["pong"] = pong
        if pong != "PONG":
            result["error"] = f"unexpected pong: {pong!r}"
            return result

        t2 = time.perf_counter()
        _write_line(sock, "IMPORT|" + model_path)
        ack = _read_line(sock)
        result["import_ack_ms"] = round((time.perf_counter() - t2) * 1000, 2)
        result["ack"] = ack
        result["total_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        result["ok"] = ack == "OK"
        if not result["ok"]:
            result["error"] = f"unexpected ack: {ack!r}"
    except Exception as ex:
        result["error"] = f"{type(ex).__name__}: {ex}"
        result["total_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    return result


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python tcp_bridge_client.py <abs.fbx> [port]", file=sys.stderr)
        return 2
    path = sys.argv[1]
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 35971
    data = bridge_ping_import(path, port=port)
    print(json.dumps(data, indent=2, ensure_ascii=False))
    return 0 if data.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
