#!/usr/bin/env python3
"""Context and usage telemetry for Claude Code sessions.

Claude Code hands the rich payload (context_window, rate_limits, model) only to
the *statusline* command. Hooks receive session_id / transcript_path / cwd and
nothing else. This bridges the gap: cheap producers write session snapshots to
disk, and hook subcommands read them back into the model's context.

Used two ways:
  * As hook commands wired in ~/.claude/settings.json:
      prompt (UserPromptSubmit), tick (PostToolUse), precompact (PreCompact),
      sessionstart (SessionStart), stop (Stop), sessionend (SessionEnd)
  * By Claude itself via Bash:
      status / plan / peers / harvest / doctor

Sources, in preference order:
  1. claude-hud's context-cache  — authoritative; carries the true window size
     and Claude Code's own used_percentage. Refreshed every ~3s by the
     statusline. Located by matching the plaintext transcriptPath in the
     sibling transcript-cache, never by recomputing its sha256 filename.
  2. the session transcript      — Claude Code's own format; message.usage on
     each assistant record. Independent of any plugin.
  3. nothing                     — print nothing rather than guess.

Env knobs: CTXMON_DISABLE=1 (hook subcommands become no-ops), CTXMON_QUIET=1
(suppress the per-turn line but keep threshold alarms and cross-session
broadcast), CTXMON_STATE_DIR / FLIGHTDECK_DIR (relocate state). The RG_CTXMON_*
spellings are the pre-release names and are still honoured.

Hook subcommands NEVER fail loudly: any error exits 0, so a bug here can never
wedge a Claude Code session.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

SELF = Path(__file__).resolve()
CTXMON_DIR = SELF.parent
HOME = Path(os.path.expanduser("~"))
CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (HOME / ".claude"))

# State lives in a shared flightdeck home, NOT in the plugin directory and NOT
# in CLAUDE_PLUGIN_DATA. Two reasons: ${CLAUDE_PLUGIN_ROOT} changes on every
# plugin update and its old tree is cleaned up within weeks, and
# CLAUDE_PLUGIN_DATA is per-plugin, which would hide the IPC relay's registry
# from ctxmon -- the join that makes the cross-session view work.
FLIGHTDECK_DIR = Path(os.environ.get("FLIGHTDECK_DIR") or (CLAUDE_DIR / "flightdeck"))
STATE_DIR = Path(os.environ.get("CTXMON_STATE_DIR")
                 or os.environ.get("RG_CTXMON_STATE_DIR")
                 or (FLIGHTDECK_DIR / "ctxmon"))
SESSIONS_DIR = STATE_DIR / "sessions"
SL_DIR = STATE_DIR / "sl"            # statusline payloads, written by the tee
HUD_DIR = CLAUDE_DIR / "plugins" / "claude-hud"

# The IPC relay's registry, if that plugin is installed. The legacy path is the
# pre-plugin layout, kept so an existing install keeps working after upgrade.
IPC_STATE_CANDIDATES = (
    FLIGHTDECK_DIR / "ipc" / "relay-state.json",
    CLAUDE_DIR / "ipc" / "state" / "relay-state.json",
)


def _ipc_state() -> Path:
    for p in IPC_STATE_CANDIDATES:
        if p.exists():
            return p
    return IPC_STATE_CANDIDATES[0]

HOOK_CMDS = {"prompt", "tick", "precompact", "sessionstart", "stop", "sessionend"}

SCHEMA = 1

# Measured across 1,664 consecutive-usage-record pairs from 6 real transcripts:
# median 7.47 bytes/token, p10 3.09, p90 21.49. The p25 value is used so the
# estimate OVER-states tokens in ~75% of cases — this figure only ever triggers
# an early warning, and warning late is the expensive failure.
BYTES_PER_TOKEN = 4.7

# Working reserve, borrowed from claude-hud's AUTOCOMPACT_BUFFER_PERCENT.
#
# It is NOT an observed auto-compact threshold. Measured 2026-08-11: a session
# on this machine ran from 85k to 985,111 tokens — 98.5% of a 1M window — across
# 504 distinct usage readings with ZERO drops, so auto-compact never fired at
# any point below that. Treat this as the margin we choose to keep in hand (room
# to finish a turn and write the harvest), and never describe it to the user as
# the point where auto-compact will trigger.
AUTOCOMPACT_BUFFER = 0.165

DEFAULT_WINDOW = 200_000

# Bands are fractions of the USABLE budget (to auto-compact), so they mean the
# same thing on a 200k window as on a 1M one.
BANDS = (
    (0.50, "NORMAL", ""),
    (0.70, "CONSERVE",
     "delegate file-heavy search to subagents; background long runs"),
    (0.85, "HARVEST",
     "write down what this session LEARNED, now, while the detail is still in "
     "context: memory files for durable facts, tasks for open work, docs for "
     "decisions. Compaction keeps conclusions and drops provenance"),
    (99.0, "HANDOFF",
     "stop starting new work; finish the harvest and write the handoff"),
)

# Tool results at or above this size are called out individually — one such
# result can cost more than a whole turn of ordinary work.
BIG_RESULT_TOKENS = 8_000


# ---------------------------------------------------------------- plumbing

def _log(msg: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        log = STATE_DIR / "ctxmon.log"
        if log.exists() and log.stat().st_size > 262144:
            log.unlink()
        with log.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def _hook_input() -> dict:
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except (OSError, ValueError):
        return {}


def _read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _atomic_write(p: Path, data) -> None:
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, p)
    except OSError as e:
        _log(f"write {p.name} failed: {e!r}")


def _norm_path(p: str) -> str:
    """Compare paths across the separator and case differences that appear
    between what Claude Code reports, what Node's path.resolve wrote, and what
    os.getcwd() returns. Case folding is correct on Windows and harmless on
    POSIX for the identity comparisons this is used for."""
    return (p or "").replace("\\", "/").rstrip("/").lower()


def _sid8(session_id: str) -> str:
    return (session_id or "anon")[:8]


def _emit(event: str, text: str) -> None:
    """Inject text into the model's context via hook stdout."""
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": event, "additionalContext": text}}))


