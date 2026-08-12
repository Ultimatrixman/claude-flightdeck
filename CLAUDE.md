# CLAUDE.md — claude-flightdeck developer guide

Two Claude Code plugins, published at
https://github.com/Ultimatrixman/claude-flightdeck (MIT, public).

- **`ctxmon`** puts context and quota telemetry into *Claude's* context, not the
  user's screen: a per-turn budget line, a silent threshold alarm, a scheduling
  planner, a cross-session view, and a provenance trail written at compaction.
- **`ipc`** is a message channel between concurrent sessions: peer discovery,
  advisory work claims with a `PreToolUse` edit guard, and a `/ping` `/pong`
  rally.

This repo is a **workspace of its own**, unrelated to any other project checked
out on the same machine. Do not carry another repo's conventions in here beyond
what is restated below.

## Commands

Everything is standard library only. Any Python 3.9+ works; there is no venv.

```bash
python3 plugins/ctxmon/tests/test_ctxmon.py   # the ctxmon unit suite
python3 plugins/ipc/tests/test_ipc.py         # the ipc claim matcher
bash tests/integration.sh                     # the shell layer
bash tests/integration_setup.sh               # settings.json round trip
python3 tools/build_bundle.py                 # regenerate plugins/flightdeck
python3 tools/build_bundle.py --check         # freshness gate, exit 1 if stale
python3 tools/check_manifests.py              # manifests agree with each other
python3 install.py --dry-run                  # standalone installer, no writes
```

CI runs all of these on windows/macos/ubuntu against Python 3.9 and 3.13, plus
shellcheck on the two bin wrappers and statusline.sh. Confirm a gate by its
summary line, never by exit code alone.

## Layout

```
.claude-plugin/marketplace.json   three entries: ctxmon, ipc, flightdeck
plugins/ctxmon/                   the telemetry plugin (source of truth)
plugins/ipc/                      the IPC plugin (source of truth)
plugins/flightdeck/               GENERATED bundle — never hand-edit
tools/build_bundle.py             generates the bundle; --check gates freshness
tools/check_manifests.py          validates every manifest against the tree
install.py                        standalone settings.json installer
```

`plugins/flightdeck/` is generated. Edit a component, then regenerate. A hand
edit there is silently overwritten and CI will fail on the next push.

## Hard-won invariants

Each of these cost a real bug. Re-read the matching one before touching that
area.

**Hooks must never fail loudly.** Every hook subcommand exits 0 on any error.
A non-zero hook can interrupt a session, and no telemetry is worth that. The
shell wrappers exit 0 even when no interpreter is found.

**Exiting 0 is not enough; the payload is validated too.** The harness checks
hook stdout against a per-event schema and prints the whole rejected object
plus the expected schema into the transcript, which is a loud failure by any
other name. Only some events accept
`hookSpecificOutput.additionalContext`; `PreCompact` does not, and shipped
through 0.1.7 emitting it, so every single compaction ended with a validation
error under it. `_CONTEXT_EVENTS` is the whitelist and `_emit` degrades
anything outside it to a top-level `systemMessage`, which is valid everywhere.
There was never anything to inject at `PreCompact` anyway: the context that
would carry it is the context being replaced. `cmd_sessionstart` hands the
trail path back on the far side, where it survives.

**Telemetry with no handling rule gets narrated.** The per-turn line was bare
numbers, sent every turn, at every band. A model given a status readout that
often treats it as salient and reports it: quoting its own context percentage
back at the user, apologising for a budget it has not spent, trimming work
nobody asked it to trim. Two things fix it and both are load-bearing. The line
carries only quota, agents out, and a bare percentage until a band actually
asks for something, because the figures a model has no use for are exactly the
ones it fills space with. And `CTX_PROTOCOL` states the contract for the
channel once per session: instrumentation, not content for the reply, and only
`HARVEST` and `HANDOFF` ask for action. Once per session, not per turn, is the
point; a suffix repeated every turn is the same mistake again.

**`NORMAL` is not an event.** It is the band with no advice, so announcing a
crossing into it is an alarm whose content is "nothing is wrong". It reached
users at 4% context because `cmd_prompt` returned early before `_band_reset`
whenever the source was not yet readable, which is the ordinary state of turn
one, leaving no band on disk for `cmd_tick` to compare against. Seed the band
before any early return, and gate the announcement on `advice` being non-empty
rather than on a band index.

**A subagent's `tool_result` is not its completion.** Agents run in the
background, so `tool_result` returns in ~2s carrying only the agent id. Real
completion is a `queue-operation` record holding a `<task-notification>` naming
the originating `tool-use-id`. Pairing against `tool_result` reports every agent
as a 2-second job and makes the planner answer "safe to fan out" always.

**Streamed records are duplicated; dedupe before summing.** One assistant
message is rewritten many times (measured 449 usage records across 152 message
ids, one repeated 10 times). `scan_window` dedupes on `message.id`;
`_scan_transcript` dedupes by value change. Summing blindly inflated spend
3.38x.

