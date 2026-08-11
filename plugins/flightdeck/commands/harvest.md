---
description: Write this session's provenance trail now - what was asked, read, run and dispatched
argument-hint: "[session-id-prefix]"
---

Write the provenance trail:

```
"${CLAUDE_PLUGIN_ROOT}/bin/ctxmon" harvest $ARGUMENTS
```

(If that variable does not resolve in your shell, use this plugin's real
installed path. `$ARGUMENTS`, when given, is `--session <id-prefix>`.)

This runs automatically at `PreCompact`. Run it by hand before a manual
`/compact`, at the end of a long session, or against another session that ended
without one.

The trail is **provenance, not a summary**: what was asked, every file touched
with read/write counts, every agent dispatched, and every command verbatim. A
compaction summary keeps conclusions and drops the evidence behind them; this
keeps the evidence, so a measurement can be re-run rather than re-derived.

After writing it, tell the user the path and the totals line.

Then do the part the file cannot do for you: if this session discovered
anything durable — a fact about the codebase, a decision and its reason, an
open thread — write it somewhere that outlives the context window now, while
the detail is still in front of you. Notes, docs, tasks, or memory, whichever
this project uses.

**Commands are redacted for credential-shaped strings before writing**, but
redaction is pattern matching, not proof. Treat a trail as you would your shell
history.