def _ago(sec) -> str:
    try:
        s = max(0.0, float(sec))
    except (TypeError, ValueError):
        return "?"
    if s < 60:
        return f"{int(s)}s"
    if s < 3600:
        return f"{int(s // 60)}m"
    if s < 86400:
        # Minutes matter here. Flooring to whole hours made the planner read as
        # self-contradictory: "1h of runway against a p90 agent of 36m" next to
        # a verdict of "safe to fan out" is correct when the runway is really
        # 1h54m, and looks like a bug when it says 1h.
        h, m = int(s // 3600), int((s % 3600) // 60)
        return f"{h}h{m:02d}m" if m else f"{h}h"
    d, h = int(s // 86400), int((s % 86400) // 3600)
    return f"{d}d{h}h" if h else f"{d}d"


def _k(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "?"
    if abs(n) < 1000:
        return str(n)
    if abs(n) < 1_000_000:
        return f"{n / 1000:.0f}k" if abs(n) >= 10_000 else f"{n / 1000:.1f}k"
    return f"{n / 1_000_000:.2f}M"


# ---------------------------------------------------------------- hud source

def _hud_cache_for(transcript_path: str) -> dict | None:
    """claude-hud persists a context snapshot every ~3s, keyed by
    sha256(path.resolve(transcript_path)). Recomputing that hash from a hook is
    fragile (separator and case must match Node's resolve exactly), so prefer
    the sibling transcript-cache, which stores transcriptPath in plaintext."""
    if not transcript_path:
        return None
    want = _norm_path(transcript_path)
    tdir, cdir = HUD_DIR / "transcript-cache", HUD_DIR / "context-cache"
    try:
        entries = sorted(tdir.glob("*.json"), key=lambda p: p.stat().st_mtime,
                         reverse=True)
    except OSError:
        entries = []
    for entry in entries[:60]:
        d = _read_json(entry)
        if not isinstance(d, dict):
            continue
        got = _norm_path(str(d.get("transcriptPath") or ""))
        if got and got == want:
            return _read_json(cdir / entry.name)
    # Fallback: recompute the hash for both separator spellings.
    for cand in (transcript_path.replace("/", "\\"), transcript_path):
        h = hashlib.sha256(cand.encode("utf-8")).hexdigest()
        d = _read_json(cdir / f"{h}.json")
        if d:
            return d
    return None


# ------------------------------------------------------- transcript source

AGENT_LOG_MAX_BYTES = 262144


def agent_log_path() -> Path:
    return STATE_DIR / "agent-durations.jsonl"


def record_agent_duration(bid: str, end_t: float, seconds: float,
                          label: str = "") -> None:
    """Append one completed agent run.

    Durations were previously derived only from the current 5-hour window, so
    n was routinely 1 or 2 and every verdict carried LOW CONFIDENCE. A run that
    completed 20 minutes before the window opened is still the best evidence
    available for how long the next one will take.

    Append-only and deduped on read rather than on write: a background agent
    produces a provisional end (its tool_result, which is really dispatch) and
    later a true end (its task-notification), and taking the max per id
    resolves that without needing to rewrite rows.
    """
    if not bid or seconds <= 0:
        return
    try:
        p = agent_log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists() and p.stat().st_size > AGENT_LOG_MAX_BYTES:
            keep = p.read_text(encoding="utf-8").splitlines()[-600:]
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write("\n".join(keep) + "\n")
        with open(p, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"id": bid, "t": round(end_t, 1),
                                "s": round(seconds, 1), "l": label[:40]}) + "\n")
    except OSError:
        pass


def agent_history(days: float = 30.0) -> list:
    """Durations of completed agent runs, most recent first.

    Keeps the LONGEST duration recorded per id: for a background agent the
    task-notification supersedes the tool_result, which only measured dispatch.
    """
    cutoff = time.time() - days * 86400
    best: dict[str, dict] = {}
    try:
        for line in agent_log_path().read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if (r.get("t") or 0) < cutoff or not r.get("id"):
                continue
            prev = best.get(r["id"])
            if prev is None or (r.get("s") or 0) > (prev.get("s") or 0):
                best[r["id"]] = r
    except OSError:
        return []
    return sorted(best.values(), key=lambda r: -(r.get("t") or 0))


def _scan_state_path(sid8: str) -> Path:
    return STATE_DIR / f"scan-{sid8}.json"


def _scan_transcript(path: str, sid8: str) -> dict:
    """Walk the transcript for usage totals and in-flight agent count.

    Incremental: a transcript reaches multiple megabytes, and this runs on
    every tool call. State from the previous scan is resumed whenever the file
    has only grown; a shrink (rewrite or compaction) forces a full rescan.

    In-flight agents are derived by matching tool_use ids against tool_result
    ids rather than by incrementing a counter, so a missed hook or a killed
    subagent corrects itself on the next read instead of drifting forever.
    """
    empty = {"size": 0, "ctx": 0, "out": 0, "last_usage_off": 0, "turns": 0,
             "open_agents": [], "agents_total": 0, "seen_agents": [],
             "agent_starts": {}}
    if not path:
        return empty
    p = Path(path)
    try:
        size = p.stat().st_size
    except OSError:
        return empty

    prev = _read_json(_scan_state_path(sid8)) or {}
    if (isinstance(prev, dict) and prev.get("path") == path
            and isinstance(prev.get("size"), int) and 0 < prev["size"] <= size):
        st = {k: prev.get(k, empty[k]) for k in empty}
        start = prev["size"]
    else:
        st = dict(empty)
        start = 0

    open_agents = set(st.get("open_agents") or [])
    seen_agents = set(st.get("seen_agents") or [])
    # id -> first-seen timestamp, carried across incremental scans so a run
    # that started before this scan resumed can still be measured.
    agent_starts = dict(st.get("agent_starts") or {})
    try:
        with p.open("rb") as f:
            f.seek(start)
            off = start
            for raw in f:
                off += len(raw)
                if not raw.strip():
                    continue
                try:
                    r = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                rec_t = _iso_epoch(r.get("timestamp") or "")
                if r.get("type") == "queue-operation":
                    c = r.get("content") or ""
                    if "<task-notification>" in c and rec_t:
                        m = re.search(r"<tool-use-id>([^<]+)</tool-use-id>", c)
                        if m:
                            bid = m.group(1).strip()
                            start = agent_starts.get(bid)
                            if start:
                                record_agent_duration(bid, rec_t, rec_t - start)
                    continue
                if r.get("type") == "user" and not r.get("isSidechain"):
                    msg = r.get("message") or {}
                    content = msg.get("content")
                    if isinstance(content, str):
                        st["turns"] += 1
                msg = r.get("message") or {}
                u = msg.get("usage")
                if u and not r.get("isSidechain"):
                    ctx = (u.get("input_tokens", 0)
                           + u.get("cache_creation_input_tokens", 0)
                           + u.get("cache_read_input_tokens", 0))
                    if ctx:
                        if ctx != st["ctx"]:
                            st["out"] += u.get("output_tokens", 0)
                        st["ctx"] = ctx
                        st["last_usage_off"] = off
                content = msg.get("content")
                if isinstance(content, list):
                    for blk in content:
                        if not isinstance(blk, dict):
                            continue
                        if (blk.get("type") == "tool_use"
                                and blk.get("name") in ("Agent", "Task")):
                            bid = blk.get("id")
                            # A streaming response rewrites the same record
                            # repeatedly; without this a duplicate arriving
                            # after the result would reopen a closed agent.
                            if bid and bid not in seen_agents:
                                seen_agents.add(bid)
                                open_agents.add(bid)
                                if rec_t:
                                    agent_starts[bid] = rec_t
                        elif blk.get("type") == "tool_result":
                            rid = blk.get("tool_use_id")
                            open_agents.discard(rid)
                            # Provisional: for a background agent this is
                            # dispatch, not completion. The later notification
                            # supersedes it because reads take the max per id.
                            start = agent_starts.get(rid)
                            if start and rec_t and rec_t > start:
                                record_agent_duration(rid, rec_t, rec_t - start)
    except OSError as e:
        _log(f"scan failed: {e!r}")
        return st

    st["size"] = size
    st["open_agents"] = [a for a in open_agents if a]
    st["seen_agents"] = sorted(a for a in seen_agents if a)
    st["agents_total"] = len(st["seen_agents"])
    # Bounded: only ids still plausibly in flight need their start time kept.
    cutoff = time.time() - 86400
    st["agent_starts"] = {k: v for k, v in agent_starts.items() if v >= cutoff}
    out = dict(st)
    out["path"] = path
    _atomic_write(_scan_state_path(sid8), out)
    return st


def _uncounted(path: str, last_usage_off: int) -> int:
    """Upper-bound estimate of tokens added since the last API round trip.

    The transcript's usage record reflects the request that produced the last
    assistant message, so anything written since (tool results, this turn's
    prompt) is invisible to it. Bytes appended after that record, divided by
    the calibrated constant, bound how much is missing."""
    if not path or not last_usage_off:
        return 0
    try:
        size = Path(path).stat().st_size
    except OSError:
        return 0
    return max(0, int((size - last_usage_off) / BYTES_PER_TOKEN))


# ---------------------------------------------------------------- snapshot

def _band(frac: float) -> tuple[str, str]:
    for limit, name, advice in BANDS:
        if frac < limit:
            return name, advice
    return BANDS[-1][1], BANDS[-1][2]


SL_MAX_AGE_S = 1800

# How recently the statusline must have written for its TOKEN COUNTS to be
# treated as current. The statusline ticks several times a second in a live
# session, so anything older than this is a session that has stopped rendering.
SL_LIVE_S = 120


def _statusline_payload(sid8: str) -> dict:
    """Read the statusline capture, DROPPING quota that has gone stale.

    Claude Code renders a statusline for every session it lists, not only live
    ones, so a payload gets written for sessions that are merely enumerated:
    26 of 28 captures on this machine were stale, several by 200+ hours, each
    still carrying the rate_limits of a window that closed days ago.

    A stale reading is indistinguishable from a current one by value alone --
    only `resets_at` already being in the past gives it away. Sampling them
    injected phantom window resets into the burn-rate series (three
    contradictory readings inside 51 seconds), and rendering one put a dead
    window's percentage into a live prompt as though it were current.
    """
    p = SL_DIR / f"{sid8}.json"
    d = _read_json(p) or {}
    if not d:
        return {}
    d = dict(d)
    try:
        d["_age_s"] = time.time() - p.stat().st_mtime
    except OSError:
        d["_age_s"] = 1e9
    resets = ((d.get("rate_limits") or {}).get("five_hour") or {}).get("resets_at")
    fresh = isinstance(resets, (int, float)) and resets > time.time()
    if fresh:
        try:
            fresh = (time.time() - p.stat().st_mtime) < SL_MAX_AGE_S
        except OSError:
            fresh = False
    if not fresh:
        d.pop("rate_limits", None)
        d["_quota_stale"] = True
    return d


def _ipc_name_for(session_id: str) -> str:
    d = _read_json(_ipc_state())
    if not isinstance(d, dict):
        return ""
    for name, rec in d.items():
        if isinstance(rec, dict) and rec.get("session_id") == session_id:
            return name
    return ""


def build_snapshot(hook: dict, phase: str | None = None) -> dict:
    """Assemble one session's state from every available source."""
    sid = hook.get("session_id") or ""
    sid8 = _sid8(sid)
    tpath = hook.get("transcript_path") or ""
    now = time.time()

    prev = _read_json(SESSIONS_DIR / f"{sid8}.json") or {}
    scan = _scan_transcript(tpath, sid8)

    sl = _statusline_payload(sid8)
    quota_sample(sl)  # burn rate needs a series; nothing else keeps one

    hud = _hud_cache_for(tpath) or {}
    usage = hud.get("current_usage") or {}
    hud_ctx = ((usage.get("input_tokens") or 0)
               + (usage.get("cache_creation_input_tokens") or 0)
               + (usage.get("cache_read_input_tokens") or 0))

    # The statusline payload carries the authoritative window size and usage.
    # Reading it here is what makes claude-hud genuinely optional: without this
    # a 1M-window user with no hud fell back to DEFAULT_WINDOW and was pushed
    # into HANDOFF at what was really 26% used.
    slcw = sl.get("context_window") or {}
    slu = slcw.get("current_usage") or {}
    sl_ctx = ((slu.get("input_tokens") or 0)
              + (slu.get("cache_creation_input_tokens") or 0)
              + (slu.get("cache_read_input_tokens") or 0))
    sl_window = slcw.get("context_window_size") or 0

    # Window SIZE does not expire the way usage does: it is a property of the
    # account and model, so a stale payload still knows it. Token counts from
    # the same payload are only trusted while it is being refreshed.
    window = (hud.get("context_window_size") or sl_window
              or prev.get("ctx_window") or DEFAULT_WINDOW)
    # Explicit None check, not `or`: an age of exactly 0.0 is falsy, so `or`
    # turned the freshest possible payload into the oldest. It reproduced only
    # where the write and the stat landed in the same clock tick.
    _age = sl.get("_age_s")
    sl_live = isinstance(_age, (int, float)) and _age < SL_LIVE_S

    if hud_ctx:
        ctx, source = hud_ctx, "hud"
    elif sl_ctx and sl_live:
        ctx, source = sl_ctx, "statusline"
    elif scan.get("ctx"):
        ctx, source = scan["ctx"], "transcript"
    else:
        ctx, source = 0, "none"

    usable = max(1, int(window * (1 - AUTOCOMPACT_BUFFER)))
    uncounted = _uncounted(tpath, scan.get("last_usage_off") or 0)
    frac = (ctx + uncounted) / usable
    band, advice = _band(frac)

    rl = sl.get("rate_limits") or {}
    model = ((sl.get("model") or {}).get("display_name")
             or prev.get("model") or "")

    if phase is None:
        phase = prev.get("phase") or "idle"
    phase_since = (prev.get("phase_since") or now
                   if phase == prev.get("phase") else now)

    snap = {
        "schema": SCHEMA,
        "session_id": sid,
        "sid8": sid8,
        "ipc_name": _ipc_name_for(sid) or prev.get("ipc_name") or "",
        "cwd": hook.get("cwd") or prev.get("cwd") or "",
        "model": model,
        "updated": now,
        "phase": phase,
        "phase_since": phase_since,
        "last_response_at": prev.get("last_response_at") or 0,
        "turns": scan.get("turns") or 0,
        "ctx_tokens": ctx,
        "ctx_window": window,
        "ctx_pct": round(ctx / window * 100, 1) if window else 0,
        "usable_tokens": usable,
        "headroom_tokens": max(0, usable - ctx - uncounted),
        "uncounted_est": uncounted,
        "out_total": scan.get("out") or 0,
        "agents_running": len(scan.get("open_agents") or []),
        "agents_total": scan.get("agents_total") or 0,
        "band": band,
        "advice": advice,
        "source": source,
        "quota_stale": bool(sl.get("_quota_stale")),
        "rate_5h_pct": (rl.get("five_hour") or {}).get("used_percentage"),
        "rate_5h_resets_at": (rl.get("five_hour") or {}).get("resets_at"),
        "rate_7d_pct": (rl.get("seven_day") or {}).get("used_percentage"),
        "rate_7d_resets_at": (rl.get("seven_day") or {}).get("resets_at"),
    }
    return snap


def save_snapshot(snap: dict) -> None:
    _atomic_write(SESSIONS_DIR / f"{snap['sid8']}.json", snap)


def _quota_str(snap: dict) -> str:
    bits = []
    for label, key in (("5h", "rate_5h_pct"), ("7d", "rate_7d_pct")):
        v = snap.get(key)
        if isinstance(v, (int, float)):
            bits.append(f"{label} {int(v)}%")
    if not bits:
        return ""
    s = " · quota " + "/".join(bits)
    reset = snap.get("rate_5h_resets_at")
    if isinstance(reset, (int, float)):
        left = reset - time.time()
        if left > 0:
            s += f" (resets {_ago(left)})"
    return s


def status_line(snap: dict) -> str:
    """The one-line form injected each turn. Kept deliberately short."""
    ctx, win = snap["ctx_tokens"], snap["ctx_window"]
    unc = snap.get("uncounted_est") or 0
    unc_s = f" (+≤{_k(unc)} uncounted)" if unc >= 1500 else ""
    agents = snap.get("agents_running") or 0
    agent_s = f" · {agents} agent{'s' if agents != 1 else ''} running" if agents else ""
    advice = f": {snap['advice']}" if snap.get("advice") else ""
    return (f"[ctx] {_k(ctx)}/{_k(win)} ({snap['ctx_pct']}%){unc_s} · "
            f"{_k(snap['headroom_tokens'])} safe headroom · "
            f"out {_k(snap['out_total'])}{agent_s}{_quota_str(snap)} · "
            f"{snap['band']}{advice}")


# ------------------------------------------------------------- quota & plan

PROJECTS_DIR = CLAUDE_DIR / "projects"
QUOTA_HIST = STATE_DIR / "quota-history.jsonl"
QUOTA_HIST_MAX_BYTES = 131072
WINDOW_H = 5.0


def _iso_epoch(ts: str) -> float:
    """Transcript timestamps are ISO-8601 with a trailing Z."""
    if not ts:
        return 0.0
    try:
        from datetime import datetime
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, ImportError):
        return 0.0


