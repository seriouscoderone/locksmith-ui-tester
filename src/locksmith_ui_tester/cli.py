#!/usr/bin/env python3
# -*- encoding: utf-8 -*-
"""
devctl — CLI for talking to the locksmith-ui-tester control server.

Available as `devctl` on PATH after `pip install locksmith-ui-tester`,
or invoke directly: `python -m locksmith_ui_tester.cli <op>`.

Usage:

    devctl <op>                       # no-arg op
    devctl <op> '<json-args>'         # op with kwargs

Examples:

    devctl ping
    devctl screenshot
    devctl screenshot '{"path": "/tmp/my.png"}'
    devctl tree '{"clickable_only": true}'
    devctl click '{"target": "Templates"}'
    devctl type '{"target": "_name_field", "text": "Hello"}'
    devctl select '{"target": "_kind", "value": "government"}'
    devctl current_page

The Locksmith wallet must be running with the locksmith-ui-tester plugin
installed and not excluded. The CLI exits with status 1 on connection
failure and status 2 on a server-reported error.
"""
from __future__ import annotations

import argparse
import json
import socket
import sys


DEFAULT_SOCKET_PATH = "/tmp/locksmith-control.sock"


def send(op: str, kwargs: dict, socket_path: str, timeout: float) -> dict:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(socket_path)
    except OSError as e:
        raise SystemExit(
            f"devctl: cannot connect to {socket_path}: {e}\n"
            f"  Is the wallet running with the locksmith-ui-tester plugin installed and active?"
        )
    payload = {"op": op, **kwargs}
    sock.sendall(json.dumps(payload).encode("utf-8") + b"\n")

    # Read until a newline appears. Responses are small (a few KB at most).
    buf = b""
    while b"\n" not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf += chunk
    sock.close()

    line = buf.split(b"\n", 1)[0].decode("utf-8")
    try:
        return json.loads(line)
    except json.JSONDecodeError as e:
        raise SystemExit(f"devctl: invalid JSON from server: {e}\n  {line!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Locksmith dev-control CLI")
    parser.add_argument("op", help="operation name (ping, screenshot, tree, click, …)")
    parser.add_argument(
        "args", nargs="?", default="{}",
        help="JSON object of keyword arguments (default: {})",
    )
    parser.add_argument(
        "--socket", default=DEFAULT_SOCKET_PATH,
        help=f"socket path (default: {DEFAULT_SOCKET_PATH})",
    )
    parser.add_argument(
        "--timeout", type=float, default=5.0,
        help="socket timeout in seconds (default: 5.0)",
    )
    args = parser.parse_args()

    try:
        kwargs = json.loads(args.args)
    except json.JSONDecodeError as e:
        print(f"devctl: arg is not valid JSON: {e}", file=sys.stderr)
        return 1
    if not isinstance(kwargs, dict):
        print("devctl: arg must be a JSON object", file=sys.stderr)
        return 1

    result = send(args.op, kwargs, args.socket, args.timeout)
    print(json.dumps(result, indent=2))
    if "error" in result:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
