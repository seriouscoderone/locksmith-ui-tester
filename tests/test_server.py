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

    # Disabled counterparts. Qt's disabled state blocks input events but
    # NOT programmatic setters — setText/setCurrentIndex succeed on these
    # — which is what makes an unguarded type/select worse than click.
    locked_btn = QPushButton("Locked")
    locked_btn.setObjectName("disabled_button")
    locked_btn.setEnabled(False)
    lay.addWidget(locked_btn)

    locked_field = QLineEdit()
    locked_field.setObjectName("disabled_field")
    locked_field.setEnabled(False)
    lay.addWidget(locked_field)

    locked_combo = QComboBox()
    locked_combo.setObjectName("disabled_combo")
    for k in ("alpha", "beta"):
        locked_combo.addItem(k)
    locked_combo.setEnabled(False)
    lay.addWidget(locked_combo)

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


def test_screenshot_with_target_grabs_specific_widget(qapp, server, tmp_path):
    """Passing `target` resolves the same way click/type do — useful for
    capturing top-level dialogs that aren't part of the main-window
    pixmap."""
    _window, _srv, sock_path = server
    out = str(tmp_path / "btn.png")
    result = _client_send(
        qapp, sock_path,
        {"op": "screenshot", "path": out, "target": "hello_button"},
    )
    assert result["ok"] is True
    assert result["path"] == out
    assert result["target"] == "hello_button"
    assert os.path.exists(out)
    assert os.path.getsize(out) > 0
    # The button is much smaller than the whole window (300×200).
    assert result["size"][0] < 300


def test_screenshot_with_unknown_target_returns_error(qapp, server, tmp_path):
    _window, _srv, sock_path = server
    out = str(tmp_path / "missing.png")
    result = _client_send(
        qapp, sock_path,
        {"op": "screenshot", "path": out, "target": "no_such_widget"},
    )
    assert "error" in result
    assert "no_such_widget" in result["error"]
    assert not os.path.exists(out)


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


def test_click_on_disabled_widget_errors_instead_of_reporting_success(qapp, server):
    """QAbstractButton.click() is a silent no-op on a disabled button, so
    ok:true there means "nothing happened" while reading as "it worked"."""
    window, _srv, sock_path = server
    received: list[int] = []
    btn = window.findChild(QPushButton, "disabled_button")
    btn.clicked.connect(lambda: received.append(1))

    result = _client_send(qapp, sock_path,
                          {"op": "click", "target": "disabled_button"})
    qapp.processEvents()
    assert "error" in result
    assert "disabled" in result["error"]
    assert received == []  # confirms the click really was a no-op
    # Diagnostics survive the refusal, under a key that doesn't claim an
    # action took place.
    assert result["widget"]["objectName"] == "disabled_button"
    assert result["widget"]["enabled"] is False


def test_type_into_disabled_field_errors_and_leaves_it_unchanged(qapp, server):
    """Worse than the click case: setText() SUCCEEDS on a disabled
    QLineEdit, so without the guard this drives the app into a state no
    user could produce, and the run keeps going against it."""
    window, _srv, sock_path = server
    field = window.findChild(QLineEdit, "disabled_field")
    assert field.text() == ""

    result = _client_send(qapp, sock_path,
                          {"op": "type", "target": "disabled_field",
                           "text": "Mallory"})
    qapp.processEvents()
    assert "error" in result
    assert "disabled" in result["error"]
    assert field.text() == ""


def test_select_on_disabled_combo_errors_and_leaves_it_unchanged(qapp, server):
    """Same shape as type: setCurrentIndex() works fine on a disabled
    combo, signals and all."""
    window, _srv, sock_path = server
    combo = window.findChild(QComboBox, "disabled_combo")
    before = combo.currentText()

    result = _client_send(qapp, sock_path,
                          {"op": "select", "target": "disabled_combo",
                           "value": "beta"})
    qapp.processEvents()
    assert "error" in result
    assert "disabled" in result["error"]
    assert combo.currentText() == before