def quota_sample(sl: dict) -> None:
    """Append a rate-limit reading, but only when a percentage actually moved.

    Burn rate is meaningless without a time series, and nothing on this machine
    keeps one: the statusline renders the current value and discards it."""
    rl = (sl or {}).get("rate_limits") or {}
    five, seven = rl.get("five_hour") or {}, rl.get("seven_day") or {}
    f_pct, s_pct = five.get("used_percentage"), seven.get("used_percentage")
    if not isinstance(f_pct, (int, float)):
        return
    row = {"t": round(time.time(), 1), "f": f_pct, "s": s_pct,
           "fr": five.get("resets_at"), "sr": seven.get("resets_at")}
    try:
        if QUOTA_HIST.exists():
            if QUOTA_HIST.stat().st_size > QUOTA_HIST_MAX_BYTES:
                keep = QUOTA_HIST.read_text(encoding="utf-8").splitlines()[-400:]
                QUOTA_HIST.write_text("\n".join(keep) + "\n", encoding="utf-8")
            last = None
            with QUOTA_HIST.open("rb") as fh:
                try:
                    fh.seek(-4096, os.SEEK_END)
                except OSError:
                    fh.seek(0)
                tail = fh.read().decode("utf-8", "replace").splitlines()
                if tail:
                    try:
                        last = json.loads(tail[-1])
                    except ValueError:
                        last = None
            if last and last.get("f") == f_pct and last.get("s") == s_pct:
                return
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with QUOTA_HIST.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        pass


