# -*- encoding: utf-8 -*-
"""
locksmith_ui_tester.plugin module

AppPlugin glue: captures the main window in on_app_started, then returns
a DevControlServer from get_app_services(). The Locksmith PluginManager
handles lifecycle — calls server.start() after on_app_started, calls
server.stop() in reverse order before on_app_stopping.

Trust boundary: install confirmation in the Locksmith Plugins UI. There
is no env-var gate. See locksmith-plugin.toml for the warning text users
see at install time.
"""
from __future__ import annotations

from typing import Any

from keri import help

from locksmith.plugins.base import AppPlugin
from locksmith_ui_tester.server import DEFAULT_SOCKET_PATH, DevControlServer

logger = help.ogler.getLogger(__name__)


class LocksmithUiTesterPlugin(AppPlugin):
    plugin_id = "ui_tester"

    def __init__(self) -> None:
        self._window: Any = None

    def initialize(self, app: Any) -> None:
        # Discovery-time hook. No work needed — the server can't start
        # until on_app_started gives us a window.
        pass

    def on_app_started(self, app: Any, window: Any) -> None:
        self._window = window
        logger.warning(
            "ui-tester: dev-control socket starting at %s — any local "
            "process can drive this wallet",
            DEFAULT_SOCKET_PATH,
        )

    def get_app_services(self) -> list[Any]:
        # The PluginManager calls this exactly once per plugin per
        # app lifecycle (immediately after on_app_started, see
        # locksmith.plugins.manager). Constructing a fresh server
        # here is therefore safe — there will not be two servers
        # competing for the same socket.
        if self._window is None:
            return []
        return [DevControlServer(self._window)]
