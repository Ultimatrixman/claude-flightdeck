# claude-flightdeck

Instruments for running Claude Code sessions.

Claude Code shows *you* how much context and quota a session is using. It does
not tell **Claude**. The model has no way to query its own token count, learns
about compaction only after crossing it, and cannot see that the session in your
other terminal is at 94% and about to stall.

This fixes that, with two plugins:

- **`ctxmon`** — context and quota telemetry, a scheduling planner, a
  cross-session view, and a provenance trail written before compaction.
- **`ipc`** — a message channel between concurrent sessions: peer discovery,
  advisory work claims, and a `/ping` `/pong` chat rally.

They are independent. `ctxmon` is read-only. `ipc` binds `127.0.0.1:4180` and
runs a small relay daemon, which is a different decision — so it installs
separately.

## Install

```
/plugin marketplace add Ultimatrixman/claude-flightdeck
/plugin install flightdeck@claude-flightdeck     # both
```

Or pick one:

```
/plugin install ctxmon@claude-flightdeck
/plugin install ipc@claude-flightdeck
```

Then run **`/ctxmon:setup`** once. Everything works without it except quota:
the 5-hour and 7-day figures are delivered by Claude Code *only* to the
statusline command, and a plugin cannot ship a `statusLine`. Setup wraps
whatever statusline you already have (including none) and hands the identical
bytes straight through, so nothing about your display changes.

<details>
<summary>Standalone install, from a clone</summary>

```bash
git clone https://github.com/Ultimatrixman/claude-flightdeck
cd claude-flightdeck
python3 install.py              # both components + statusline capture
python3 install.py --dry-run    # print resulting settings, write nothing
python3 install.py --uninstall  # remove everything it added
```

It backs up `settings.json` first, appends to existing hook arrays rather than
replacing them, and will not touch an entry it did not create.
</details>

## What you get

A one-line budget report at the start of every turn, roughly 40 tokens:

```
[ctx] 322k/1.00M (32.2%) · 513k safe headroom · out 157k · quota 5h 18%/7d 28% (resets 4h) · NORMAL
```

A **threshold alarm** that stays silent — it prints nothing at steady state and
speaks only when a band is newly crossed or one tool result was unusually large.
This is the layer that matters, because a single turn can burn six figures of
tokens between your messages.

Four bands, expressed as fractions of usable budget so they mean the same thing
on a 200k window as on a 1M one:

| Band | Directive |
|---|---|
| `NORMAL` | work directly |
| `CONSERVE` | delegate file-heavy search to subagents; background long runs |
| `HARVEST` | write down what the session **learned** — memory, tasks, docs |
| `HANDOFF` | stop starting new work; finish the harvest |

### `/ctxmon:plan` — the scheduling question

```
5-hour window   57% used, resets in 2h
spent so far    1.88M proxy tokens across 3 session(s), 4 agent run(s)
implied budget  ~3.30M proxy tokens per 5h window
burn rate       9.8%/h
projected       quota exhausted in 3h · window resets in 4h
agent duration  n=4  median 46m  p90 1h

VERDICT  one wave only. 2h of runway against a p90 agent of 1h: start the wave
         now, do not queue a second.
         NOTE quota runs out 40m BEFORE the window resets. Slow down or idle.
```

An agent killed mid-flight returns nothing **and its quota is spent anyway**, so
the planner refuses to recommend a wave that cannot finish. Agent durations are
measured, not guessed.

Anthropic reports quota as a *percentage*, never a token count. The absolute
figure is recovered as `spent ÷ percent used`, in a self-consistent "proxy
token" unit (output + cache-creation + input). It is a planning unit, **not a
billing figure**.

### `/ctxmon:peers` — who is doing what

```
session       ctx            band       phase        agents  claim / cwd
s-3f9c21ab    262k/1.00M     NORMAL     busy 24s          0  «refactor auth middleware»
s-7d40e155    424k/1.00M     CONSERVE   busy 28m          2  «migrate payment adapters»
s-b1e88024    980k/1.00M     HANDOFF    idle 9h           0  api-server
```

`phase` is the part nothing else records. A session at 60% that went idle two
minutes ago is available; a session at 60% that has been busy for eight minutes
with four agents in flight is not. Those are otherwise indistinguishable.

### `/ctxmon:harvest` — provenance, not a summary

Runs automatically at `PreCompact`, or on demand. Writes what was asked, every
file touched with read/write counts, every agent dispatched, and every command
verbatim.

