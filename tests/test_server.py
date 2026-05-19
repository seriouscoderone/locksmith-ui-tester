# -*- encoding: utf-8 -*-
"""Smoke tests for the dev-control server.

These tests spin up a real DevControlServer against a temporary socket
path and exercise the wire protocol from a plain Python AF_UNIX client.
Qt's event loop drives the server side; the client side is synchronous
and uses small qapp.processEvents() pumps between send and recv.
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QComboBox, QLineEdit, QMainWindow, QPlainTextEdit, QPushButton, QVBoxLayout,
    QWidget,
)

from locksmith_ui_tester.server import DevControlServer


# ----- helpers --------------------------------------------------------


def _make_window() -> QMainWindow:
    win = QMainWindow()
    central = QWidget()
    central.setObjectName("central")
    lay = QVBoxLayout(central)

    btn = QPushButton("Hello")
    btn.setObjectName("hello_button")
    lay.addWidget(btn)

    line = QLineEdit()
    line.setObjectName("name_field")
    lay.addWidget(line)

    combo = QComboBox()
    combo.setObjectName("kind_combo")
    for k in ("individual", "organization", "government"):
        combo.addItem(k)
    lay.addWidget(combo)

    notes = QPlainTextEdit()
    notes.setObjectName("notes_field")
    lay.addWidget(notes)

    win.setCentralWidget(central)
    win.resize(300, 200)
    return win


def _client_send(qapp, sock_path: str, payload: dict, timeout_s: float = 2.0) -> dict:
    """Sync client. Sends one command, returns the parsed response.

    Pumps qapp.processEvents() between send and recv so the Qt-side
    server gets time to handle the connection and write the reply.
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout_s)
    sock.connect(sock_path)
    sock.sendall(json.dumps(payload).encode() + b"\n")

    sock.setblocking(False)
    deadline = time.monotonic() + timeout_s
    buf = b""
    while time.monotonic() < deadline:
        qapp.processEvents()
        try:
            chunk = sock.recv(65536)
        except (BlockingIOError, socket.timeout):
            chunk = b""
        if chunk:
            buf += chunk
            if b"\n" in buf:
                break
        else:
            time.sleep(0.02)
    sock.close()
    if b"\n" not in buf:
        raise AssertionError(f"no response within {timeout_s}s; got {buf!r}")
    return json.loads(buf.split(b"\n", 1)[0].decode())


@pytest.fixture
def short_sock_path():
    """A short /tmp-rooted socket path. macOS AF_UNIX limits paths to
    104 chars, well below typical pytest tmp_path depths."""
    p = f"/tmp/locksmith-ctl-test-{secrets.token_hex(4)}.sock"
    yield p
    if os.path.exists(p):
        try:
            os.unlink(p)
        except OSError:
            pass


@pytest.fixture
def server(qapp, short_sock_path):
    """Yield (window, server, socket_path) with the server already started."""
    sock_path = short_sock_path
    window = _make_window()
    window.show()
    qapp.processEvents()
    srv = DevControlServer(window, socket_path=sock_path)
    assert srv.start(), "server should start"
    qapp.processEvents()
    QTest.qWait(50)
    qapp.processEvents()
    yield window, srv, sock_path
    srv.stop()
    qapp.processEvents()
    window.deleteLater()
    qapp.processEvents()


# ----- tests ---------------------------------------------------------


def test_start_creates_socket_then_stop_removes_it(qapp, short_sock_path):
    window = _make_window()
    srv = DevControlServer(window, socket_path=short_sock_path)
    assert srv.start()
    qapp.processEvents()
    assert os.path.exists(short_sock_path)
    srv.stop()
    assert not os.path.exists(short_sock_path)


def test_ping(qapp, server):
    _window, _srv, sock_path = server
    result = _client_send(qapp, sock_path, {"op": "ping"})
    assert result == {"ok": True, "pong": True}


def test_unknown_op_returns_error_and_lists_available(qapp, server):
    _window, _srv, sock_path = server
    result = _client_send(qapp, sock_path, {"op": "no_such_op"})
    assert "error" in result
    assert "ping" in result.get("available", [])