def quota_history(hours: float = 6.0) -> list:
    cutoff = time.time() - hours * 3600
    rows = []
    try:
        for line in QUOTA_HIST.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            # Defensive: rows written before the staleness filter existed can
            # carry a resets_at that was ALREADY past when sampled. Those are
            # the phantom resets that broke burn-rate measurement.
            fr = r.get("fr")
            if (r.get("t") or 0) >= cutoff and (not fr or fr > r["t"]):
                rows.append(r)
    except OSError:
        pass
    return rows


def dedupe_quota(rows: list) -> list:
    """Drop stale readings that a second session wrote after a fresher one.

    Quota is machine-wide and rises monotonically inside a window, so a DROP is
    either a real window reset or a stale sample. The discriminator is
    `resets_at`: a real reset moves it forward by hours, while a stale payload
    repeats the SAME resets_at with a lower percentage.

    Two live sessions tick their statuslines at different moments, so the
    shared series interleaved a fresh 10% reading with an older 3% one eleven
    seconds apart. Read as a reset, that truncated the burn window to a few
    seconds and made the rate permanently unmeasurable.
    """
    out: list = []
    for r in sorted(rows, key=lambda x: x.get("t") or 0):
        f, fr = r.get("f"), r.get("fr")
        if not isinstance(f, (int, float)):
            continue
        if out:
            pf, pfr = out[-1].get("f"), out[-1].get("fr")
            if isinstance(fr, (int, float)) and isinstance(pfr, (int, float)):
                is_reset = fr > pfr + 60
            else:
                # No resets_at to compare (legacy or malformed row): the only
                # signal left is the size of the drop. A window reset collapses
                # to near zero; a stale sample dips modestly. Erring toward
                # "stale" here would silently discard a real reset and report
                # the previous window's burn rate as current.
                is_reset = f <= max(5.0, pf * 0.5)
            if f < pf - 0.5 and not is_reset:
                continue  # stale sample from a session that ticked earlier
        out.append(r)
    return out


def burn_per_hour(hist: list, key: str = "f") -> float | None:
    """Percentage points consumed per hour, measured over the samples held.

    Returns None rather than a number when the series is too short or spans too
    little time — a burn rate from two samples 40 seconds apart is noise, and
    acting on it is worse than admitting ignorance."""
    pts = [(r["t"], r[key]) for r in dedupe_quota(hist)
           if isinstance(r.get(key), (int, float)) and r.get("t")]
    if len(pts) < 2:
        return None
    pts.sort()
    # A window reset drops the percentage; only measure the current window.
    for i in range(len(pts) - 1, 0, -1):
        if pts[i][1] < pts[i - 1][1] - 1:
            pts = pts[i:]
            break
    if len(pts) < 2:
        return None
    span_h = (pts[-1][0] - pts[0][0]) / 3600
    if span_h < 0.08:  # under ~5 minutes
        return None
    delta = pts[-1][1] - pts[0][1]
    return delta / span_h if span_h > 0 else None


def scan_window(window_start: float) -> dict:
    """Sum work done across EVERY session since the window opened.

    Quota is machine-wide, so a per-session number cannot answer 'how much is
    left'. Only transcripts modified since the window opened are opened at all,
    which prunes the several hundred historical ones.

    The token figure is a proxy: Anthropic bills input and output at different
    weights and does not publish them, so this sums output plus newly-written
    cache. It is self-consistent, which is what planning needs — the implied
    allowance is expressed in the same unit it is measured in.
    """
    out = {"proxy_tokens": 0, "sessions": 0, "agents": 0,
           "agent_durations": [], "open_agents": 0}
    try:
        files = [p for p in PROJECTS_DIR.glob("*/*.jsonl")
                 if p.stat().st_mtime >= window_start]
    except OSError:
        return out
    for p in files:
        touched = False
        # First-seen start time per tool_use id. A streaming response rewrites
        # the same assistant record several times, so the same tool_use block
        # appears repeatedly; keeping the LAST occurrence collapses every
        # measured duration to a couple of seconds, because the final duplicate
        # is written just before the result arrives.
        starts: dict[str, float] = {}
        # Subagents run in the BACKGROUND by default, so their tool_result
        # returns in ~2s carrying only the agent's id -- that gap is dispatch
        # latency, not runtime. Real completion arrives later as a
        # queue-operation record holding a <task-notification> that names the
        # originating tool-use-id. Pairing against tool_result alone reported
        # every agent as a 2-second job and made the verdict meaningless.
        notified: dict[str, float] = {}
        results: dict[str, float] = {}
        # One assistant message is written to the transcript many times as it
        # streams: measured 449 usage records across 152 distinct message ids,
        # one of them repeated 10 times. Summing every copy inflated spend by
        # 3.38x, and with it the implied budget and the tokens/hour burn rate.
        seen_msgs: set[str] = set()
        try:
            with p.open("rb") as f:
                for raw in f:
                    if not raw.strip():
                        continue
                    try:
                        r = json.loads(raw.decode("utf-8", "replace"))
                    except ValueError:
                        continue
                    t = _iso_epoch(r.get("timestamp") or "")
                    if t and t < window_start:
                        continue
                    if r.get("type") == "queue-operation":
                        c = r.get("content") or ""
                        if "<task-notification>" in c:
                            m = re.search(r"<tool-use-id>([^<]+)</tool-use-id>", c)
                            if m and t:
                                notified.setdefault(m.group(1).strip(), t)
                        continue
                    msg = r.get("message") or {}
                    u = msg.get("usage")
                    if u:
                        mid = msg.get("id") or r.get("uuid")
                        if mid is None or mid not in seen_msgs:
                            if mid is not None:
                                seen_msgs.add(mid)
                            out["proxy_tokens"] += (
                                (u.get("output_tokens") or 0)
                                + (u.get("cache_creation_input_tokens") or 0)
                                + (u.get("input_tokens") or 0))
                            touched = True
                    content = msg.get("content")
                    if isinstance(content, list):
                        for blk in content:
                            if not isinstance(blk, dict):
                                continue
                            if (blk.get("type") == "tool_use"
                                    and blk.get("name") in ("Agent", "Task")):
                                bid = blk.get("id")
                                if bid and bid not in starts:
                                    starts[bid] = t
                                    out["agents"] += 1
                            elif blk.get("type") == "tool_result":
                                bid = blk.get("tool_use_id")
                                if bid:
                                    results.setdefault(bid, t)
        except OSError:
            continue
        for bid, st in starts.items():
            # Prefer the notification: for a background agent it is the only
            # record of real runtime. Fall back to tool_result, which IS the
            # true end for a synchronous (run_in_background: false) agent.
            end = notified.get(bid) or results.get(bid)
            if end and st and end > st:
                out["agent_durations"].append(end - st)
                # Backfill the rolling log. The per-session incremental scan
                # only sees bytes written after it last ran, so without this a
                # freshly installed ctxmon would have no history at all until
                # new agents happened to run.
                record_agent_duration(bid, end, end - st)
            else:
                out["open_agents"] += 1
        if touched:
            out["sessions"] += 1
    return out


def _pctile(xs: list, q: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, int(len(s) * q))]


