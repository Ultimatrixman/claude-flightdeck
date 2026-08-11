---
description: Can this work finish before the quota window closes? Burn rate, runway, and a verdict on starting agent work
argument-hint: ""
---

Run the scheduling planner:

```
"${CLAUDE_PLUGIN_ROOT}/bin/ctxmon" plan
```

(If that variable does not resolve in your shell, use this plugin's real
installed path.)

Show the user the output as-is, then act on the verdict for whatever they asked
you to do next:

- **safe to fan out** — start the wave.
- **one wave only** — start it, but do not queue a second batch behind it.
- **do NOT start agent work** — the runway is shorter than a p90 agent run. An
  agent killed mid-flight returns nothing **and its quota is spent anyway**. Do
  short inline work instead, or wait for the reset.

Two things to read carefully rather than skim:

- A `NOTE quota runs out … BEFORE the window resets` line means the current
  burn rate exhausts the allowance early. Pace the work or idle.
- `LOW CONFIDENCE (n<3)` on agent duration means the estimate rests on almost
  no history. Treat the verdict as weak and size work conservatively.

If quota is unknown, run `/ctxmon:setup` — quota reaches only the statusline.
