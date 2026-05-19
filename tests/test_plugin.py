# -*- encoding: utf-8 -*-
"""Unit tests for the LocksmithUiTesterPlugin glue."""
from __future__ import annotations

import pytest

from PySide6.QtWidgets import QMainWindow

from locksmith_ui_tester.plugin import LocksmithUiTesterPlugin
from locksmith_ui_tester.server import DevControlServer


def test_plugin_id_is_ui_tester():
    plugin = LocksmithUiTesterPlugin()
    assert plugin.plugin_id == "ui_tester"


def test_initialize_is_a_noop(qtbot):
    """initialize() runs at discovery time, before any window exists."""
    plugin = LocksmithUiTesterPlugin()
    plugin.initialize(app=None)
    # No state should have been set.
    assert plugin._window is None


def test_get_app_services_returns_empty_before_app_started(qtbot):
    """Without a window, there's nothing to drive."""
    plugin = LocksmithUiTesterPlugin()
    assert plugin.get_app_services() == []


def test_on_app_started_captures_window(qtbot):
    plugin = LocksmithUiTesterPlugin()
    window = QMainWindow()
    qtbot.addWidget(window)
    plugin.on_app_started(app=None, window=window)
    assert plugin._window is window


def test_get_app_services_returns_server_after_app_started(qtbot):
    plugin = LocksmithUiTesterPlugin()
    window = QMainWindow()
    qtbot.addWidget(window)
    plugin.on_app_started(app=None, window=window)

    services = plugin.get_app_services()
    assert len(services) == 1
    assert isinstance(services[0], DevControlServer)


def test_get_app_services_passes_window_to_server(qtbot):
    plugin = LocksmithUiTesterPlugin()
    window = QMainWindow()
    qtbot.addWidget(window)
    plugin.on_app_started(app=None, window=window)

    server = plugin.get_app_services()[0]
    # DevControlServer keeps the window as _window — same attr in the
    # ported code. Verify the reference is the captured one.
    assert server._window is window
