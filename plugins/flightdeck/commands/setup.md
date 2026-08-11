---
description: Wire the quota capture into your statusline (the one thing a plugin cannot install itself)
argument-hint: ""
---

Install ctxmon's statusline wrapper.

Everything else in ctxmon works the moment the plugin is installed. This step
exists for one reason: the 5-hour and 7-day **quota** figures are delivered by
Claude Code *only* to the statusline command, and a plugin cannot ship a
`statusLine` setting. Without this, context telemetry works and quota does not.

Do this:

1. Read `~/.claude/settings.json` (or `$CLAUDE_CONFIG_DIR/settings.json`).

2. Note the current `statusLine.command`, if any.
   - If it is **already** the ctxmon wrapper (its path ends in
     `plugins/ctxmon/statusline.sh`), tell the user it is already installed and
     stop.

3. Back up the file to `settings.json.bak.flightdeck-<YYYYMMDD-HHMMSS>` and
   tell the user the backup path.

4. Write the previous command, verbatim and unmodified, to
   `<flightdeck>/ctxmon/statusline-inner.sh`, where `<flightdeck>` is
   `$FLIGHTDECK_DIR` or `$CLAUDE_CONFIG_DIR/flightdeck` or `~/.claude/flightdeck`.
   Create the directory if needed. **If there was no previous statusline,
   create the file empty** — an empty file means "there was nothing", which is
   not the same as the file being absent, and the wrapper distinguishes them.

5. Set `statusLine` to:
   ```json
   { "type": "command", "command": "bash \"<plugin-root>/statusline.sh\"" }
   ```
   using this plugin's real installed path for `<plugin-root>`.

6. Tell the user to restart Claude Code, and that their existing statusline is
   unchanged — the wrapper captures the payload and hands the identical bytes
   straight through to it.

To undo: restore `statusLine.command` from the backup, or from
`statusline-inner.sh`.
