"""Shared default-path resolution for the dev-control socket.

The socket lives under HOME so two locksmith instances launched with
different HOME values get independent sockets and don't fight over a
single /tmp path.

Set LOCKSMITH_CONTROL_SOCKET to override (useful for tests, or when
addressing a specific instance explicitly).
"""
from __future__ import annotations

import os
from pathlib import Path


def default_socket_path() -> str:
    override = os.environ.get("LOCKSMITH_CONTROL_SOCKET")
    if override:
        return override
    return str(Path.home() / ".locksmith-control.sock")
