---
description: This session's context budget, band, phase and agent count
argument-hint: ""
---

Show this session's budget:

```
"${CLAUDE_PLUGIN_ROOT}/bin/ctxmon" status
```

(If that variable does not resolve in your shell, use this plugin's real
installed path.)

Show the output as-is. Two readings need care:

- **`safe headroom` is not "until auto-compact".** It is the window minus a
  16.5% working reserve. Auto-compact was never observed to fire in testing —
  a session ran to 98.5% of a 1M window with no compaction — so treat the
  reserve as margin you chose to keep, not a cliff the harness enforces.
- **`+≤N uncounted`** is an upper-bound estimate of tool output accumulated
  since the last API round trip, derived from transcript bytes. The
  authoritative figure arrives with the next request.

If several sessions share this working directory, `status` may report a
different one than you expect; pass `--session <id-prefix>`, or use
the `peers` command, which is unambiguous.