def cmd_plan(args) -> int:
    """Answer the scheduling question: is there room to start this work, and
    will it finish before the quota window closes?"""
    hist = dedupe_quota(quota_history(hours=WINDOW_H + 1))
    latest = hist[-1] if hist else {}
    snaps = _all_snapshots(max_age_s=WINDOW_H * 3600)
    now = time.time()

    f_pct = latest.get("f")
    resets_at = latest.get("fr")
    if not isinstance(f_pct, (int, float)):
        for s in snaps:
            if isinstance(s.get("rate_5h_pct"), (int, float)):
                f_pct, resets_at = s["rate_5h_pct"], s.get("rate_5h_resets_at")
                break

    if not isinstance(f_pct, (int, float)):
        print("quota unknown: rate_limits reach only the statusline, so the tee "
              "must be installed and the statusline must have ticked.\n"
              "Run `doctor` to check. Context planning still works via `status`.")
        return 1

    reset_in = (resets_at - now) if isinstance(resets_at, (int, float)) else None
    window_start = (resets_at - WINDOW_H * 3600
                    if isinstance(resets_at, (int, float)) else now - WINDOW_H * 3600)
    burn = burn_per_hour(hist)

    print(f"5-hour window   {f_pct:.0f}% used"
          + (f", resets in {_ago(reset_in)}" if reset_in and reset_in > 0 else ""))
    s_pct = latest.get("s")
    if isinstance(s_pct, (int, float)):
        print(f"7-day window    {s_pct:.0f}% used")

    w = scan_window(window_start)
    spent = w["proxy_tokens"]
    print(f"spent so far    {_k(spent)} proxy tokens across {w['sessions']} "
          f"session(s), {w['agents']} agent run(s)")

    # The divisor is the percentage used, so a near-empty window explodes the
    # estimate: at 3% this printed "~77.4M proxy tokens", against ~3.3M derived
    # from the same machine at 56%. Below 15% the figure is not worth showing.
    if f_pct >= 15 and spent > 0:
        allowance = spent / (f_pct / 100.0)
        print(f"implied budget  ~{_k(allowance)} proxy tokens per 5h window "
              f"(derived: spent ÷ percent used; sharpens as the window fills)")
        print(f"remaining       ~{_k(max(0, allowance - spent))} proxy tokens")
    else:
        print(f"implied budget  not derivable yet (window only {f_pct:.0f}% used; "
              f"needs ≥15% before the division is stable)")

    if burn is None:
        print("burn rate       not enough history yet (needs samples ≥5min apart)")
        exhaust_in = None
    else:
        print(f"burn rate       {burn:.1f}%/h"
              + (f" · {_k(spent / max(0.05, (now - window_start) / 3600))}/h"
                 if spent else ""))
        exhaust_in = ((100 - f_pct) / burn * 3600) if burn > 0.05 else None
        if exhaust_in:
            print(f"projected       quota exhausted in {_ago(exhaust_in)}"
                  + (f" · window resets in {_ago(reset_in)}" if reset_in and reset_in > 0 else ""))

    # The rolling log spans windows; the window scan sees only this one. Prefer
    # whichever has more evidence, and say which was used.
    hist = [r["s"] for r in agent_history() if isinstance(r.get("s"), (int, float))]
    if len(hist) >= len(w["agent_durations"]):
        durs, scope = hist, "rolling"
    else:
        durs, scope = w["agent_durations"], "this window"
    p50, p90 = _pctile(durs, 0.5), _pctile(durs, 0.9)
    if durs:
        print(f"agent duration  n={len(durs)} ({scope})  median {_ago(p50)}  "
              f"p90 {_ago(p90)}"
              + (f"  ({w['open_agents']} still running)" if w["open_agents"] else "")
              + ("  LOW CONFIDENCE (n<3)" if len(durs) < 3 else ""))
    else:
        print("agent duration  no completed agent runs recorded yet")

    print()
    limit_s = min([x for x in (exhaust_in, reset_in) if x and x > 0], default=None)
    if limit_s is None:
        print("VERDICT  no binding limit measurable yet. Proceed, and re-run "
              "once the quota history has a few samples.")
    elif p90 is None:
        print(f"VERDICT  {_ago(limit_s)} of runway. No agent-duration history "
              f"to compare against, so size work conservatively.")
    elif p90 < limit_s * 0.5:
        print(f"VERDICT  safe to fan out. {_ago(limit_s)} of runway against a "
              f"p90 agent of {_ago(p90)}, so a full wave finishes with margin.")
    elif p90 < limit_s:
        print(f"VERDICT  one wave only. {_ago(limit_s)} of runway against a p90 "
              f"agent of {_ago(p90)}: start the wave now, do not queue a second.")
    else:
        print(f"VERDICT  do NOT start agent work. {_ago(limit_s)} of runway is "
              f"shorter than a p90 agent run ({_ago(p90)}). An agent killed "
              f"mid-flight returns nothing and its quota is spent anyway. "
              f"Do short inline work, or wait for the reset.")
    if exhaust_in and reset_in and exhaust_in < reset_in:
        print(f"         NOTE quota runs out {_ago(reset_in - exhaust_in)} BEFORE "
              f"the window resets at this burn rate. Slow down or idle.")
    return 0


# ---------------------------------------------------------------- hook cmds

def _env_flag(*names: str) -> bool:
    """RG_-prefixed names are the pre-release spelling, still honoured."""
    return any(os.environ.get(x) == "1" for x in names)


def _disabled() -> bool:
    return _env_flag("CTXMON_DISABLE", "RG_CTXMON_DISABLE")


def cmd_prompt(_args) -> int:
    """UserPromptSubmit: mark the session busy and inject the status line."""
    hook = _hook_input()
    snap = build_snapshot(hook, phase="busy")
    save_snapshot(snap)
    if _env_flag("CTXMON_QUIET", "RG_CTXMON_QUIET") or snap["source"] == "none":
        return 0
    _band_reset(snap)
    _emit("UserPromptSubmit", status_line(snap))
    return 0


def _band_path(sid8: str) -> Path:
    return STATE_DIR / f"band-{sid8}.txt"