def test_click_on_widget_disabled_by_ancestor_errors(qapp, server):
    """Qt reports a widget as disabled when any ancestor is, so the guard
    catches a button inside a disabled container without special-casing."""
    window, _srv, sock_path = server
    container = QWidget()
    container.setObjectName("locked_panel")
    inner_lay = QVBoxLayout(container)
    inner = QPushButton("Inner Action")
    inner.setObjectName("inner_button")
    inner_lay.addWidget(inner)
    window.centralWidget().layout().addWidget(container)
    container.setEnabled(False)
    container.show()
    qapp.processEvents()

    assert inner.isEnabled() is False  # inherited, never set directly
    result = _client_send(qapp, sock_path,
                          {"op": "click", "target": "inner_button"})
    qapp.processEvents()
    assert "error" in result
    assert "disabled" in result["error"]


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


def test_select_by_index_out_of_range_errors_and_lists_items(qapp, server):
    """An out-of-range index is a silent no-op in Qt (setCurrentIndex
    ignores it), so the op must range-check it the same way the `value`
    path checks findText — including enumerating what IS available."""
    window, _srv, sock_path = server
    combo = window.findChild(QComboBox, "kind_combo")
    before = combo.currentText()
    result = _client_send(qapp, sock_path,
                          {"op": "select", "target": "kind_combo",
                           "index": 99})
    qapp.processEvents()
    assert "error" in result
    assert "out of range" in result["error"]
    assert "individual" in result["error"]  # enumerates available items
    assert combo.currentText() == before  # combo untouched


def test_select_by_negative_index_errors(qapp, server):
    window, _srv, sock_path = server
    combo = window.findChild(QComboBox, "kind_combo")
    before = combo.currentText()
    result = _client_send(qapp, sock_path,
                          {"op": "select", "target": "kind_combo",
                           "index": -1})
    qapp.processEvents()
    assert "error" in result
    assert "out of range" in result["error"]
    assert combo.currentText() == before


def test_select_by_valid_index_selects_item(qapp, server):
    window, _srv, sock_path = server
    combo = window.findChild(QComboBox, "kind_combo")
    result = _client_send(qapp, sock_path,
                          {"op": "select", "target": "kind_combo",
                           "index": 2})
    qapp.processEvents()
    assert result["ok"] is True
    assert result["selected_index"] == 2
    assert combo.currentText() == "government"


def test_get_text_reads_line_edit(qapp, server):
    window, _srv, sock_path = server
    field = window.findChild(QLineEdit, "name_field")
    field.setText("Alice")
    result = _client_send(qapp, sock_path,
                          {"op": "get_text", "target": "name_field"})
    qapp.processEvents()
    assert result["ok"] is True
    assert result["text"] == "Alice"


def test_get_text_reads_button_label(qapp, server):
    _window, _srv, sock_path = server
    result = _client_send(qapp, sock_path,
                          {"op": "get_text", "target": "hello_button"})
    assert result["ok"] is True
    assert result["text"] == "Hello"


def test_get_text_reads_combo_current(qapp, server):
    window, _srv, sock_path = server
    combo = window.findChild(QComboBox, "kind_combo")
    combo.setCurrentText("organization")
    result = _client_send(qapp, sock_path,
                          {"op": "get_text", "target": "kind_combo"})
    qapp.processEvents()
    assert result["ok"] is True
    assert result["text"] == "organization"


def test_is_checked_reads_qcheckbox_state(qapp, server):
    from PySide6.QtWidgets import QCheckBox
    window, _srv, sock_path = server
    cb = QCheckBox("Toggle me")
    cb.setObjectName("demoCheckbox")
    window.centralWidget().layout().addWidget(cb)
    cb.show()
    qapp.processEvents()

    r1 = _client_send(qapp, sock_path,
                      {"op": "is_checked", "target": "demoCheckbox"})
    assert r1 == {"ok": True, "checked": False}

    cb.setChecked(True)
    qapp.processEvents()
    r2 = _client_send(qapp, sock_path,
                      {"op": "is_checked", "target": "demoCheckbox"})
    assert r2 == {"ok": True, "checked": True}


