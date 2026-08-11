---
description: Every live session's context, band, phase, idle age and running agents
argument-hint: ""
---

Show the cross-session view:

```
"${CLAUDE_PLUGIN_ROOT}/bin/ctxmon" peers
```

(If that variable does not resolve in your shell, use this plugin's real
installed path.)

Show the output as-is. The column that matters most for distributing work is
**phase**, because nothing else on the machine records it:

- `idle 2m` — that session sent its final response two minutes ago. Its numbers
  are settled and it is available.
- `busy 8m` — it is mid-turn, its context is still growing, and its snapshot is
  a floor rather than a current reading.
- `(stale)` — that session has not fired a hook recently. Trust the context
  figure as a lower bound and nothing else.

A session in `HANDOFF` with high context should be given no new work; it needs
to finish and write its handoff. If the `ipc` plugin is installed, the
`claim` column shows what each session has declared it is working on.
