# locksmith-ui-tester

> **⚠️ Dev tool only.** This plugin opens a UNIX socket at `/tmp/locksmith-control.sock` that lets **any local process** drive the Locksmith wallet UI — inspect widgets, click buttons, type text, read screenshots. Install only on a development machine. **Do NOT install on a wallet that holds real keys.**

An installable Locksmith plugin that exposes a JSON-over-unix-socket control surface for driving the running UI from test scripts, dev loops, or AI-assisted development.

## What it does

When installed and not excluded, the plugin starts a `DevControlServer` while the wallet is open. The server accepts newline-delimited JSON commands and replies with JSON results, all on the Qt main thread:

| Op | What it does |
|---|---|
| `ping` | Liveness check |
| `screenshot` | Save a PNG of the main window |
| `tree` | Enumerate visible widgets with type, rect, text, tooltip |
| `current_page` | Report the current vault sub-page key |
| `click` | Click a widget by objectName / text / tooltip / `Type:N` selector |
| `click_list_item` | Click an item in a QListWidget by its text |
| `type` | Type into a QLineEdit by selector |
| `select` | Set a QComboBox value by selector |

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

The socket lives at `/tmp/locksmith-control.sock` with the file permissions Qt's `QLocalServer` sets by default — readable/writable by any process on the local system running as the same user. Anyone who can reach that socket can drive the wallet completely. This is the trust boundary. Install only where you accept that boundary.

## License

MIT.