def test_is_checked_errors_on_non_checkable_widget(qapp, server):
    _window, _srv, sock_path = server
    # QLineEdit has no isChecked — should error rather than silently false.
    result = _client_send(qapp, sock_path,
                          {"op": "is_checked", "target": "name_field"})
    assert "error" in result
    assert "isChecked" in result["error"]


def test_is_visible_distinguishes_existence_from_visibility(qapp, server):
    window, _srv, sock_path = server
    # Existing, visible widget
    r1 = _client_send(qapp, sock_path,
                      {"op": "is_visible", "target": "hello_button"})
    assert r1 == {"ok": True, "visible": True, "exists": True}

    # Existing widget but hidden
    btn = window.findChild(QPushButton, "hello_button")
    btn.hide()
    qapp.processEvents()
    r2 = _client_send(qapp, sock_path,
                      {"op": "is_visible", "target": "hello_button"})
    assert r2 == {"ok": True, "visible": False, "exists": True}
    btn.show()

    # Non-existent widget
    r3 = _client_send(qapp, sock_path,
                      {"op": "is_visible", "target": "ghost_widget"})
    assert r3 == {"ok": True, "visible": False, "exists": False}


def test_is_enabled_mirrors_is_visible_shape(qapp, server):
    """Asserting a control is inert is a read, not a drive — is_enabled
    is how you do it once the driving ops refuse disabled targets."""
    _window, _srv, sock_path = server
    r1 = _client_send(qapp, sock_path,
                      {"op": "is_enabled", "target": "hello_button"})
    assert r1 == {"ok": True, "enabled": True, "exists": True}

    r2 = _client_send(qapp, sock_path,
                      {"op": "is_enabled", "target": "disabled_button"})
    assert r2 == {"ok": True, "enabled": False, "exists": True}


def test_is_enabled_reports_missing_widget_without_erroring(qapp, server):
    _window, _srv, sock_path = server
    result = _client_send(qapp, sock_path,
                          {"op": "is_enabled", "target": "ghost_widget"})
    assert result == {"ok": True, "enabled": False, "exists": False}


def test_is_enabled_finds_hidden_widget(qapp, server):
    """Like is_visible, resolution goes through _find_widget_any so a
    hidden-but-present widget reports exists: True."""
    window, _srv, sock_path = server
    btn = window.findChild(QPushButton, "hello_button")
    btn.hide()
    qapp.processEvents()
    result = _client_send(qapp, sock_path,
                          {"op": "is_enabled", "target": "hello_button"})
    btn.show()
    assert result == {"ok": True, "enabled": True, "exists": True}


def test_wait_for_returns_when_widget_appears(qapp, server):
    window, _srv, sock_path = server
    btn = window.findChild(QPushButton, "hello_button")
    btn.hide()
    qapp.processEvents()

    # Schedule the widget to show shortly after wait_for begins
    from PySide6.QtCore import QTimer
    QTimer.singleShot(150, btn.show)

    result = _client_send(qapp, sock_path,
                          {"op": "wait_for", "target": "hello_button",
                           "condition": "visible", "timeout_ms": 2000},
                          timeout_s=3.0)
    assert result["ok"] is True
    assert result["elapsed_ms"] >= 100


def test_wait_for_times_out_when_widget_never_appears(qapp, server):
    _window, _srv, sock_path = server
    result = _client_send(qapp, sock_path,
                          {"op": "wait_for", "target": "ghost_widget",
                           "condition": "visible", "timeout_ms": 200},
                          timeout_s=2.0)
    assert "error" in result
    assert "timeout" in result["error"]


