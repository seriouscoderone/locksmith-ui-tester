# -*- encoding: utf-8 -*-
"""
locksmith_ui_tester.server module

Control server for the locksmith-ui-tester plugin. Lifecycle is managed
by the Locksmith plugin system: start() runs after on_app_started,
stop() runs before on_app_stopping.

Listens on a Unix socket under HOME (default ~/.locksmith-control.sock)
and accepts newline-delimited JSON commands that drive the live UI on the
Qt main thread. Trust boundary: any local process that can reach the socket
can drive the app. The plugin install confirmation in Locksmith is what
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

from locksmith_ui_tester.paths import default_socket_path


logger = help.ogler.getLogger(__name__)


DEFAULT_SOCKET_PATH = default_socket_path()


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
        # The client may have already disconnected / been deleted while we
        # were dispatching — common when a slot opens a blocking modal
        # dialog (QFileDialog, QMessageBox) and devctl times out waiting.
        # Writing to a freed C++ object raises RuntimeError into Qt's event
        # loop, which can crash the whole wallet. Guard it.
        try:
            client.write(data)
            client.flush()
            client.disconnectFromServer()
        except RuntimeError as e:
            logger.warning("DevControlServer: client gone before reply could send (%s)", e)

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
            "click_table_row": self._op_click_table_row,
            "click_row_action": self._op_click_row_action,
            "type": self._op_type,
            "select": self._op_select,
            # Generic state-inspection ops. Cypress-equivalent: cy.get().text(),
            # cy.get().should('be.visible'), cy.get().its('length'),
            # cy.contains(...).wait(), etc. Tests should prefer these over
            # the feature-coupled peer_* ops below.
            "get_text": self._op_get_text,
            "is_visible": self._op_is_visible,
            "is_checked": self._op_is_checked,
            "wait_for": self._op_wait_for,
            "count": self._op_count,
            "get_table_rows": self._op_get_table_rows,
            "get_list_items": self._op_get_list_items,
            # Peer-mode integration-test helpers. These intentionally
            # bypass UI to keep the integration fixture light.
            # TODO: refactor each into click sequences using the generic
            # ops above; see plan in branch feat/direct-peer-design.
            "peer_open_test_vault": self._op_peer_open_test_vault,
            "peer_create_test_aid": self._op_peer_create_test_aid,
            "peer_force_pair": self._op_peer_force_pair,
            "peer_list": self._op_peer_list,
            "peer_get_port": self._op_peer_get_port,
            "peer_get_aid_pre": self._op_peer_get_aid_pre,
            "peer_import_blob": self._op_peer_import_blob,
            "peer_test_send": self._op_peer_test_send,
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
                    # QListWidget internally translates clicked(QModelIndex)
                    # into itemClicked(QListWidgetItem) — emitting only
                    # `clicked` triggers both signal chains. Previously this
                    # code emitted itemClicked AND clicked which fired the
                    # consumer slot twice (and opened duplicate dialogs).
                    lw.clicked.emit(lw.indexFromItem(item))
                    return {
                        "ok": True,
                        "list_object_name": lw.objectName(),
                        "item_text": item.text(),
                        "index": i,
                    }
        return {"error": f"list item not found: {item_text!r}"}

    def _op_click_table_row(self, cmd: dict[str, Any]) -> dict[str, Any]:
        """Click a row in a QTableWidget by matching any cell's text.

        QTableWidget items are not QWidgets, so `click`/`click_list_item`
        can't reach them. This walks visible QTableWidgets, finds the
        first row containing the target text in any cell, and emits
        cellClicked(row, 0) — which is what Locksmith's PaginatedTable
        listens for to fire its row_clicked signal.
        """
        text = cmd.get("text")
        if not text:
            return {"error": "text is required"}
        from PySide6.QtWidgets import QTableWidget
        for tw in self._window.findChildren(QTableWidget):
            if not tw.isVisible():
                continue
            for row in range(tw.rowCount()):
                for col in range(tw.columnCount()):
                    item = tw.item(row, col)
                    if item is None:
                        continue
                    if item.text().strip() == text:
                        tw.selectRow(row)
                        # Emit both cellPressed and cellClicked since
                        # different consumers may listen on either signal.
                        # Locksmith's PaginatedTable listens on cellPressed.
                        tw.cellPressed.emit(row, 0)
                        tw.cellClicked.emit(row, 0)
                        return {
                            "ok": True,
                            "row": row,
                            "matched_col": col,
                            "cell_text": item.text(),
                        }
        return {"error": f"table row not found: {text!r}"}

    def _op_click_row_action(self, cmd: dict[str, Any]) -> dict[str, Any]:
        """Trigger a row-action menu item (e.g. "Rotate", "Delete") for the
        row whose any cell matches ``row_text``. Acts on PaginatedTable's
        Actions column, which is set up with a SkewersMenuButton via
        ``setCellWidget``.

        Args:
            row_text: text in any cell of the target row (e.g. an alias).
            action: name of the action to trigger (case-sensitive, must
                match the SkewersMenuButton's actions_list).

        We trigger the action by emitting the SkewersMenuButton's
        ``action_triggered`` signal directly — the same signal the QMenu
        items would fire — so the rest of the row-action handler stack
        runs exactly as if the user had clicked through the popup.
        """
        row_text = cmd.get("row_text")
        action = cmd.get("action")
        if not row_text or not action:
            return {"error": "row_text and action are required"}
        from PySide6.QtWidgets import QTableWidget
        # Lazy import — Locksmith may not be on PYTHONPATH from harness tests
        try:
            from locksmith.ui.toolkit.tables.components import SkewersMenuButton
        except Exception:  # noqa: BLE001
            SkewersMenuButton = None

        for tw in self._window.findChildren(QTableWidget):
            if not tw.isVisible():
                continue
            for row in range(tw.rowCount()):
                row_matches = False
                for col in range(tw.columnCount()):
                    item = tw.item(row, col)
                    if item is not None and item.text().strip() == row_text:
                        row_matches = True
                        break
                if not row_matches:
                    continue
                # Find the SkewersMenuButton in any cell widget on this row.
                for col in range(tw.columnCount()):
                    cell = tw.cellWidget(row, col)
                    if cell is None:
                        continue
                    candidates = [cell]
                    if SkewersMenuButton is not None:
                        candidates.extend(cell.findChildren(SkewersMenuButton))
                    for candidate in candidates:
                        if SkewersMenuButton is None or not isinstance(candidate, SkewersMenuButton):
                            continue
                        if action not in candidate.actions_list:
                            return {
                                "error": f"action {action!r} not available on row "
                                         f"{row_text!r}; available={candidate.actions_list}",
                            }
                        candidate.action_triggered.emit(action)
                        return {
                            "ok": True,
                            "row": row,
                            "action": action,
                            "row_text": row_text,
                        }
                return {
                    "error": f"row {row_text!r} matched but no SkewersMenuButton found",
                }
        return {"error": f"table row not found: {row_text!r}"}

    def _op_type(self, cmd: dict[str, Any]) -> dict[str, Any]:
        target = cmd.get("target")
        text = cmd.get("text", "")
        if not target:
            return {"error": "target is required"}
        widget = self._find_widget(target)
        if widget is None:
            return {"error": f"widget not found: {target!r}"}
        # Drill into wrapper widgets that expose an inner QLineEdit /
        # QTextEdit. Locksmith's FloatingLabelLineEdit stores the actual
        # input as `.line_edit`; calling setText on the wrapper writes to
        # the floating label instead of the input. Match on attribute, not
        # class, so we handle any wrapper that follows this pattern.
        from PySide6.QtWidgets import QLineEdit, QPlainTextEdit, QTextEdit
        inner = (
            getattr(widget, "line_edit", None)
            or getattr(widget, "text_edit", None)
            or getattr(widget, "plain_text_edit", None)
        )
        if isinstance(inner, (QLineEdit, QTextEdit, QPlainTextEdit)):
            widget = inner
        if isinstance(widget, QPlainTextEdit):
            widget.setPlainText(text)
            return {"ok": True, "wrote_to": type(widget).__name__}
        # QSpinBox / QDoubleSpinBox: accept numeric text and call setValue
        from PySide6.QtWidgets import QSpinBox, QDoubleSpinBox
        if isinstance(widget, (QSpinBox, QDoubleSpinBox)):
            try:
                value = float(text) if isinstance(widget, QDoubleSpinBox) else int(text)
            except ValueError:
                return {"error": f"{type(widget).__name__} expects numeric text, got {text!r}"}
            widget.setValue(value)
            return {"ok": True, "wrote_to": type(widget).__name__, "value": value}
        # Editable QComboBox: drive the inner QLineEdit. setCurrentText
        # on an editable combo with a value not in its item list updates
        # the visible text but doesn't fire signals reliably; routing
        # through the lineEdit's setText mirrors a user typing.
        from PySide6.QtWidgets import QComboBox
        if isinstance(widget, QComboBox) and widget.isEditable():
            line = widget.lineEdit()
            if line is not None:
                line.setText(text)
                return {"ok": True, "wrote_to": f"{type(widget).__name__}.lineEdit"}
        if hasattr(widget, "setText"):
            widget.setText(text)
            return {"ok": True, "wrote_to": type(widget).__name__}
        return {"error": f"widget {type(widget).__name__} has no setText/setPlainText"}

    def _op_select(self, cmd: dict[str, Any]) -> dict[str, Any]:
        """Select an item in a QComboBox by visible text.

        Drives through ``findText`` + ``setCurrentIndex`` rather than
        ``setCurrentText``: ExtensibleSelectorWidget (and many other
        consumers) listen on ``currentIndexChanged``, which fires only
        on an actual index change. ``setCurrentText`` on a non-editable
        combo is a no-op when the text already matches, and even when
        it does change the index, Qt's behavior is inconsistent across
        versions about whether the signal fires.

        Also accepts ``index`` (int) for cases where the visible text
        is ambiguous (two items named "Select Witnesses", etc.).

        Optional ``occurrence`` (int, 0-based) picks among multiple
        widgets that share a label/objectName — useful when a dialog
        has two structurally-identical combos for "Add" and "Remove".
        """
        target = cmd.get("target")
        value = cmd.get("value")
        index = cmd.get("index")
        occurrence = cmd.get("occurrence", 0)
        if not target:
            return {"error": "target is required"}
        if value is None and index is None:
            return {"error": "value or index is required"}

        widget = self._find_widget(target, occurrence=occurrence)
        if widget is None:
            return {"error": f"widget not found: {target!r} (occurrence={occurrence})"}

        # FloatingLabelComboBox wraps a QComboBox child; resolve to it.
        combo = widget
        if not hasattr(combo, "setCurrentIndex") or not hasattr(combo, "findText"):
            inner = getattr(combo, "combo_box", None) or getattr(combo, "_combo", None)
            if inner is not None:
                combo = inner
        if not hasattr(combo, "setCurrentIndex") or not hasattr(combo, "findText"):
            return {"error": f"widget {type(widget).__name__} is not a QComboBox"}

        if index is not None:
            target_idx = int(index)
        else:
            target_idx = combo.findText(str(value))
            if target_idx < 0:
                # Enumerate available items for the diagnostic — much
                # easier to debug than "select failed silently".
                items = [combo.itemText(i) for i in range(combo.count())]
                return {
                    "error": f"value {value!r} not found in combo "
                             f"{target!r}; available={items}",
                }

        # Force currentIndexChanged to fire even if we're "setting" the
        # same index (rare, but ExtensibleSelectorWidget resets to -1
        # after each pick — the natural next pick may be the same index
        # as before from the user's POV). Toggling through -1 mirrors
        # what a real click does.
        if combo.currentIndex() == target_idx:
            combo.setCurrentIndex(-1)
        combo.setCurrentIndex(target_idx)
        return {
            "ok": True,
            "selected_index": target_idx,
            "selected_text": combo.itemText(target_idx),
        }

    # ----- state inspection (Cypress-like assertions) ---------------

    def _op_get_text(self, cmd: dict[str, Any]) -> dict[str, Any]:
        """Read the visible text of a widget.

        Resolves wrapper widgets (FloatingLabelLineEdit → inner line_edit,
        FloatingLabelComboBox → currentText, etc.) so callers get what
        the user actually sees. For widgets without a meaningful text
        method, returns empty string.
        """
        target = cmd.get("target")
        occurrence = cmd.get("occurrence", 0)
        if not target:
            return {"error": "target is required"}
        widget = self._find_widget(target, occurrence=occurrence)
        if widget is None:
            return {"error": f"widget not found: {target!r}"}

        from PySide6.QtWidgets import (
            QComboBox, QLabel, QLineEdit, QPlainTextEdit, QSpinBox,
            QDoubleSpinBox, QTextEdit, QAbstractButton,
        )

        inner = (
            getattr(widget, "line_edit", None)
            or getattr(widget, "text_edit", None)
            or getattr(widget, "combo_box", None)
            or getattr(widget, "_combo", None)
        )
        if inner is not None:
            widget = inner

        if isinstance(widget, QComboBox):
            return {"ok": True, "text": widget.currentText()}
        if isinstance(widget, (QSpinBox, QDoubleSpinBox)):
            return {"ok": True, "text": str(widget.value())}
        if isinstance(widget, (QLineEdit,)):
            return {"ok": True, "text": widget.text()}
        if isinstance(widget, (QTextEdit, QPlainTextEdit)):
            return {"ok": True, "text": widget.toPlainText()}
        # QLabel / QAbstractButton both expose .text()
        if isinstance(widget, (QLabel, QAbstractButton)):
            return {"ok": True, "text": widget.text()}
        if hasattr(widget, "text"):
            try:
                return {"ok": True, "text": widget.text()}
            except Exception:  # noqa: BLE001
                pass
        return {"ok": True, "text": ""}

    def _op_is_checked(self, cmd: dict[str, Any]) -> dict[str, Any]:
        """True if a QCheckBox / QRadioButton / checkable QPushButton
        is currently checked. Errors if the widget has no isChecked
        method (so callers fail loudly instead of silently treating a
        non-checkable widget as 'not checked').
        """
        target = cmd.get("target")
        occurrence = cmd.get("occurrence", 0)
        if not target:
            return {"error": "target is required"}
        widget = self._find_widget(target, occurrence=occurrence)
        if widget is None:
            return {"error": f"widget not found: {target!r}"}
        if not hasattr(widget, "isChecked"):
            return {"error": f"widget {type(widget).__name__} has no isChecked"}
        return {"ok": True, "checked": bool(widget.isChecked())}

    def _op_is_visible(self, cmd: dict[str, Any]) -> dict[str, Any]:
        """True if a widget exists in the tree AND is currently visible.

        Visibility here means Qt's isVisible() — the widget is on a
        currently-displayed page/dialog. Find uses the same selector
        semantics as click/type/select but does NOT require visibility,
        so callers can poll for "appears soon."
        """
        target = cmd.get("target")
        occurrence = cmd.get("occurrence", 0)
        if not target:
            return {"error": "target is required"}
        widget = self._find_widget_any(target, occurrence=occurrence)
        if widget is None:
            return {"ok": True, "visible": False, "exists": False}
        return {"ok": True, "visible": widget.isVisible(), "exists": True}

    def _op_wait_for(self, cmd: dict[str, Any]) -> dict[str, Any]:
        """Poll until a widget matches a condition, or timeout.

        ``condition`` is one of "visible" (default) or "hidden".
        ``timeout_ms`` defaults to 5000. Polls every 50ms by processing
        Qt events between checks so dialogs that open asynchronously
        can settle.
        """
        from PySide6.QtCore import QElapsedTimer, QCoreApplication
        target = cmd.get("target")
        condition = cmd.get("condition", "visible")
        timeout_ms = int(cmd.get("timeout_ms", 5000))
        occurrence = cmd.get("occurrence", 0)
        if not target:
            return {"error": "target is required"}
        if condition not in ("visible", "hidden"):
            return {"error": f"condition must be visible|hidden, got {condition!r}"}

        timer = QElapsedTimer()
        timer.start()
        while True:
            widget = self._find_widget_any(target, occurrence=occurrence)
            satisfied = (
                (condition == "visible" and widget is not None and widget.isVisible())
                or (condition == "hidden" and (widget is None or not widget.isVisible()))
            )
            if satisfied:
                return {"ok": True, "elapsed_ms": timer.elapsed()}
            if timer.elapsed() >= timeout_ms:
                return {
                    "error": f"timeout waiting for {target!r} to be {condition} "
                             f"after {timeout_ms}ms",
                }
            QCoreApplication.processEvents()
            # 50ms sleep without blocking the Qt main thread
            from PySide6.QtCore import QThread
            QThread.msleep(50)

    def _op_count(self, cmd: dict[str, Any]) -> dict[str, Any]:
        """Count visible widgets matching a selector.

        Useful for "list has N entries" assertions. Uses the same
        selector grammar as find — objectName / text / tooltip / Type:N.
        For Type:N selectors, returns the total number of visible widgets
        of that type (ignores the :N suffix in the count).
        """
        target = cmd.get("target")
        if not target:
            return {"error": "target is required"}
        # Strip a Type:N suffix; for counting we want the total of that type.
        type_only = target
        if ":" in target:
            head, _, tail = target.partition(":")
            try:
                int(tail)
                type_only = head
            except ValueError:
                type_only = target

        count = 0
        for w in self._window.findChildren(QWidget):
            if not w.isVisible():
                continue
            if type(w).__name__ == type_only:
                count += 1
                continue
            if w.objectName() == target:
                count += 1
                continue
            if hasattr(w, "text"):
                try:
                    if w.text().strip() == target:
                        count += 1
                        continue
                except Exception:  # noqa: BLE001
                    pass
        return {"ok": True, "count": count}

    def _op_get_list_items(self, cmd: dict[str, Any]) -> dict[str, Any]:
        """Read the text content of a QListWidget as a list of strings.

        Each entry is the visible text of the QListWidgetItem. If a row
        has a UserRole-stored value (common pattern for storing an AID
        per-row), it's available under the `data` field of the
        per-item dict. Returns {ok: True, items: [{text, data}, ...]}.
        """
        from PySide6.QtWidgets import QListWidget
        target = cmd.get("target")
        occurrence = cmd.get("occurrence", 0)
        if not target:
            return {"error": "target is required"}
        widget = self._find_widget(target, occurrence=occurrence)
        if widget is None:
            return {"error": f"widget not found: {target!r}"}
        if not isinstance(widget, QListWidget):
            return {"error": f"{target!r} is {type(widget).__name__}, not QListWidget"}

        items = []
        for i in range(widget.count()):
            it = widget.item(i)
            items.append({
                "text": it.text() if it is not None else "",
                "data": it.data(Qt.UserRole) if it is not None else None,
            })
        return {"ok": True, "items": items}

    def _op_get_table_rows(self, cmd: dict[str, Any]) -> dict[str, Any]:
        """Read the text content of a QTableWidget as a list of row dicts.

        Each row is a dict mapping column header → cell text. If the
        table has no horizontal header labels, falls back to col_0, col_1.
        Returns {ok: True, rows: [{...}, ...]}.
        """
        from PySide6.QtWidgets import QTableWidget
        target = cmd.get("target")
        occurrence = cmd.get("occurrence", 0)
        if not target:
            return {"error": "target is required"}
        widget = self._find_widget(target, occurrence=occurrence)
        if widget is None:
            return {"error": f"widget not found: {target!r}"}
        if not isinstance(widget, QTableWidget):
            return {"error": f"{target!r} is {type(widget).__name__}, not QTableWidget"}

        headers = []
        for col in range(widget.columnCount()):
            h = widget.horizontalHeaderItem(col)
            headers.append(h.text() if h is not None else f"col_{col}")

        rows = []
        for row in range(widget.rowCount()):
            row_dict = {}
            for col in range(widget.columnCount()):
                item = widget.item(row, col)
                row_dict[headers[col]] = item.text() if item is not None else ""
            rows.append(row_dict)
        return {"ok": True, "rows": rows, "headers": headers}

    def _find_widget_any(self, target: str, occurrence: int = 0) -> QWidget | None:
        """Like _find_widget but includes invisible widgets in the search.
        Used by is_visible / wait_for so callers can distinguish
        "widget doesn't exist" from "widget exists but is hidden."
        """
        # Type:N selector path
        if ":" in target:
            type_name, _, idx_str = target.partition(":")
            try:
                idx = int(idx_str)
                if type_name:
                    hits = [w for w in self._window.findChildren(QWidget)
                            if type(w).__name__ == type_name]
                    return hits[idx] if 0 <= idx < len(hits) else None
            except ValueError:
                pass

        matches = []
        for w in self._window.findChildren(QWidget):
            if w.objectName() == target:
                matches.append(w)
                continue
            if hasattr(w, "text"):
                try:
                    if isinstance(w.text(), str) and w.text().strip() == target:
                        matches.append(w)
                        continue
                except Exception:  # noqa: BLE001
                    pass
            try:
                if isinstance(w.toolTip(), str) and w.toolTip().strip() == target:
                    matches.append(w)
                    continue
            except Exception:  # noqa: BLE001
                pass
            lt = getattr(w, "label_text", None) or getattr(w, "_label_text", None)
            if isinstance(lt, str) and lt.strip() == target:
                matches.append(w)
        return matches[occurrence] if 0 <= occurrence < len(matches) else None

    # ----- widget lookup --------------------------------------------

    def _find_widget(self, target: str, occurrence: int = 0) -> QWidget | None:
        """Find a visible widget by selector.

        Selectors supported:
          - exact `objectName`
          - exact `.text()` (for widgets that have a text method)
          - exact `.toolTip()` (for icon buttons)
          - `Type:N` — the N-th visible widget whose class name is `Type`
            (e.g. "QLineEdit:0", "LocksmithButton:1"). Index respects
            widget-tree order. Useful when widgets lack names/text.

        ``occurrence`` picks the Nth structurally-identical match in
        widget-tree order (e.g. a dialog with two combos labeled "Select
        Witnesses"). Defaults to 0 (first match). Same widget-tree order
        as the Type:N selector.
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
                continue
            # Locksmith's MenuButton stores its visible label in a
            # `label_text` attribute. FloatingLabelLineEdit stores the
            # same idea under `_label_text` (private). Match either so
            # the vault navigation menu AND input wrappers are reachable
            # by their visible label.
            lt = getattr(w, "label_text", None) or getattr(w, "_label_text", None)
            if isinstance(lt, str) and lt.strip() == target:
                matches.append(w)
        # When two widgets compete for the same label (e.g. a
        # FloatingLabelLineEdit AND its inner QLabel both expose
        # "Passcode"), prefer the wrapper — that's the input widget the
        # user expects to interact with. Wrapper detection covers
        # FloatingLabelLineEdit (.line_edit), FloatingLabelPlainTextEdit
        # (.text_edit), and FloatingLabelComboBox (.combo_box). Without
        # this, a `select` against a label shared by N combos would pick
        # up the floating QLabels as part of the occurrence list and
        # misalign indices.
        if len(matches) > 1:
            wrappers = [
                w for w in matches
                if hasattr(w, "line_edit")
                or hasattr(w, "text_edit")
                or hasattr(w, "combo_box")
            ]
            if wrappers:
                matches = wrappers
        if 0 <= occurrence < len(matches):
            return matches[occurrence]
        return matches[0] if matches and occurrence == 0 else None

    # ----- peer-mode helpers ----------------------------------------

    def _app(self):
        return getattr(self._window, "app", None)

    def _vault(self):
        app = self._app()
        return getattr(app, "vault", None) if app else None

    def _op_peer_open_test_vault(self, cmd: dict[str, Any]) -> dict[str, Any]:
        """Create + open a vault programmatically for tests.

        Bypasses the open-vault dialog entirely; matches what the dialog
        does internally (format_bran -> stretch -> habbing.Habery ->
        run_vault_controller).
        """
        from keri.app import habbing as kerihabbing
        from keri.core import signing
        from keri.vdr import credentialing
        from locksmith.core.habbing import (
            format_bran, keystore_exists, open_hby,
        )
        from locksmith.core.crypto import stretch_password_to_passcode

        name = cmd.get("name") or "peer_test"
        passcode = cmd.get("passcode") or "DoB2-e4Rr-gVOr-Nb1Y-7yBl-gI3n-i4cB-gf07"
        app = self._app()
        if app is None:
            return {"error": "no app"}

        bran = stretch_password_to_passcode(format_bran(passcode))
        base = app.config.base
        if not keystore_exists(name, base):
            salt = signing.Salter(raw=app.config.salt.encode("utf-8")).qb64
            hby = kerihabbing.Habery(
                name=name, base=base, bran=bran, salt=salt,
                algo=app.config.algo, tier=app.config.tier,
            )
            hby.close()

        try:
            vault, qtask = open_hby(name=name, base=base, bran=bran, app=app)
            # Optionally enable peer mode at the desired port. Vault.__init__
            # has already run with the (default) settings — peer_doer is
            # None. We pin the desired settings, then call
            # restart_peer_mode, which on a None peer_doer simply
            # constructs + extends. This avoids the dead-doer crash from
            # restarting an already-running peer doer.
            peer = cmd.get("peer") or {}
            if peer:
                from locksmith.peer.records import PeerModeSettings
                vault.db.peerSettings.pin(keys=("default",), val=PeerModeSettings(
                    enabled=bool(peer.get("enabled", True)),
                    port=int(peer.get("port", 5621)),
                    bind_host=peer.get("bind_host", "127.0.0.1"),
                    advertised_host=peer.get("advertised_host", "127.0.0.1"),
                ))
                vault.restart_peer_mode()
            app.open_vault(name=name, vault=vault, qtask=qtask)
            # Trigger UI navigation to the vault page so the rest of the
            # peer-mode UI is reachable through normal devctl click ops.
            try:
                from locksmith.ui.navigation import Pages
                window = self._window
                if hasattr(window, "nav_manager"):
                    window.nav_manager.navigate_to(Pages.VAULT, vault_name=name)
            except Exception:
                pass  # navigation is a UX nicety, not required for the op
            return {"ok": True, "name": name}
        except Exception as e:  # noqa: BLE001
            return {"error": f"open failed: {e}"}

    def _op_peer_create_test_aid(self, cmd: dict[str, Any]) -> dict[str, Any]:
        """Create a transferable AID with no witnesses (for direct-mode tests).

        Fires the same `identifier_created` signal the real InceptDoer
        emits so the IdentifierListPage refreshes and the new AID shows
        up as a clickable row. Without this, harness-driven UI tests
        that try to click the row never see it.
        """
        vault = self._vault()
        if vault is None:
            return {"error": "no vault open"}
        alias = cmd.get("alias")
        if not alias:
            return {"error": "alias is required"}
        if vault.hby.habByName(alias) is not None:
            return {"ok": True, "aid": vault.hby.habByName(alias).pre, "existing": True}
        try:
            hab = vault.hby.makeHab(name=alias, transferable=True, wits=[], toad=0)
        except Exception as e:  # noqa: BLE001
            return {"error": f"makeHab failed: {e}"}
        signals = getattr(vault, "signals", None)
        if signals is not None:
            try:
                signals.emit_doer_event(
                    doer_name="InceptDoer",
                    event_type="identifier_created",
                    data={"alias": alias, "pre": hab.pre, "success": True},
                )
            except Exception:  # noqa: BLE001
                pass  # signal best-effort; harness state still consistent
        return {"ok": True, "aid": hab.pre}

    def _op_peer_force_pair(self, cmd: dict[str, Any]) -> dict[str, Any]:
        """Directly insert a PeerRecord into the allowlist, bypassing OOBI
        resolution. Test-only — real users use the Add Peer dialog.
        """
        from datetime import datetime, timezone
        from locksmith.peer.allowlist import PeerAllowlist
        from locksmith.peer.records import PeerRecord
        vault = self._vault()
        if vault is None:
            return {"error": "no vault open"}
        aid = cmd.get("aid")
        endpoint_url = cmd.get("endpoint_url")
        if not aid or not endpoint_url:
            return {"error": "aid and endpoint_url are required"}
        PeerAllowlist(vault.db).add(PeerRecord(
            aid=aid,
            label=cmd.get("label") or aid[:12],
            endpoint_url=endpoint_url,
            paired_at=datetime.now(timezone.utc).isoformat(),
        ))
        return {"ok": True}

    def _op_peer_list(self, cmd: dict[str, Any]) -> dict[str, Any]:
        from locksmith.peer.allowlist import PeerAllowlist
        vault = self._vault()
        if vault is None:
            return {"error": "no vault open"}
        al = PeerAllowlist(vault.db)
        return {
            "ok": True,
            "peers": [
                {"aid": r.aid, "label": r.label, "endpoint_url": r.endpoint_url}
                for r in al.list()
            ],
        }

    def _op_peer_get_port(self, cmd: dict[str, Any]) -> dict[str, Any]:
        vault = self._vault()
        if vault is None or vault.peer_doer is None or vault.peer_doer.server is None:
            return {"error": "peer mode not running"}
        ha = vault.peer_doer.server.ha
        # hio Server.ha is (host, port) after bind
        try:
            host, port = ha
        except (TypeError, ValueError):
            return {"error": f"unexpected ha shape: {ha!r}"}
        return {"ok": True, "host": host, "port": port}

    def _op_peer_get_aid_pre(self, cmd: dict[str, Any]) -> dict[str, Any]:
        vault = self._vault()
        if vault is None:
            return {"error": "no vault open"}
        alias = cmd.get("alias")
        hab = vault.hby.habByName(alias) if alias else None
        if hab is None:
            return {"error": f"no hab {alias!r}"}
        return {"ok": True, "aid": hab.pre}

    def _op_peer_import_blob(self, cmd: dict[str, Any]) -> dict[str, Any]:
        """Mirror Add Peer dialog blob path: import the blob into the
        vault's Habery (parses KEL + role/loc) and add the peer to the
        allowlist with the blob's tcp endpoint.

        Returns {ok: True, aid, endpoint_url}. label defaults to aid[:12].
        """
        from datetime import datetime, timezone
        from keri import kering
        from locksmith.peer.allowlist import PeerAllowlist
        from locksmith.peer.cesr_blob import (
            PeerBlobError, import_peer_blob,
        )
        from locksmith.peer.records import PeerRecord
        vault = self._vault()
        if vault is None:
            return {"error": "no vault open"}
        blob = cmd.get("blob")
        if not blob:
            return {"error": "blob is required"}
        try:
            aid = import_peer_blob(vault.hby, blob)
        except PeerBlobError as e:
            return {"error": f"{e.reason}: {e}"}
        loc = vault.hby.db.locs.get(keys=(aid, kering.Schemes.tcp))
        endpoint_url = cmd.get("endpoint_url") or (loc.url if loc else "")
        if not endpoint_url:
            return {"error": "no tcp endpoint in blob"}
        PeerAllowlist(vault.db).add(PeerRecord(
            aid=aid,
            label=cmd.get("label") or aid[:12],
            endpoint_url=endpoint_url,
            paired_at=datetime.now(timezone.utc).isoformat(),
        ))
        return {"ok": True, "aid": aid, "endpoint_url": endpoint_url}

    def _op_peer_test_send(self, cmd: dict[str, Any]) -> dict[str, Any]:
        """Drive peer_send with given recipient + bytes. Stub mailbox
        callback records whether fallback was attempted. Test-only."""
        from locksmith.peer.allowlist import PeerAllowlist
        from locksmith.peer.sending import peer_send
        vault = self._vault()
        if vault is None:
            return {"error": "no vault open"}
        recipient_aid = cmd.get("recipient_aid")
        payload_str = cmd.get("payload", "")
        if not recipient_aid:
            return {"error": "recipient_aid is required"}
        payload = payload_str.encode("utf-8") if isinstance(payload_str, str) else bytes(payload_str)

        mailbox_calls = []

        def stub_mailbox(aid, bs):
            mailbox_calls.append((aid, len(bs)))
            return True

        outcome = peer_send(
            allowlist=PeerAllowlist(vault.db),
            recipient_aid=recipient_aid,
            exn_bytes=payload,
            mailbox_send=stub_mailbox,
        )
        return {
            "ok": True,
            "outcome": outcome.value,
            "mailbox_calls": mailbox_calls,
        }
