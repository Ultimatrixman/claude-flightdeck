# claude-flightdeck

Claude Code tells **you** how much context and quota a session is using. It
does not tell **Claude**.

The model cannot query its own token count. It learns about compaction only
after crossing it. It has no idea that the session in your other terminal is at
94% and about to stall, or that the four agents you just dispatched cannot
finish before your quota window closes.

This puts that information where decisions actually get made.

```
[ctx] quota 5h 18%/7d 28% (resets 4h) · ctx 32%
```

That line arrives at the start of every turn, costs about 20 tokens, and is
read by Claude, not printed for you. It stays that short while there is nothing
to decide. Once the session crosses into a band that asks for something, it
opens up into the full readout and carries the directive with it:

```
[ctx] 620k/1.00M (62%) · 215k safe headroom · out 157k · quota 5h 41%/7d 28% (resets 2h) · CONSERVE: delegate file-heavy search to subagents; background long runs
```

The split is deliberate. Quota is the figure that decides whether work already
dispatched can finish before the window closes, so it leads. Context is the
figure a model will happily narrate back at you all day if you let it, so below
CONSERVE it is one number and nothing else.

## What already exists

Worth saying plainly, because most of this data is not hard to get:

| You want | Already covered by | What it does |
|---|---|---|
| What did I spend last week | [ccusage](https://ccusage.com) | Reads the same local JSONL, reports daily / session / 5-hour blocks with cost |
| What is my context at right now | [claude-hud](https://github.com/jarrodwatts/claude-hud) and other statuslines | Renders context fill and window state live |
| Quick check | Built-in `/usage`, `/context`, `/cost` | Shows the current window |
| Send a note to my other session | Native [cross-session messaging](https://code.claude.com/docs/en/cross-session-messaging) (v2.1.224+, **macOS and Linux**), or [inter-session](https://github.com/yilunzhang/claude-code-inter-session) | Moves text between terminals |

These are good tools. Use them. flightdeck reads the same files and does not
replace any of them, and claude-hud in particular composes with it rather than
competing.

What none of them do is change what Claude does next. They render to a human,
who then has to notice, decide, and type an instruction. That loop is the thing
this removes.

## What only this does

**Budget that reaches the model.** The status line above is injected through a
hook, so Claude sizes its own work against it. Delegating a wide search to
subagents at 55% instead of reading forty files inline is a decision it can now
make on its own, because it can see the number.

**A silent alarm on the path that actually burns context.** A single turn can
spend six figures of tokens between two of your messages, and a per-turn
readout is blind to exactly that. The `PostToolUse` hook prints nothing at
steady state, so it costs zero tokens, and speaks only when a threshold that
asks for something is newly crossed, or one tool result was unusually large.

**Numbers Claude will not read back to you.** Telemetry sent to a model without
a rule for handling it gets treated as something to report. Every session opens
with the contract for the channel: the figures are for its own planning, not
for your reply, and only `HARVEST` and `HANDOFF` ask it to act. The point is
notes taken before compaction drops them, not a running commentary on token
spend.

**A scheduling verdict, not a dashboard.** This is the part with no equivalent
anywhere:

```
5-hour window   57% used, resets in 2h
burn rate       9.8%/h
projected       quota exhausted in 3h · window resets in 4h
agent duration  n=4  median 46m  p90 1h

VERDICT  one wave only. 2h of runway against a p90 agent of 1h: start the wave
         now, do not queue a second.
         NOTE quota runs out 40m BEFORE the window resets. Slow down or idle.
```

No single input produces that. Quota percentage alone cannot tell you whether a
fan-out fits. Measured agent durations alone cannot either. It takes burn rate,
time to reset, and real wall-clock agent history together, and the reason it
matters is blunt: **an agent killed mid-flight returns nothing and its quota is
spent anyway.**

**Session phase, which is different from session messaging.**

```
session       ctx            band       phase        agents  claim / cwd
s-3f9c21ab    262k/1.00M     NORMAL     busy 24s          0  «refactor auth middleware»
s-7d40e155    424k/1.00M     CONSERVE   busy 28m          2  «migrate payment adapters»
s-b1e88024    980k/1.00M     HANDOFF    idle 9h           0  api-server
```

Native messaging lets sessions talk. Nothing records whether a session is
mid-turn or settled, and for how long. A session at 60% that went idle ninety
seconds ago is available. A session at 60% that has been busy eight minutes with
four agents in flight is not. Those are indistinguishable without this, and the
difference decides where the next piece of work should go.

**Provenance that survives compaction.** A compaction summary keeps conclusions
and drops the evidence behind them. At `PreCompact`, flightdeck writes what was
asked, every file touched with read and write counts, every agent dispatched,
and every command verbatim. It is a mechanical extract rather than a summary, so
it stays true when the summary is thin, and a measurement can be re-run instead
of re-derived. Truncation, when it happens, is stated in the file rather than
hidden.

It also warns early enough to matter. The `HARVEST` band fires with roughly
seven hours of headroom, so there is time to write memories, tasks and docs
while the detail is still in context, rather than at `PreCompact` when it is
already too late to think.

## About the IPC half

Cross-session messaging is now native in Claude Code, **on macOS and Linux
only**. If you are on either, use the built-in feature for messaging.

The `ipc` plugin here is worth installing when you want:

- **Windows**, where the native feature is unavailable.
- **Advisory work claims.** A session declares what it is working on and over
  which paths. When another session is about to edit a file inside that claim, a
  `PreToolUse` guard warns it first. Patterns are anchored to the directory the
  claiming session was working in, so `tests/**` claimed in one repo says
  nothing about another repo's `tests/`. Claims go stale on their own, because a
  claim that warns forever trains everyone to ignore the guard.
- **Phase and claims shown together** in `peers`, so "who is busy" and
  "who owns this file" are one answer instead of two.

It binds `127.0.0.1:4180` and runs a small relay daemon. That is a real decision,
which is why it installs separately from the read-only telemetry.

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

Then run **`/flightdeck:setup`** once. Everything works without it except quota: the
5-hour and 7-day figures are delivered by Claude Code only to the statusline
command, and a plugin cannot ship a `statusLine`. Setup wraps whatever
statusline you already have (including none) and passes the identical bytes
straight through, so nothing about your display changes.

**Upgrading from 0.1.6 or earlier:** update the plugin and run
`/flightdeck:setup` again. Setup used to recognise only one of the paths its own
wrapper can sit at, so on some upgrades it saved that wrapper as the statusline
it was supposed to be wrapping. The wrapper then handed the payload to itself on
every render. 0.1.7 recognises all of them, clears the bad record if one was
already written, and moves `statusLine` onto a path that survives the next
upgrade. If your statusline shows a plain built-in line afterwards, your
original command was overwritten before 0.1.7 could protect it, and setting it
once by hand is the only way back.

<details>
<summary>Standalone install, from a clone</summary>

```bash
git clone https://github.com/Ultimatrixman/claude-flightdeck
cd claude-flightdeck
python3 install.py              # both components plus statusline capture
python3 install.py --dry-run    # print resulting settings, write nothing
python3 install.py --uninstall  # remove everything it added
```

It backs up `settings.json` first, appends to existing hook arrays rather than
replacing them, and will not touch an entry it did not create.
</details>

## Commands

**Commands are namespaced by the plugin you installed**, not by the component
they came from. Install the `flightdeck` bundle and they are `/flightdeck:*`.
Install `ctxmon` and `ipc` separately and they are `/ctxmon:*` and `/ipc:*`.
Type `/` and the plugin name to see them.

| Command (bundle install) | What it answers |
|---|---|
| `/flightdeck:plan` | Can this work finish before the window closes |
| `/flightdeck:status` | Where is this session's budget |
| `/flightdeck:peers` | What is every other session doing |
| `/flightdeck:harvest` | Write the provenance trail now |
| `/flightdeck:setup` | Wire the quota capture into your statusline |
| `/flightdeck:ping`, `/flightdeck:pong` | Open or close a chat rally with another session |

The four bands are fractions of usable budget, so they mean the same thing on a
200k window as on a 1M one:

| Band | Directive given to Claude |
|---|---|
| `NORMAL` | nothing; the line shrinks and the mid-turn alarm stays quiet |
| `CONSERVE` | delegate file-heavy search to subagents, background long runs |
| `HARVEST` | write down what the session learned: memory, tasks, docs |
| `HANDOFF` | stop starting new work, finish the harvest |

`NORMAL` is not a directive, so it is never announced. Only the three bands
that ask for something ever interrupt a turn.

## Privacy, before you enable trails

Trails are plaintext files containing your prompts and **every command
verbatim**. Commands are scanned for credential-shaped strings (bearer tokens,
`ghp_` / `sk-` / `AKIA` prefixes, `--password`, `*_TOKEN=`, JWTs) and redacted
before writing, but **redaction is pattern matching, not proof**. Treat a trail
the way you treat your shell history.

The planner reads transcript metadata across every project on the machine,
not only the current one, because quota is machine-wide. It reads token counts
and timestamps, never message content.

Nothing is transmitted anywhere. There is no network code in `ctxmon` at all.
`ipc` speaks only to `127.0.0.1`.

Set `CTXMON_DISABLE=1` to turn every hook off.

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

Sources are tried in order, each falling back to the next: claude-hud's cache if
that plugin happens to be installed, then the statusline payload this plugin
captures itself, then the session transcript in Claude Code's own format, then
nothing. It prints nothing rather than guessing.

Window size comes from the statusline payload, which is what makes claude-hud
genuinely optional rather than optional in name only. Size and usage age
differently and are treated differently: window size is a property of your
account and model, so even a stale payload still knows it, while token counts
are trusted only while that session's statusline is actively rendering.

State lives in `~/.claude/flightdeck/`, deliberately not in the plugin
directory, because `${CLAUDE_PLUGIN_ROOT}` changes on every plugin update and
the old tree is cleaned up within weeks.

## Things measured while building this

All from real transcripts, and all things a reasonable implementation gets
wrong by default:

- **Auto-compact never fired.** A session ran from 85k to **985,111 tokens,
  98.5% of a 1M window, across 504 usage readings with zero drops.** So "safe
  headroom" is a reserve you keep in hand, not a cliff the harness enforces.
- **A subagent's `tool_result` is not its completion.** Agents run in the
  background by default, so `tool_result` returns in about 2 seconds carrying
  only the agent's id. Real completion is a separate `queue-operation` record
  holding a `<task-notification>`. Pairing against `tool_result` reported every
  agent as a 2-second job, which made the planner answer "safe to fan out" in
  every situation. Measured correctly, the same four runs were median 46m and
  p90 1h, and the verdict became "one wave only".
- **Most captured statusline payloads are stale.** Claude Code renders a
  statusline for every session it lists, not only live ones. 26 of 28 captures
  were stale, several by more than 200 hours, still carrying a closed window's
  `rate_limits`. Only `resets_at` being in the past reveals it.
- **A dip in quota is not necessarily a window reset.** Two live sessions tick
  at different moments, so a fresh 10% reading can be followed eleven seconds
  later by an older session's 3%. Read as a reset, that made burn rate
  permanently unmeasurable. A real reset moves `resets_at` forward by hours.
- **A Python interpreter spawn costs about 91ms**, unaffordable at the roughly
  300ms statusline cadence, which is why the wrapper is pure bash builtins.

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

Python 3.9 or newer and a POSIX shell. On Windows that means Git Bash, which
Claude Code already uses. No third-party packages: both components are standard
library only.

## Development

```bash
python3 plugins/ctxmon/tests/test_ctxmon.py   # unit suite
bash tests/integration.sh                     # the shell layer
bash tests/integration_setup.sh               # settings.json round trip
python3 tools/build_bundle.py                 # regenerate the bundle plugin
python3 tools/build_bundle.py --check         # fails if stale
python3 tools/check_manifests.py
```

`plugins/flightdeck/` is generated from the two components. Edit the component,
then regenerate. CI runs all of the above across Windows, macOS and Linux on
Python 3.9 and 3.13, plus shellcheck on the shipped shell files.

## License

MIT. See [LICENSE](LICENSE).