def test_wait_for_enabled_returns_when_widget_becomes_enabled(qapp, server):
    """The natural readiness signal: wait until the control the test is
    about to drive can actually be driven."""
    window, _srv, sock_path = server
    btn = window.findChild(QPushButton, "disabled_button")

    from PySide6.QtCore import QTimer
    # Context-object overload: Qt drops the callback if btn dies first,
    # so a timer outliving this test can't fire into the next one's setup.
    QTimer.singleShot(150, btn, lambda: btn.setEnabled(True))

    result = _client_send(qapp, sock_path,
                          {"op": "wait_for", "target": "disabled_button",
                           "condition": "enabled", "timeout_ms": 2000},
                          timeout_s=3.0)
    assert result["ok"] is True
    assert result["elapsed_ms"] >= 100


def test_wait_for_disabled_returns_when_widget_becomes_disabled(qapp, server):
    window, _srv, sock_path = server
    btn = window.findChild(QPushButton, "hello_button")

    from PySide6.QtCore import QTimer
    QTimer.singleShot(150, btn, lambda: btn.setEnabled(False))

    result = _client_send(qapp, sock_path,
                          {"op": "wait_for", "target": "hello_button",
                           "condition": "disabled", "timeout_ms": 2000},
                          timeout_s=3.0)
    btn.setEnabled(True)
    assert result["ok"] is True


def test_wait_for_disabled_does_not_pass_on_missing_widget(qapp, server):
    """Unlike `hidden`, absence must NOT satisfy `disabled` — otherwise a
    typo'd target returns ok instantly, which is the very bug class this
    change exists to remove."""
    _window, _srv, sock_path = server
    result = _client_send(qapp, sock_path,
                          {"op": "wait_for", "target": "ghost_widget",
                           "condition": "disabled", "timeout_ms": 200},
                          timeout_s=2.0)
    assert "error" in result
    assert "not found" in result["error"]


def test_wait_for_enabled_timeout_names_the_actual_state(qapp, server):
    """Three different failures — absent, hidden, present-but-disabled —
    point at three different bugs, so the message must tell them apart."""
    window, _srv, sock_path = server

    missing = _client_send(qapp, sock_path,
                           {"op": "wait_for", "target": "ghost_widget",
                            "condition": "enabled", "timeout_ms": 200},
                           timeout_s=2.0)
    assert "not found" in missing["error"]

    stuck = _client_send(qapp, sock_path,
                         {"op": "wait_for", "target": "disabled_button",
                          "condition": "enabled", "timeout_ms": 200},
                         timeout_s=2.0)
    assert "disabled" in stuck["error"]

    btn = window.findChild(QPushButton, "hello_button")
    btn.hide()
    qapp.processEvents()
    unshown = _client_send(qapp, sock_path,
                           {"op": "wait_for", "target": "hello_button",
                            "condition": "enabled", "timeout_ms": 200},
                           timeout_s=2.0)
    btn.show()
    assert "hidden" in unshown["error"]


def test_wait_for_enabled_requires_visibility(qapp, server):
    """An enabled-but-hidden widget must not satisfy `enabled`: click
    resolves via _find_widget, which requires visible, so passing here
    would just move the failure to the next step."""
    window, _srv, sock_path = server
    btn = window.findChild(QPushButton, "hello_button")
    btn.hide()
    qapp.processEvents()
    assert btn.isEnabled()
    result = _client_send(qapp, sock_path,
                          {"op": "wait_for", "target": "hello_button",
                           "condition": "enabled", "timeout_ms": 200},
                          timeout_s=2.0)
    btn.show()
    # Must be a timeout naming the real state, not a rejected condition —
    # asserting only on "error" would pass against code that doesn't
    # support `enabled` at all.
    assert "timeout" in result["error"]
    assert "hidden" in result["error"]


def test_wait_for_rejects_unknown_condition_and_lists_valid_ones(qapp, server):
    _window, _srv, sock_path = server
    result = _client_send(qapp, sock_path,
                          {"op": "wait_for", "target": "hello_button",
                           "condition": "bogus"})
    assert "error" in result
    for cond in ("visible", "hidden", "enabled", "disabled"):
        assert cond in result["error"]