A compaction summary keeps conclusions and drops the evidence behind them. This
keeps the evidence, so a measurement can be **re-run** rather than re-derived.
Truncation, when it happens, is stated in the file rather than hidden.

## Privacy — read this before enabling trails

Trails are plaintext files on disk containing **your prompts and every command
verbatim**. Commands are scanned for credential-shaped strings (bearer tokens,
`ghp_`/`sk-`/`AKIA` prefixes, `--password`, `*_TOKEN=`, JWTs) and redacted
before writing, but **redaction is pattern matching, not proof**. Treat a trail
as you would your shell history.

`/ctxmon:plan` reads transcript metadata across **every project on the
machine**, not just the current one, because quota is machine-wide. It reads
token counts and timestamps, never message content.

Nothing is ever transmitted anywhere. There is no network code in `ctxmon` at
all. `ipc` speaks only to `127.0.0.1`.

To disable trail writing entirely, set `CTXMON_DISABLE=1` (which disables all
hooks) or simply do not install `ctxmon`.

## How it works

```
 Claude Code ──stdin──> statusline.sh ──> your existing statusline
                             │
                             └──> state/sl/<sid>.json     (quota + window size)
                                        │
 transcript .jsonl ────────────────────>├──> ctxmon.py ──> hook stdout
 claude-hud context-cache ─────────────>│                  (into Claude's context)
 ipc relay-state.json ─────────────────>┘
```

Sources are tried in order and each falls back to the next: claude-hud's cache
if that plugin happens to be installed (it carries the true window size), then
the session transcript in Claude Code's own format, then nothing — it prints
nothing rather than guessing. claude-hud is **optional**; it is not a dependency.

State lives in `~/.claude/flightdeck/`, deliberately not in the plugin
directory: `${CLAUDE_PLUGIN_ROOT}` changes on every plugin update and the old
tree is cleaned up within weeks.

## Things measured while building this

Facts that were surprising enough to be worth recording, all from real data:

- **Auto-compact was never observed to fire.** A session ran from 85k to
  **985,111 tokens — 98.5% of a 1M window — across 504 usage readings with zero
  drops.** So "safe headroom" is a reserve you keep in hand, not a cliff the
  harness enforces.
- **A subagent's `tool_result` is not its completion.** Agents run in the
  background by default, so `tool_result` returns in ~2s carrying only the
  agent's id. Real completion is a separate `queue-operation` record holding a
  `<task-notification>`. Measuring the wrong pair reported every agent as a
  2-second job; measured correctly the same runs were **median 46m, p90 1h**.
- **Most captured statusline payloads are stale.** Claude Code renders a
  statusline for every session it *lists*, not only live ones — 26 of 28
  captures were stale, several by 200+ hours, still carrying a closed window's
  `rate_limits`. Only `resets_at` being in the past reveals it.
- **A dip in quota is not necessarily a window reset.** Two live sessions tick
  at different moments, so a fresh 10% reading can be followed 11 seconds later
  by an older session's 3%. Read as a reset, that made burn rate permanently
  unmeasurable.
- **A Python interpreter spawn costs ~91ms**, which is why the statusline
  wrapper is pure bash builtins — at a ~300ms statusline cadence a Python tee
  was unaffordable.

## Configuration

| Variable | Effect |
|---|---|
| `CTXMON_DISABLE=1` | every ctxmon hook becomes a no-op |
| `CTXMON_QUIET=1` | drop the per-turn line, keep alarms and cross-session broadcast |
| `CTXMON_PYTHON` | interpreter to use, if auto-detection picks wrong |
| `FLIGHTDECK_DIR` | relocate all state |
| `RG_IPC_DISABLE=1` | every IPC hook becomes a no-op |
| `RG_IPC_RELAY_URL` | point at a relay other than `127.0.0.1:4180` |

## Requirements

Python 3.9+ and a POSIX shell. On Windows that means Git Bash, which Claude
Code already uses. No third-party packages: both components are standard
library only.

## Development

```bash
python3 plugins/ctxmon/tests/test_ctxmon.py   # 58 tests
python3 tools/build_bundle.py                 # regenerate the bundle plugin
python3 tools/build_bundle.py --check         # fails if stale
python3 tools/check_manifests.py
```

`plugins/flightdeck/` is **generated** from the two components — edit the
component, then regenerate. CI enforces freshness.

## License

MIT. See [LICENSE](LICENSE).
