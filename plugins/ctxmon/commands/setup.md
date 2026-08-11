---
description: Wire the quota capture into your statusline (the one thing a plugin cannot install itself)
argument-hint: "[--dry-run | --uninstall]"
---

Run:

```
"${CLAUDE_PLUGIN_ROOT}/bin/ctxmon" setup $ARGUMENTS
```

(If that variable does not resolve in your shell, use this plugin's real
installed path.)

Show the user the output as-is, then stop. Do not edit `settings.json`
yourself: the command backs the file up, preserves every other key, records
whatever statusline was already there so it can be handed the payload
untouched, and is safe to run twice.

Why this step exists at all: the 5-hour and 7-day **quota** figures are
delivered by Claude Code only to the statusline command, and a plugin cannot
ship a `statusLine` key. Everything else in ctxmon works without it. Context
telemetry, the bands, the trail and the cross-session view all function; only
quota and the planner need this.

`--dry-run` prints what would change and writes nothing. `--uninstall` puts
your previous statusline back.

Re-run it after upgrading the plugin. The wrapper is installed to
`<flightdeck>/statusline.sh` rather than referenced inside the plugin
directory, because that path carries the plugin version and moves on every
upgrade; re-running refreshes the copy.