def test_count_returns_matching_visible_widgets(qapp, server):
    # The window has 1 QLineEdit, 1 QComboBox, 1 QPlainTextEdit, 1 QPushButton.
    _window, _srv, sock_path = server
    r1 = _client_send(qapp, sock_path,
                      {"op": "count", "target": "QPushButton"})
    assert r1["ok"] is True
    assert r1["count"] >= 1  # may also have internal Qt buttons; >=1 is enough

    r2 = _client_send(qapp, sock_path,
                      {"op": "count", "target": "hello_button"})
    assert r2["ok"] is True
    assert r2["count"] == 1

    r3 = _client_send(qapp, sock_path,
                      {"op": "count", "target": "ghost_widget"})
    assert r3 == {"ok": True, "count": 0}


def _add_demo_table(qapp, window, name="guardTable"):
    from PySide6.QtWidgets import QTableWidget, QTableWidgetItem
    table = QTableWidget(1, 2)
    table.setObjectName(name)
    table.setHorizontalHeaderLabels(["AID", "Label"])
    table.setItem(0, 0, QTableWidgetItem("EAID_ONE"))
    table.setItem(0, 1, QTableWidgetItem("alice"))
    window.centralWidget().layout().addWidget(table)
    table.show()
    qapp.processEvents()
    return table


def _add_demo_list(qapp, window, name="guardList"):
    from PySide6.QtWidgets import QListWidget, QListWidgetItem
    lw = QListWidget()
    lw.setObjectName(name)
    lw.addItem(QListWidgetItem("alice"))
    window.centralWidget().layout().addWidget(lw)
    lw.show()
    qapp.processEvents()
    return lw


def test_click_table_row_on_disabled_table_says_disabled_not_not_found(qapp, server):
    """The guard must run AFTER the row matches. If disabled-ness were
    just another `continue` in the search loop, the op would fall through
    to "table row not found" — trading a precise failure for a wrong one
    that points at the selector instead of the app state."""
    window, _srv, sock_path = server
    table = _add_demo_table(qapp, window)
    table.setEnabled(False)
    qapp.processEvents()

    result = _client_send(qapp, sock_path,
                          {"op": "click_table_row", "text": "alice"})
    qapp.processEvents()
    assert "error" in result
    assert "disabled" in result["error"]
    assert "not found" not in result["error"]


def test_click_table_row_on_disabled_cell_item_errors(qapp, server):
    """Item-level disabling: the table is fine, the row is not."""
    from PySide6.QtCore import Qt as QtCore_Qt
    window, _srv, sock_path = server
    table = _add_demo_table(qapp, window, name="itemGuardTable")
    for col in range(table.columnCount()):
        item = table.item(0, col)
        item.setFlags(item.flags() & ~QtCore_Qt.ItemIsEnabled)
    qapp.processEvents()

    result = _client_send(qapp, sock_path,
                          {"op": "click_table_row", "text": "alice"})
    qapp.processEvents()
    assert "error" in result
    assert "disabled" in result["error"]


def test_click_list_item_on_disabled_list_says_disabled(qapp, server):
    window, _srv, sock_path = server
    lw = _add_demo_list(qapp, window)
    lw.setEnabled(False)
    qapp.processEvents()

    result = _client_send(qapp, sock_path,
                          {"op": "click_list_item", "text": "alice"})
    qapp.processEvents()
    assert "error" in result
    assert "disabled" in result["error"]
    assert "not found" not in result["error"]


def test_click_list_item_on_disabled_item_errors(qapp, server):
    from PySide6.QtCore import Qt as QtCore_Qt
    window, _srv, sock_path = server
    lw = _add_demo_list(qapp, window, name="itemGuardList")
    item = lw.item(0)
    item.setFlags(item.flags() & ~QtCore_Qt.ItemIsEnabled)
    qapp.processEvents()

    received: list[int] = []
    lw.clicked.connect(lambda *_: received.append(1))
    result = _client_send(qapp, sock_path,
                          {"op": "click_list_item", "text": "alice"})
    qapp.processEvents()
    assert "error" in result
    assert "disabled" in result["error"]
    assert received == []