**Quota readings go stale silently.** Claude Code writes a statusline payload
for every session it *lists*, not only live ones: 26 of 28 captures were stale,
several by 200+ hours. Only `resets_at` already being in the past reveals it.
And a *dip* is not necessarily a reset — two sessions ticking at different
moments interleave; a real reset moves `resets_at` forward by hours.

**Auto-compact was never observed to fire.** A session ran to 985,111 tokens,
98.5% of a 1M window, across 504 readings with zero drops. `AUTOCOMPACT_BUFFER`
is a reserve *we* choose. Never describe it in user-facing text as the point
auto-compact triggers; the wording is "safe headroom".

**Our own wrapper must never be recorded as the previous statusline.** Setup
saves whatever statusLine it found so the wrapper can hand the payload on, so
"is this already ours" has to recognise our wrapper *wherever it sits*, not
just the one path the current version installs. Through 0.1.6 the test was the
single substring `flightdeck/statusline.sh`, while 0.1.1 had shipped setup as
prose that pointed statusLine at the plugin's own versioned copy and install.py
wires `plugins/ctxmon/statusline.sh`. Upgrading from 0.1.1 therefore recorded a
wrapper as the inner command, which makes the wrapper hand the payload to a
wrapper reading the same record: unbounded recursion, respawned every tick, and
the user's real statusline gone. `_is_ours` and `is_ours_statusline` now match
`statusline.sh` under any `ctxmon` or `flightdeck` directory and must stay in
step; `_recorded_inner` treats a record holding one of ours as no record, which
is what also repairs the machines 0.1.6 already poisoned. Repairing
settings.json alone would have left that file on disk.

**A claim pattern is relative to the cwd of the session that made it.** The
edit guard used to take the pattern's literal prefix and ask whether that
string appeared anywhere in the target path, with no cwd on either side. A
session claiming `tests/**` in one repo therefore warned on every other repo's
`tests/` on the machine, and `src` matched `mysrcfile.py` because a substring
test knows nothing about path boundaries. The cwd is now snapshotted into the
claim at claim time, a relative pattern that cannot be placed matches nothing
at all, globs are matched with `fnmatch` and bare directories on path
boundaries. A guard that cries wolf across repositories is one people learn to
ignore, which is the same reasoning that already skips stale claims.

`ipc.py` cannot import `relay.py`, because the relay runs as its own detached
process and a hook must not pay for importing it, so the matcher is duplicated
between the two behind `# --- claim matcher (keep identical) ---` markers.
`test_claim_matcher_is_identical_in_both_modules` compares the two blocks
textually. Drift means the relay reports an overlap the guard stays silent
about, or the reverse.

**One release, one version number.** It is written in four places: the
marketplace metadata, both component manifests, and the literal in
build_bundle.py that regenerates the bundle's. `check_manifests.py` asserts
they agree, because a bundle left on the old number installs as an older
plugin than the marketplace advertises.

**State never lives in the plugin directory.** `${CLAUDE_PLUGIN_ROOT}` changes
on every update and the old tree is cleaned up within weeks. State goes to
`~/.claude/flightdeck/`, shared between both plugins so ctxmon can join the IPC
relay registry on `session_id`.

**Write generated files with explicit LF.** `Path.write_text` emits CRLF on
Windows, so a bundle generated there never matches the LF-normalised copy in
git and `--check` reports it stale forever. Use
`open(..., newline="\n")`; `write_text(newline=)` is 3.10+ and this supports
3.9. `.gitattributes` pins `eol=lf` because a CRLF shell script fails outright
on Linux and macOS.

**No process spawns on the statusline path.** It runs at roughly 300ms; a
Python spawn measured 91ms. The wrapper is bash builtins only. `read -d '' -t 2`
returns non-zero at EOF, which is normal and must not be treated as failure,
and the payload keeps a trailing newline that must be trimmed before testing
for a closing brace.

**`exec` on the right of a pipe replaces only the subshell.** The wrapper must
`exit 0` explicitly or it falls through to its fallback message.

## Truthfulness rules

**No silent caps.** Anything that truncates says so in its own output. The
trail's first version dropped the 7 earliest prompts of 47 and said nothing,
and the earliest prompts are where intent lives.

**Never render a stale number as current.** If quota cannot be verified fresh,
omit it rather than print it.

**Redaction is not optional.** Trails hold commands verbatim. `redact()` runs on
every command and prompt before writing, and the docs say plainly that pattern
matching is not proof.

**Estimates are labelled as estimates.** The uncounted-growth figure prints as
`≤N`, and agent-duration verdicts carry `LOW CONFIDENCE (n<3)`.

## Writing style for user-facing text

README, plugin descriptions, command markdown and release notes are
outward-facing: **no em-dash connectors**, and nothing that reads as
AI-generated. Concede what already exists before claiming what is new; the
comparison table earns the rest of the page.

Comments state constraints the code cannot show. Do not narrate the next line.