def test_screenshot_saves_png(qapp, server, tmp_path):
    _window, _srv, sock_path = server
    out = str(tmp_path / "shot.png")
    result = _client_send(qapp, sock_path, {"op": "screenshot", "path": out})
    assert result["ok"] is True
    assert result["path"] == out
    assert os.path.exists(out)
    assert os.path.getsize(out) > 0


def test_tree_lists_visible_widgets(qapp, server):
    _window, _srv, sock_path = server
    result = _client_send(qapp, sock_path, {"op": "tree"})
    assert result["ok"] is True
    names = {w.get("objectName") for w in result["widgets"]}
    assert "hello_button" in names
    assert "name_field" in names
    assert "kind_combo" in names


def test_tree_text_contains_filter(qapp, server):
    _window, _srv, sock_path = server
    result = _client_send(qapp, sock_path,
                          {"op": "tree", "text_contains": "Hello"})
    texts = {w.get("text") for w in result["widgets"] if "text" in w}
    assert "Hello" in texts


def test_click_by_text(qapp, server):
    window, _srv, sock_path = server
    received: list[int] = []
    # Find the button on the live window and connect a probe.
    btn = window.findChild(QPushButton, "hello_button")
    btn.clicked.connect(lambda: received.append(1))
    result = _client_send(qapp, sock_path, {"op": "click", "target": "Hello"})
    qapp.processEvents()
    assert result["ok"] is True
    assert received == [1]


def test_click_by_object_name(qapp, server):
    window, _srv, sock_path = server
    received: list[int] = []
    btn = window.findChild(QPushButton, "hello_button")
    btn.clicked.connect(lambda: received.append(1))
    result = _client_send(qapp, sock_path,
                          {"op": "click", "target": "hello_button"})
    qapp.processEvents()
    assert result["ok"] is True
    assert received == [1]


def test_click_unknown_target_returns_error(qapp, server):
    _window, _srv, sock_path = server
    result = _client_send(qapp, sock_path,
                          {"op": "click", "target": "no_such_widget"})
    assert "error" in result


def test_type_into_line_edit(qapp, server):
    window, _srv, sock_path = server
    field = window.findChild(QLineEdit, "name_field")
    assert field.text() == ""
    result = _client_send(qapp, sock_path,
                          {"op": "type", "target": "name_field",
                           "text": "Alice"})
    qapp.processEvents()
    assert result["ok"] is True
    assert field.text() == "Alice"


def test_type_into_plain_text_edit(qapp, server):
    window, _srv, sock_path = server
    field = window.findChild(QPlainTextEdit, "notes_field")
    result = _client_send(qapp, sock_path,
                          {"op": "type", "target": "notes_field",
                           "text": "Line 1\nLine 2"})
    qapp.processEvents()
    assert result["ok"] is True
    assert field.toPlainText() == "Line 1\nLine 2"


def test_select_combo_value(qapp, server):
    window, _srv, sock_path = server
    combo = window.findChild(QComboBox, "kind_combo")
    assert combo.currentText() == "individual"
    result = _client_send(qapp, sock_path,
                          {"op": "select", "target": "kind_combo",
                           "value": "government"})
    qapp.processEvents()
    assert result["ok"] is True
    assert combo.currentText() == "government"


def test_invalid_json_returns_error(qapp, server):
    _window, _srv, sock_path = server
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(2.0)
    sock.connect(sock_path)
    sock.sendall(b"{not valid json\n")
    sock.setblocking(False)

    deadline = time.monotonic() + 2.0
    buf = b""
    while time.monotonic() < deadline:
        qapp.processEvents()
        try:
            chunk = sock.recv(65536)
        except (BlockingIOError, socket.timeout):
            chunk = b""
        if chunk:
            buf += chunk
            if b"\n" in buf:
                break
        else:
            time.sleep(0.02)
    sock.close()

    result = json.loads(buf.split(b"\n", 1)[0].decode())
    assert "error" in result
    assert "json" in result["error"].lower()