def test_click_row_action_on_disabled_table_says_disabled(qapp, server):
    """click_row_action needs Locksmith's SkewersMenuButton for its happy
    path, which isn't importable here — but the disabled-table refusal
    happens before that lookup, so it is testable standalone."""
    window, _srv, sock_path = server
    table = _add_demo_table(qapp, window, name="actionGuardTable")
    table.setEnabled(False)
    qapp.processEvents()

    result = _client_send(qapp, sock_path,
                          {"op": "click_row_action",
                           "row_text": "alice", "action": "Rotate"})
    qapp.processEvents()
    assert "error" in result
    assert "disabled" in result["error"]
    assert "not found" not in result["error"]


def test_click_table_row_still_works_when_enabled(qapp, server):
    """Guard must not break the happy path."""
    window, _srv, sock_path = server
    table = _add_demo_table(qapp, window, name="okTable")
    received: list[int] = []
    table.cellClicked.connect(lambda *_: received.append(1))

    result = _client_send(qapp, sock_path,
                          {"op": "click_table_row", "text": "alice"})
    qapp.processEvents()
    assert result["ok"] is True
    assert received == [1]


def test_get_table_rows_reads_qtablewidget_content(qapp, server):
    from PySide6.QtWidgets import QTableWidget, QTableWidgetItem
    window, _srv, sock_path = server
    table = QTableWidget(2, 2)
    table.setObjectName("demoTable")
    table.setHorizontalHeaderLabels(["AID", "Label"])
    table.setItem(0, 0, QTableWidgetItem("EAID_ONE"))
    table.setItem(0, 1, QTableWidgetItem("alice"))
    table.setItem(1, 0, QTableWidgetItem("EAID_TWO"))
    table.setItem(1, 1, QTableWidgetItem("bob"))
    window.centralWidget().layout().addWidget(table)
    table.show()
    qapp.processEvents()

    result = _client_send(qapp, sock_path,
                          {"op": "get_table_rows", "target": "demoTable"})
    assert result["ok"] is True
    assert result["headers"] == ["AID", "Label"]
    assert result["rows"] == [
        {"AID": "EAID_ONE", "Label": "alice"},
        {"AID": "EAID_TWO", "Label": "bob"},
    ]


def test_get_list_items_reads_qlistwidget_with_userrole(qapp, server):
    from PySide6.QtCore import Qt as QtCore_Qt
    from PySide6.QtWidgets import QListWidget, QListWidgetItem
    window, _srv, sock_path = server
    lw = QListWidget()
    lw.setObjectName("pairedPeersPage.peersList")
    for label, aid in [("alice", "EAID_ALICE"), ("bob", "EAID_BOB")]:
        item = QListWidgetItem(f"{label}  —  {aid}")
        item.setData(QtCore_Qt.UserRole, aid)
        lw.addItem(item)
    window.centralWidget().layout().addWidget(lw)
    lw.show()
    qapp.processEvents()

    result = _client_send(qapp, sock_path,
                          {"op": "get_list_items",
                           "target": "pairedPeersPage.peersList"})
    assert result["ok"] is True
    assert result["items"] == [
        {"text": "alice  —  EAID_ALICE", "data": "EAID_ALICE"},
        {"text": "bob  —  EAID_BOB", "data": "EAID_BOB"},
    ]


def test_get_list_items_rejects_non_list(qapp, server):
    _window, _srv, sock_path = server
    result = _client_send(qapp, sock_path,
                          {"op": "get_list_items", "target": "hello_button"})
    assert "error" in result
    assert "not QListWidget" in result["error"]


def test_get_table_rows_rejects_non_table(qapp, server):
    _window, _srv, sock_path = server
    result = _client_send(qapp, sock_path,
                          {"op": "get_table_rows", "target": "hello_button"})
    assert "error" in result
    assert "not QTableWidget" in result["error"]


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