def _band_reset(snap: dict) -> None:
    """Record the band announced by the per-turn line so the mid-turn alarm
    only ever speaks on a NEW band."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        _band_path(snap["sid8"]).write_text(snap["band"], encoding="utf-8")
    except OSError:
        pass


def cmd_tick(_args) -> int:
    """PostToolUse: silent unless a threshold band is newly crossed, or one
    tool result was unusually large. Silence is the whole point — this fires
    on every expensive tool call and must cost nothing at steady state."""
    hook = _hook_input()
    if hook.get("agent_id"):
        return 0  # a subagent's own context is not the main session's budget
    snap = build_snapshot(hook)
    save_snapshot(snap)
    if snap["source"] == "none":
        return 0

    sid8 = snap["sid8"]
    try:
        seen = _band_path(sid8).read_text(encoding="utf-8").strip()
    except OSError:
        seen = ""
    order = [b[1] for b in BANDS]
    now_i = order.index(snap["band"]) if snap["band"] in order else 0
    seen_i = order.index(seen) if seen in order else -1

    msgs = []
    if now_i > seen_i:
        try:
            _band_path(sid8).write_text(snap["band"], encoding="utf-8")
        except OSError:
            pass
        msgs.append(f"[ctx] crossed into {snap['band']}: {status_line(snap)}")

    prev_unc = snap.get("uncounted_est") or 0
    if prev_unc >= BIG_RESULT_TOKENS and now_i <= seen_i:
        last = STATE_DIR / f"big-{sid8}.txt"
        try:
            marked = int(last.read_text(encoding="utf-8") or 0)
        except (OSError, ValueError):
            marked = 0
        step = prev_unc // BIG_RESULT_TOKENS
        if step > marked:
            try:
                last.write_text(str(step), encoding="utf-8")
            except OSError:
                pass
            msgs.append(
                f"[ctx] ≤{_k(prev_unc)} tokens of tool output have accumulated "
                f"since the last round trip (not yet in the counter above). "
                f"{_k(snap['headroom_tokens'])} safe headroom left.")
    if not msgs:
        return 0
    _emit("PostToolUse", "\n".join(msgs))
    return 0


def cmd_stop(_args) -> int:
    """Stop: the response is final. Record the idle transition so OTHER
    sessions can tell 'finished 90s ago' from 'busy for 8 minutes'. Nothing is
    printed — a Stop hook that emits a block decision would trap the session."""
    hook = _hook_input()
    if hook.get("stop_hook_active"):
        return 0
    snap = build_snapshot(hook, phase="idle")
    snap["last_response_at"] = time.time()
    save_snapshot(snap)
    try:
        (STATE_DIR / f"big-{snap['sid8']}.txt").unlink(missing_ok=True)
    except OSError:
        pass
    return 0


# Redaction runs over every command before it is written to a trail. A trail is
# a plaintext file on disk containing commands VERBATIM, so one `curl -H
# "Authorization: Bearer sk-..."` would persist a live credential. Patterns are
# deliberately broad: a false positive costs a few unreadable characters, a miss
# costs a leaked key.
_SECRET_PATTERNS = (
    (re.compile(r"\b(sk-|ghp_|gho_|ghs_|ghu_|github_pat_|xox[abprs]-|AKIA|ASIA)"
                r"[A-Za-z0-9_\-]{8,}"), "<redacted:token>"),
    (re.compile(r"(?i)\b(authorization|api[-_]?key|secret|password|passwd|token)"
                r"\s*[:=]\s*[\"']?([^\s\"']{6,})"), r"\1=<redacted>"),
    (re.compile(r"(?i)--(password|token|api[-_]?key|secret)(\s+|=)\S+"),
     r"--\1=<redacted>"),
    (re.compile(r"(?i)\b([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD))=\S+"),
     r"\1=<redacted>"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"
                r"\.[A-Za-z0-9_\-]{10,}"), "<redacted:jwt>"),
)


def redact(text: str) -> str:
    """Strip credential-shaped substrings. Never disabled by config: a tool
    that writes your shell history to disk has no business making this
    optional."""
    if not text:
        return text
    for pat, repl in _SECRET_PATTERNS:
        text = pat.sub(repl, text)
    return text


_NOT_A_PROMPT = (
    "<task-notification>", "<local-command-", "<command-name>",
    "<command-message>", "<command-args>", "<system-reminder>",
    "<user-prompt-submit-hook>", "Caveat:", "[Request interrupted",
)


def _prompt_text(content: str) -> str | None:
    """Return what the user actually asked, or None for harness traffic.

    Task notifications, hook output and command echoes all arrive as user-role
    string content; 20 of 47 "prompts" in the first real harvest were those.
    But a SLASH COMMAND is intent, not noise -- filtering the whole
    `<command-name>` family reduced a real 219-command session to 2 recorded
    prompts. Slash commands are folded back to `/name args` instead.
    """
    c = (content or "").strip()
    if not c:
        return None
    if c.startswith("<command-name>"):
        m = re.search(r"<command-name>([^<]*)</command-name>", c)
        a = re.search(r"<command-args>([^<]*)</command-args>", c)
        name = (m.group(1).strip() if m else "").lstrip("/")
        if not name:
            return None
        args = (a.group(1).strip() if a else "")
        return redact(f"/{name} {args}".strip())[:220]
    if c.startswith(_NOT_A_PROMPT):
        return None
    return redact(c)[:220]


def build_trail(path: str, limit: int = 400) -> str:
    """Reconstruct the session's provenance trail from its transcript.

    A compaction summary preserves conclusions and drops the evidence: which
    files were actually read, what each command returned, what each agent was
    asked. All of it is still on disk at PreCompact time, and none of it
    survives otherwise. This is a mechanical extraction — no summarizing, no
    judgment, just the trail — so it stays true when the summary is thin."""
    files: dict[str, dict] = {}
    cmds: list[tuple[float, str]] = []
    agents: list[tuple[float, str]] = []
    prompts: list[tuple[float, str]] = []
    if not path:
        return ""
    try:
        with Path(path).open("rb") as f:
            for raw in f:
                if not raw.strip():
                    continue
                try:
                    r = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                t = _iso_epoch(r.get("timestamp") or "")
                msg = r.get("message") or {}
                content = msg.get("content")
                if r.get("type") == "user" and isinstance(content, str):
                    c = _prompt_text(content)
                    if (c and not r.get("isSidechain")
                            and (not prompts or prompts[-1][1] != c)):
                        prompts.append((t, c))
                if not isinstance(content, list):
                    continue
                for blk in content:
                    if not isinstance(blk, dict) or blk.get("type") != "tool_use":
                        continue
                    name = blk.get("name") or ""
                    inp = blk.get("input") or {}
                    fp = (inp.get("file_path") or inp.get("notebook_path") or "")
                    if name in ("Read", "Edit", "Write", "NotebookEdit") and fp:
                        rec = files.setdefault(fp, {"read": 0, "write": 0})
                        rec["read" if name == "Read" else "write"] += 1
                    elif name in ("Bash", "PowerShell"):
                        c = redact(
                            (inp.get("command") or "").replace("\n", " "))[:150]
                        d = inp.get("description") or ""
                        if c:
                            cmds.append((t, f"{c}" + (f"   # {d}" if d else "")))
                    elif name in ("Agent", "Task"):
                        d = (inp.get("description")
                             or (inp.get("prompt") or "")[:100])
                        agents.append((t, f"{inp.get('subagent_type') or 'agent'}: {d}"))
    except OSError as e:
        _log(f"trail scan failed: {e!r}")
        return ""

    def stamp(t):
        return time.strftime("%H:%M", time.localtime(t)) if t else "  -  "

    def note(kept: int, total: int) -> list:
        """A cap that hides its own effect turns a partial record into a
        confident-looking complete one. Say what was dropped, always."""
        if kept >= total:
            return []
        return [f"- _… {total - kept} of {total} entries omitted by the "
                f"{limit}-entry cap; the full record is the transcript itself._"]

    out = ["# Session trail (mechanical extract)", "",
           f"Session {Path(path).stem}",
           f"Written {time.strftime('%Y-%m-%d %H:%M:%S')}",
           f"Totals: {len(prompts)} prompts · {len(files)} files · "
           f"{len(agents)} agents · {len(cmds)} commands",
           "", "This is provenance, not a summary: what was read, run and asked.",
           "Fine detail the compaction summary drops is recoverable from here.", ""]

    if prompts:
        out += ["## What was asked", ""]
        # Intent lives at the START of a session and status at the END, so when
        # this has to truncate it keeps both ends, never just the tail. The
        # first pass at this dropped the 7 earliest prompts of 47 -- exactly
        # the ones stating what the session set out to do.
        if len(prompts) <= limit:
            shown = prompts
            gap = 0
        else:
            head = limit // 3
            shown = prompts[:head] + prompts[-(limit - head):]
            gap = len(prompts) - len(shown)
        for i, (t, p) in enumerate(shown):
            if gap and i == limit // 3:
                out.append(f"- _… {gap} prompts omitted from the middle …_")
            out.append(f"- `{stamp(t)}` {p}")
        out.append("")
    if files:
        out += ["## Files touched", ""]
        ranked = sorted(files.items(),
                        key=lambda kv: -(kv[1]["read"] + kv[1]["write"] * 3))
        for fp, rec in ranked[:limit]:
            marks = []
            if rec["write"]:
                marks.append(f"written x{rec['write']}")
            if rec["read"]:
                marks.append(f"read x{rec['read']}")
            out.append(f"- `{fp}`: {', '.join(marks)}")
        out += note(min(limit, len(ranked)), len(ranked)) + [""]
    if agents:
        out += ["## Agents dispatched", ""]
        for t, a in agents[:limit]:
            out.append(f"- `{stamp(t)}` {a}")
        out += note(min(limit, len(agents)), len(agents)) + [""]
    if cmds:
        out += ["## Commands run", ""]
        for t, c in cmds[-limit:]:
            out.append(f"- `{stamp(t)}` `{c}`")
        out += note(min(limit, len(cmds)), len(cmds)) + [""]
    return "\n".join(out)


def cmd_precompact(_args) -> int:
    """PreCompact: context is about to be summarized. Persist the provenance
    trail and a checkpoint, so the post-compaction session can recover detail
    the summary drops."""
    hook = _hook_input()
    snap = build_snapshot(hook)
    snap["compacted_at"] = time.time()
    snap["compact_trigger"] = hook.get("trigger") or ""
    save_snapshot(snap)

    trail = build_trail(hook.get("transcript_path") or "")
    trail_path = STATE_DIR / "trails" / f"{snap['sid8']}-{int(time.time())}.md"
    if trail:
        try:
            trail_path.parent.mkdir(parents=True, exist_ok=True)
            trail_path.write_text(trail, encoding="utf-8")
        except OSError as e:
            _log(f"trail write failed: {e!r}")

    ck = {
        "session_id": snap["session_id"],
        "at": time.time(),
        "trigger": hook.get("trigger") or "",
        "ctx_tokens_before": snap["ctx_tokens"],
        "turns_before": snap["turns"],
        "cwd": snap["cwd"],
        "trail": str(trail_path) if trail else "",
    }
    _atomic_write(STATE_DIR / f"compact-{snap['sid8']}.json", ck)
    _emit("PreCompact",
          "[ctx] Compaction starting. The provenance trail (files read, "
          "commands run, agents dispatched) has been written to "
          f"{trail_path if trail else 'nowhere: the scan failed'}.")
    return 0


def cmd_harvest(args) -> int:
    """Write the provenance trail on demand, before a manual /compact or at
    the end of a long session."""
    snap = _pick(args.session)
    if not snap:
        print("no session snapshot yet")
        return 1
    tpath = ""
    for d in PROJECTS_DIR.glob("*/*.jsonl"):
        if d.stem == snap.get("session_id"):
            tpath = str(d)
            break
    trail = build_trail(tpath)
    if not trail:
        print(f"could not build a trail (transcript not found for {snap['sid8']})")
        return 1
    out = STATE_DIR / "trails" / f"{snap['sid8']}-{int(time.time())}.md"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(trail, encoding="utf-8")
    except OSError as e:
        print(f"write failed: {e!r}")
        return 1
    print(f"trail written: {out}")
    print(f"{len(trail.splitlines())} lines, {len(trail)} bytes")
    return 0


def cmd_sessionstart(_args) -> int:
    """SessionStart: after a compact or resume, hand back the pointer to the
    pre-compaction checkpoint. Silent on a genuinely fresh start."""
    hook = _hook_input()
    snap = build_snapshot(hook, phase="busy")
    save_snapshot(snap)
    src = (hook.get("source") or "").lower()
    if src not in ("compact", "resume"):
        return 0
    ckpath = STATE_DIR / f"compact-{snap['sid8']}.json"
    ck = _read_json(ckpath)
    if not ck:
        return 0
    trail = ck.get("trail") or ""
    trail_note = (f" The provenance trail for everything before the summary "
                  f"(files read, commands run, agents dispatched) is at {trail}. "
                  f"Read it before concluding a detail was lost."
                  if trail else "")
    _emit("SessionStart",
          f"[ctx] Session {src}d. Before compaction: "
          f"{_k(ck.get('ctx_tokens_before'))} tokens over "
          f"{ck.get('turns_before')} turns, "
          f"{_ago(time.time() - (ck.get('at') or time.time()))} ago."
          f"{trail_note}")
    return 0


def cmd_sessionend(_args) -> int:
    hook = _hook_input()
    sid8 = _sid8(hook.get("session_id") or "")
    snap = _read_json(SESSIONS_DIR / f"{sid8}.json")
    if snap:
        snap["phase"] = "ended"
        snap["updated"] = time.time()
        save_snapshot(snap)
    for name in (f"scan-{sid8}.json", f"band-{sid8}.txt", f"big-{sid8}.txt"):
        try:
            (STATE_DIR / name).unlink(missing_ok=True)
        except OSError:
            pass
    return 0


# ---------------------------------------------------------------- bash cmds

def _all_snapshots(max_age_s: float = 86400) -> list:
    out = []
    now = time.time()
    try:
        for p in SESSIONS_DIR.glob("*.json"):
            d = _read_json(p)
            if isinstance(d, dict) and now - (d.get("updated") or 0) < max_age_s:
                out.append(d)
    except OSError:
        pass
    return sorted(out, key=lambda d: d.get("updated") or 0, reverse=True)


def _pick(session: str | None) -> dict | None:
    snaps = _all_snapshots()
    if session:
        for s in snaps:
            if s.get("session_id", "").startswith(session) or s.get("sid8") == session[:8]:
                return s
        return None
    cwd = _norm_path(os.getcwd())
    for s in snaps:
        if _norm_path(s.get("cwd") or "") == cwd:
            return s
    return snaps[0] if snaps else None


def cmd_status(args) -> int:
    snap = _pick(args.session)
    if not snap:
        print("no session snapshot yet (hooks write one on the next prompt or tool call)")
        return 1
    if args.json:
        print(json.dumps(snap, indent=2))
        return 0
    age = time.time() - (snap.get("updated") or 0)
    print(status_line(snap))
    print(f"  session {snap['sid8']} ({snap.get('ipc_name') or 'no ipc name'})"
          f"  model {snap.get('model') or '?'}  source {snap['source']}"
          f"  snapshot {_ago(age)} old")
    print(f"  phase {snap['phase']} for {_ago(time.time() - (snap.get('phase_since') or 0))}"
          f"  turns {snap.get('turns')}"
          f"  agents {snap.get('agents_running')} running / {snap.get('agents_total')} total")
    return 0


def cmd_peers(args) -> int:
    """Cross-session view: who is working, who is idle, who has budget.

    Joins ctxmon snapshots with the IPC relay registry on session_id — the
    relay owns liveness and claims, ctxmon owns context and phase."""
    snaps = _all_snapshots(max_age_s=args.max_age)
    relay = _read_json(_ipc_state()) or {}
    by_sid = {r.get("session_id"): (n, r) for n, r in relay.items()
              if isinstance(r, dict)}
    if args.json:
        for s in snaps:
            n_r = by_sid.get(s.get("session_id"))
            s["_relay"] = n_r[1] if n_r else None
        print(json.dumps(snaps, indent=2))
        return 0
    if not snaps:
        print("no sessions seen recently")
        return 0
    now = time.time()
    print(f"{'session':<12} {'ctx':>14} {'band':<11} {'phase':<16} "
          f"{'agents':>6}  claim / cwd")
    for s in snaps:
        n_r = by_sid.get(s.get("session_id"))
        name = (n_r[0] if n_r else s.get("ipc_name")) or s["sid8"]
        claim = ""
        if n_r and isinstance(n_r[1].get("claim"), dict):
            claim = f"«{(n_r[1]['claim'].get('desc') or '')[:38]}»"
        if not claim:
            claim = Path(s.get("cwd") or "").name or "-"
        phase = s.get("phase") or "?"
        since = _ago(now - (s.get("phase_since") or now))
        phase_s = (f"idle {since}" if phase == "idle"
                   else f"busy {since}" if phase == "busy" else phase)
        stale = "" if now - (s.get("updated") or 0) < 300 else " (stale)"
        print(f"{name:<12} {_k(s['ctx_tokens']) + '/' + _k(s['ctx_window']):>14} "
              f"{s.get('band', '?'):<11} {phase_s + stale:<16} "
              f"{s.get('agents_running', 0):>6}  {claim}")
    print("\nphase is the transition marker: 'idle 2m' means that session sent "
          "its final response 2 minutes ago and its numbers are settled; "
          "'busy 8m' means it is mid-turn and its context is still growing.")
    return 0


STATUSLINE_CMD = ('bash "${CLAUDE_CONFIG_DIR:-$HOME/.claude}'
                  '/flightdeck/statusline.sh"')


def _settings_path() -> Path:
    return CLAUDE_DIR / "settings.json"


def _is_ours(cmd: str) -> bool:
    """True if cmd already runs one of our wrappers, wherever it sits.

    Deliberately wider than the version-stable path installed below. 0.1.1
    shipped setup as prose telling Claude to point statusLine at the plugin's
    own copy, so an upgrading user's command is often
    `<cache>/<marketplace>/{ctxmon,flightdeck}/<version>/statusline.sh`.
    Reading one of those as "the statusline you had before" records a wrapper
    as its own inner command, and the wrapper then hands the payload to a
    wrapper that reads the same record: unbounded recursion, respawned on
    every statusline tick, and the real previous command lost.

    Kept in step with `is_ours_statusline` in install.py, which wires a
    different path and so must recognise this one too.
    """
    path = (cmd or "").replace("\\", "/")
    if "statusline.sh" not in path:
        return False
    return any(f"/{d}/" in path for d in ("ctxmon", "flightdeck"))


def _recorded_inner(inner: Path) -> tuple[str, bool]:
    """The statusline we recorded, and whether that record is unusable.

    A record holding one of our own wrappers can only have been written by a
    version that failed to recognise its own command: 0.1.6 and earlier tested
    for one exact path, so a user who had followed 0.1.1's setup prose got the
    wrapper itself recorded as "the statusline you had before". Handing that
    back points the wrapper at a wrapper reading this same file, so it counts
    as no record at all, and what the user really had is not recoverable from
    it. Repairing settings.json alone would leave that file poisoned on disk.
    """
    try:
        prev = inner.read_text(encoding="utf-8").strip()
    except OSError:
        return "", False
    return ("", True) if _is_ours(prev) else (prev, False)


def cmd_setup(args) -> int:
    """Install the statusline wrapper, which is the one thing a plugin cannot
    do for itself: quota reaches only the statusline command, and plugin
    settings.json supports no statusLine key.

    This was prose in a command file until it produced a real bug. The prose
    said to point statusLine at the plugin's own copy of the script, and that
    path carries the plugin version, so the next upgrade would have moved it
    and silently killed both the statusline and quota capture. Code cannot
    drift from itself the way an instruction drifts from what it describes.
    """
    src = CTXMON_DIR / "statusline.sh"
    if not src.is_file():
        print(f"error: {src} not found; is this running from the plugin?")
        return 1

    p = _settings_path()
    try:
        settings = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    except ValueError as e:
        print(f"error: {p} is not valid JSON ({e}).\n"
              f"Fix or move it first; setup will not overwrite a file it "
              f"cannot parse.")
        return 1

    current = (settings.get("statusLine") or {}).get("command", "")
    # The wrapper lives OUTSIDE the plugin directory on purpose:
    # ${CLAUDE_PLUGIN_ROOT} carries the version and moves on every upgrade.
    dst = FLIGHTDECK_DIR / "statusline.sh"
    inner = STATE_DIR / "statusline-inner.sh"

    if args.uninstall:
        if not _is_ours(current):
            print("statusline is not ctxmon's; nothing to undo")
            return 0
        prev, unusable = _recorded_inner(inner)
        if prev:
            settings["statusLine"] = {"type": "command", "command": prev}
            note = "restored your previous statusline"
        else:
            settings.pop("statusLine", None)
            note = ("removed the statusLine entry (the recorded one was ours, "
                    "so restoring it would have looped; set yours again by hand)"
                    if unusable else
                    "removed the statusLine entry (there was none before)")
    else:
        if _is_ours(current):
            # Refresh the copy: an upgraded plugin ships a newer wrapper. The
            # inner record is left alone; it holds the user's real previous
            # statusline, and our own command must never replace it.
            note = ("already installed; refreshed the wrapper from this version"
                    if current.strip() == STATUSLINE_CMD else
                    f"moved your statusLine off the versioned plugin path onto "
                    f"{dst}, which survives upgrades")
            # Repair a record an earlier version poisoned with our own command.
            # Removing it is not the same as emptying it: absent returns the
            # wrapper to detecting claude-hud, which is what such a user most
            # likely had, while empty would pin the built-in minimal line.
            if _recorded_inner(inner)[1]:
                note += ("; discarded the recorded statusline, which was one "
                         "of our own wrappers that an earlier setup wrote "
                         "there and would have looped")
                if not args.dry_run:
                    try:
                        inner.unlink()
                    except OSError:
                        pass
            else:
                note += ("; the statusline you had before this was left "
                         "recorded as it was")
        else:
            # An EMPTY inner file means "there was no statusline", which is not
            # the same as the file being absent (absent means look for
            # claude-hud). Write it either way.
            note = ("wrapped your existing statusline" if current
                    else "installed (you had no statusline)")
            if not args.dry_run:
                inner.parent.mkdir(parents=True, exist_ok=True)
                with open(inner, "w", encoding="utf-8", newline="\n") as f:
                    f.write(current)
        settings["statusLine"] = {"type": "command", "command": STATUSLINE_CMD}

    if args.dry_run:
        print(f"would write {p}")
        print(f"  statusLine -> {(settings.get('statusLine') or {}).get('command', '(removed)')}")
        print(f"  wrapper    -> {dst}")
        print(f"  note       -> {note}")
        return 0

    if p.is_file():
        bak = p.with_suffix(p.suffix + f".bak.flightdeck-"
                                       f"{time.strftime('%Y%m%d-%H%M%S')}")
        try:
            bak.write_bytes(p.read_bytes())
            print(f"backup   {bak}")
        except OSError as e:
            print(f"error: could not back up {p} ({e}); refusing to write")
            return 1

    if not args.uninstall:
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())
            try:
                os.chmod(dst, 0o755)
            except OSError:
                pass
        except OSError as e:
            print(f"error: could not install the wrapper to {dst} ({e})")
            return 1
        print(f"wrapper  {dst}")

    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    except OSError as e:
        print(f"error: could not write {p} ({e})")
        return 1
    print(f"settings {p}")
    print(f"         {note}")
    print("\nRestart Claude Code for the change to take effect. Your existing "
          "statusline is unchanged: the wrapper captures the payload and hands "
          "the identical bytes straight through to it.")
    return 0


def cmd_doctor(_args) -> int:
    """Report which sources are reachable — run this when a number looks wrong."""
    print(f"state dir        {STATE_DIR}  exists={STATE_DIR.exists()}")
    print(f"hud context-cache {HUD_DIR / 'context-cache'}  "
          f"exists={(HUD_DIR / 'context-cache').exists()}")
    ipc = _ipc_state()
    print(f"ipc relay-state   {ipc}  exists={ipc.exists()}")
    print(f"statusline tee    {SL_DIR}  exists={SL_DIR.exists()}  "
          f"payloads={len(list(SL_DIR.glob('*.json'))) if SL_DIR.exists() else 0}")
    snaps = _all_snapshots()
    print(f"snapshots         {len(snaps)}")
    for s in snaps:
        has_q = isinstance(s.get("rate_5h_pct"), (int, float))
        print(f"  {s['sid8']}  source={s['source']}  quota={'yes' if has_q else 'no'}"
              f"  updated {_ago(time.time() - (s.get('updated') or 0))} ago")
    if not any(isinstance(s.get("rate_5h_pct"), (int, float)) for s in snaps):
        print("\nquota is absent: rate_limits reach only the statusline, so the "
              "tee in settings.json statusLine must be installed and the "
              "statusline must have ticked at least once this session.")
    return 0


# ---------------------------------------------------------------- main

def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser(description="Claude Code context/usage telemetry")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in sorted(HOOK_CMDS):
        sub.add_parser(c)
    p = sub.add_parser("status")
    p.add_argument("--session")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("peers")
    p.add_argument("--json", action="store_true")
    p.add_argument("--max-age", type=float, default=86400)
    sub.add_parser("doctor")
    sub.add_parser("plan")
    p = sub.add_parser("setup")
    p.add_argument("--uninstall", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("harvest")
    p.add_argument("--session")
    args = ap.parse_args()

    fn = {"prompt": cmd_prompt, "tick": cmd_tick, "stop": cmd_stop,
          "precompact": cmd_precompact, "sessionstart": cmd_sessionstart,
          "sessionend": cmd_sessionend, "status": cmd_status,
          "peers": cmd_peers, "doctor": cmd_doctor, "plan": cmd_plan,
          "harvest": cmd_harvest, "setup": cmd_setup}[args.cmd]

    if args.cmd in HOOK_CMDS:
        if _disabled():
            return 0
        try:
            return fn(args)
        except Exception as e:  # hooks must never wedge a session
            _log(f"{args.cmd} error: {e!r}")
            return 0
    return fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
