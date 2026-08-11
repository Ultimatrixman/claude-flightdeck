---
description: Wire the quota capture into your statusline (the one thing a plugin cannot install itself)
argument-hint: ""
---

Install ctxmon's statusline wrapper.

Everything else in ctxmon works the moment the plugin is installed. This step
exists for one reason: the 5-hour and 7-day **quota** figures are delivered by
Claude Code *only* to the statusline command, and a plugin cannot ship a
`statusLine`. Without this, context telemetry works and quota does not.

Throughout, `<flightdeck>` means `$FLIGHTDECK_DIR`, else
`$CLAUDE_CONFIG_DIR/flightdeck`, else `~/.claude/flightdeck`.

Do this:

1. Read `~/.claude/settings.json` (or `$CLAUDE_CONFIG_DIR/settings.json`).

2. Note the current `statusLine.command`, if any.
   - If it already ends in `flightdeck/statusline.sh`, tell the user it is
     installed and stop.

3. Back up the file to `settings.json.bak.flightdeck-<YYYYMMDD-HHMMSS>` and tell
   the user the backup path.

4. **Copy this plugin's `statusline.sh` to `<flightdeck>/statusline.sh`.**
   Do not point `statusLine` at the file inside the plugin directory. That path
   contains the plugin version (`.../flightdeck/0.1.1/statusline.sh`) and
   changes on every update, which would break the statusline the next time the
   plugin upgrades. The copy is version-stable; re-run this command after an
   upgrade to refresh it.

5. Write the previous command from step 2, verbatim and unmodified, to
   `<flightdeck>/ctxmon/statusline-inner.sh`. Create the directory if needed.
   **If there was no previous statusline, create the file empty** — an empty
   file means "there was nothing", which is not the same as the file being
   absent, and the wrapper distinguishes them.

6. Set `statusLine` to:
   ```json
   { "type": "command", "command": "bash \"${CLAUDE_CONFIG_DIR:-$HOME/.claude}/flightdeck/statusline.sh\"" }
   ```

7. Tell the user to restart Claude Code, and that their existing statusline is
   unchanged: the wrapper captures the payload and hands the identical bytes
   straight through to it.

To undo: restore `statusLine.command` from the backup, or from
`<flightdeck>/ctxmon/statusline-inner.sh`.
