# locksmith-ui-tester

> **⚠️ Dev tool only.** This plugin opens a UNIX socket at `~/.locksmith-control.sock` (override with `LOCKSMITH_CONTROL_SOCKET`) that lets **any local process** drive the Locksmith wallet UI — inspect widgets, click buttons, type text, read screenshots. Install only on a development machine. **Do NOT install on a wallet that holds real keys.**

An installable Locksmith plugin that exposes a JSON-over-unix-socket control surface for driving the running UI from test scripts, dev loops, or AI-assisted development.

## What it does

When installed and not excluded, the plugin starts a `DevControlServer` while the wallet is open. The server accepts newline-delimited JSON commands and replies with JSON results, all on the Qt main thread:

| Op | What it does |
|---|---|
| `ping` | Liveness check |
| `screenshot` | Save a PNG of the main window (or a specific widget via `target`, e.g. a top-level dialog) |
| `tree` | Enumerate visible widgets with type, rect, text, tooltip |
| `current_page` | Report the current vault sub-page key |
| `click` | Click a widget by objectName / text / tooltip / `Type:N` selector |
| `click_list_item` | Click an item in a QListWidget by its text |
| `type` | Type into a QLineEdit by selector |
| `select` | Set a QComboBox value by selector (by `value` or bounds-checked `index`) |
| `is_enabled` | Report whether a widget exists and is enabled |

> This table documents a subset of the ops. `get_text`, `is_visible`, `is_checked`, `wait_for`, `count`, `get_table_rows`, `get_list_items`, `click_table_row`, and `click_row_action` are also available — send an unknown op to get the authoritative list back in the `available` field.

### Disabled targets are refused

The ops that drive the UI — `click`, `type`, `select`, `click_list_item`, `click_table_row`, `click_row_action` — return an `error` when their target is disabled, rather than reporting success for an action that didn't happen.

This matters because Qt's disabled state blocks *input events*, not *programmatic setters*. `QAbstractButton.click()` silently no-ops on a disabled button, but `setText()` and `setCurrentIndex()` succeed outright — so an unguarded `type` or `select` would drive the app into a state no user could reach and let the run continue against it.

The refusal carries the resolved widget under a `widget` key for diagnostics. To assert that a control is *correctly* inert, read it instead of driving it:

```bash
devctl is_enabled '{"target": "submitButton"}'      # {"ok": true, "enabled": false, "exists": true}
devctl wait_for '{"target": "submitButton", "condition": "enabled"}'
```

`wait_for` accepts `visible`, `hidden`, `enabled`, and `disabled`. The latter two require the widget to exist *and* be visible, so a passing `wait_for enabled` guarantees the following `click` can resolve the same target. Note that absence satisfies `hidden` but never `disabled` — a widget that isn't there is missing, not inert.

## Install

In the Locksmith Plugins UI:

- **GitHub source:** `seriouscoderone/locksmith-ui-tester`
- **Local path source:** point at this repo on disk

The install confirmation panel shows the manifest description — read it.

After install, restart the wallet to load the plugin.

## CLI

`pip install` exposes a `devctl` command on PATH:

```bash
devctl ping
devctl click '{"target": "Vaults"}'
devctl screenshot '{"path": "/tmp/wallet.png"}'
```

Or invoke it directly: `python -m locksmith_ui_tester.cli ping`.

## Security

The socket lives at `~/.locksmith-control.sock` by default (override via the `LOCKSMITH_CONTROL_SOCKET` env var or `devctl --socket`). Hosting it under HOME means two Locksmith instances launched with different `HOME` values get independent sockets automatically. File permissions are whatever Qt's `QLocalServer` sets by default — readable/writable by any process on the local system running as the same user. Anyone who can reach that socket can drive the wallet completely. This is the trust boundary. Install only where you accept that boundary.

## License

MIT.
