# -*- encoding: utf-8 -*-
"""
locksmith_ui_tester.server module

Control server for the locksmith-ui-tester plugin. Lifecycle is managed
by the Locksmith plugin system: start() runs after on_app_started,
stop() runs before on_app_stopping.

Listens on a Unix socket at /tmp/locksmith-control.sock and accepts
newline-delimited JSON commands that drive the live UI on the Qt main
thread. Trust boundary: any local process that can reach the socket can
drive the app. The plugin install confirmation in Locksmith is what
gates that access — see the plugin's locksmith-plugin.toml description.

Wire protocol:
    Client → server:  {"op": "<name>", ...args}\n
    Server → client:  {"ok": true, ...result}\n   or   {"error": "..."}\n
"""
from __future__ import annotations

import json
import os
from typing import Any

from PySide6.QtCore import QObject, Qt
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QMainWindow, QWidget
from keri import help


logger = help.ogler.getLogger(__name__)


DEFAULT_SOCKET_PATH = "/tmp/locksmith-control.sock"


class DevControlServer(QObject):
    """Listens on a Unix socket and dispatches JSON commands.

    All command handling runs on the Qt main thread (where the server's
    QObject lives), so widget access is thread-safe. Connections are
    one-shot: client sends one command, server replies, both sides
    disconnect.
    """

    def __init__(
        self,
        window: QMainWindow,
        socket_path: str = DEFAULT_SOCKET_PATH,
        parent: QObject | None = None,
    ):
        super().__init__(parent=parent)
        self._window = window
        self._socket_path = socket_path
        self._server: QLocalServer | None = None

    def start(self) -> bool:
        """Begin listening. Returns True on success."""
        # Remove any stale socket from a prior crash.
        if os.path.exists(self._socket_path):
            try:
                os.unlink(self._socket_path)
            except OSError as e:
                logger.warning("Could not unlink stale socket %s: %s",
                               self._socket_path, e)
        self._server = QLocalServer(self)
        if not self._server.listen(self._socket_path):
            logger.error(
                "DevControlServer failed to listen on %s: %s",
                self._socket_path, self._server.errorString(),
            )
            self._server = None
            return False
        self._server.newConnection.connect(self._on_connect)
        logger.info("DevControlServer listening on %s", self._socket_path)
        return True

    def stop(self) -> None:
        """Stop listening and remove the socket file."""
        if self._server is not None:
            self._server.close()
            self._server = None
        if os.path.exists(self._socket_path):
            try:
                os.unlink(self._socket_path)
            except OSError:
                pass

    # ----- connection handling --------------------------------------

    def _on_connect(self) -> None:
        assert self._server is not None
        client = self._server.nextPendingConnection()
        if client is None:
            return
        # The client is owned by self so it is cleaned up when the server
        # is destroyed. We keep no explicit reference per-client; the Qt
        # signal connections retain it for the lifetime of the exchange.
        client.setParent(self)
        client.readyRead.connect(lambda c=client: self._on_data(c))
        client.disconnected.connect(client.deleteLater)

    def _on_data(self, client: QLocalSocket) -> None:
        if not client.canReadLine():
            return
        raw = bytes(client.readLine()).decode("utf-8", errors="replace").strip()
        if not raw:
            return
        try:
            cmd = json.loads(raw)
        except json.JSONDecodeError as e:
            self._respond(client, {"error": f"invalid json: {e}"})
            return
        if not isinstance(cmd, dict):
            self._respond(client, {"error": "command must be a JSON object"})
            return
        try:
            result = self._dispatch(cmd)
        except Exception as e:  # noqa: BLE001
            logger.exception("DevControlServer dispatch failure")
            result = {"error": f"{type(e).__name__}: {e}"}
        self._respond(client, result)

    def _respond(self, client: QLocalSocket, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8") + b"\n"
        client.write(data)
        client.flush()
        client.disconnectFromServer()

    # ----- dispatch --------------------------------------------------

    def _dispatch(self, cmd: dict[str, Any]) -> dict[str, Any]:
        op = cmd.get("op")
        handler = self._handlers().get(op)
        if handler is None:
            return {"error": f"unknown op: {op!r}",
                    "available": sorted(self._handlers().keys())}
        return handler(cmd)

    def _handlers(self) -> dict[str, Any]:
        return {
            "ping": self._op_ping,
            "screenshot": self._op_screenshot,
            "tree": self._op_tree,
            "current_page": self._op_current_page,
            "click": self._op_click,
            "click_list_item": self._op_click_list_item,
            "type": self._op_type,
            "select": self._op_select,
        }

    # ----- operations ------------------------------------------------

    def _op_ping(self, cmd: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "pong": True}

    def _op_screenshot(self, cmd: dict[str, Any]) -> dict[str, Any]:
        path = cmd.get("path", "/tmp/locksmith-screenshot.png")
        pix = self._window.grab()
        if pix.isNull():
            return {"error": "grab returned a null pixmap"}
        if not pix.save(path):
            return {"error": f"failed to save pixmap to {path}"}
        return {
            "ok": True,
            "path": path,
            "size": [pix.width(), pix.height()],
        }

    def _op_tree(self, cmd: dict[str, Any]) -> dict[str, Any]:
        visible_only = bool(cmd.get("visible_only", True))
        clickable_only = bool(cmd.get("clickable_only", False))
        text_filter = cmd.get("text_contains")
        entries: list[dict[str, Any]] = []
        for w in self._window.findChildren(QWidget):
            if visible_only and not w.isVisible():
                continue
            info = self._widget_info(w)
            if clickable_only:
                if not (hasattr(w, "click") or info.get("type") in
                        ("QListWidget", "QListWidgetItem", "QComboBox")):
                    continue
            if text_filter and text_filter not in (info.get("text") or ""):
                continue
            entries.append(info)
        return {"ok": True, "count": len(entries), "widgets": entries}

    def _widget_info(self, w: QWidget) -> dict[str, Any]:
        rect = w.rect()
        info: dict[str, Any] = {
            "type": type(w).__name__,
            "objectName": w.objectName(),
            "rect": [rect.x(), rect.y(), rect.width(), rect.height()],
            "enabled": w.isEnabled(),
            "visible": w.isVisible(),
        }
        if hasattr(w, "text"):
            try:
                t = w.text()
                if isinstance(t, str) and t:
                    info["text"] = t
            except Exception:
                pass
        try:
            tt = w.toolTip()
            if isinstance(tt, str) and tt:
                info["tooltip"] = tt
        except Exception:
            pass
        return info

    def _op_current_page(self, cmd: dict[str, Any]) -> dict[str, Any]:
        # Look on both the window and the LocksmithApplication
        # (mirrors how the plugin code does the lookup).
        app = getattr(self._window, "app", None)
        vault_page = getattr(app, "_vault_page", None) if app else None
        if vault_page is None:
            vault_page = getattr(self._window, "_vault_page", None)
        if vault_page is None:
            return {"ok": True, "vault_page": None}
        return {
            "ok": True,
            "vault_page": getattr(vault_page, "_current_page_key", None),
            "previous_vault_page": getattr(
                vault_page, "_previous_vault_page_key", None,
            ),
        }

    def _op_click(self, cmd: dict[str, Any]) -> dict[str, Any]:
        target = cmd.get("target")
        if not target:
            return {"error": "target is required"}
        widget = self._find_widget(target)
        if widget is None:
            return {"error": f"widget not found: {target!r}"}
        if hasattr(widget, "click"):
            widget.click()
        else:
            from PySide6.QtTest import QTest
            QTest.mouseClick(widget, Qt.LeftButton)
        return {"ok": True, "clicked": self._widget_info(widget)}

    def _op_click_list_item(self, cmd: dict[str, Any]) -> dict[str, Any]:
        """Click an item inside a QListWidget by its text.

        QListWidgetItem is not a QWidget, so the standard target resolver
        can't reach it. This op walks the visible QListWidgets, finds an
        item whose text() matches, selects it, and emits itemClicked.

        Optional `list` arg restricts the search to a QListWidget with
        that objectName.
        """
        from PySide6.QtWidgets import QListWidget

        item_text = cmd.get("text")
        if not item_text:
            return {"error": "text is required"}
        list_filter = cmd.get("list")
        for lw in self._window.findChildren(QListWidget):
            if not lw.isVisible():
                continue
            if list_filter and lw.objectName() != list_filter:
                continue
            for i in range(lw.count()):
                item = lw.item(i)
                if item.text().strip() == item_text:
                    lw.setCurrentItem(item)
                    lw.itemClicked.emit(item)
                    return {
                        "ok": True,
                        "list_object_name": lw.objectName(),
                        "item_text": item.text(),
                        "index": i,
                    }
        return {"error": f"list item not found: {item_text!r}"}

    def _op_type(self, cmd: dict[str, Any]) -> dict[str, Any]:
        target = cmd.get("target")
        text = cmd.get("text", "")
        if not target:
            return {"error": "target is required"}
        widget = self._find_widget(target)
        if widget is None:
            return {"error": f"widget not found: {target!r}"}
        if hasattr(widget, "setPlainText") and type(widget).__name__ == "QPlainTextEdit":
            widget.setPlainText(text)
            return {"ok": True}
        if hasattr(widget, "setText"):
            widget.setText(text)
            return {"ok": True}
        return {"error": f"widget {type(widget).__name__} has no setText/setPlainText"}

    def _op_select(self, cmd: dict[str, Any]) -> dict[str, Any]:
        target = cmd.get("target")
        value = cmd.get("value")
        if not target or value is None:
            return {"error": "target and value are required"}
        widget = self._find_widget(target)
        if widget is None:
            return {"error": f"widget not found: {target!r}"}
        if hasattr(widget, "setCurrentText"):
            widget.setCurrentText(str(value))
            return {"ok": True}
        return {"error": f"widget {type(widget).__name__} is not a QComboBox"}

    # ----- widget lookup --------------------------------------------

    def _find_widget(self, target: str) -> QWidget | None:
        """Find a visible widget by selector.

        Selectors supported:
          - exact `objectName`
          - exact `.text()` (for widgets that have a text method)
          - exact `.toolTip()` (for icon buttons)
          - `Type:N` — the N-th visible widget whose class name is `Type`
            (e.g. "QLineEdit:0", "LocksmithButton:1"). Index respects
            widget-tree order. Useful when widgets lack names/text.

        Returns the first match. Ambiguous targets return the first hit
        in widget-tree order — caller is responsible for using a more
        specific target when that's not what they want.
        """
        # Type:N selector path
        if ":" in target:
            type_name, _, idx_str = target.partition(":")
            try:
                idx = int(idx_str)
            except ValueError:
                idx = None
            if idx is not None and type_name:
                hits: list[QWidget] = []
                for w in self._window.findChildren(QWidget):
                    if not w.isVisible():
                        continue
                    if type(w).__name__ == type_name:
                        hits.append(w)
                if 0 <= idx < len(hits):
                    return hits[idx]
                return None

        # Standard name/text/tooltip path
        matches: list[QWidget] = []
        for w in self._window.findChildren(QWidget):
            if not w.isVisible():
                continue
            if w.objectName() == target:
                matches.append(w)
                continue
            if hasattr(w, "text"):
                try:
                    t = w.text()
                except Exception:
                    t = ""
                if isinstance(t, str) and t.strip() == target:
                    matches.append(w)
                    continue
            try:
                tt = w.toolTip()
            except Exception:
                tt = ""
            if isinstance(tt, str) and tt.strip() == target:
                matches.append(w)
        return matches[0] if matches else None
