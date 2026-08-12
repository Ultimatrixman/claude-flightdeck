#!/usr/bin/env python3
"""Client for Claude Code cross-session IPC (hooks + /pong).

Used two ways:
  * As hook commands wired in ~/.claude/settings.json:
      register (SessionStart), recv (UserPromptSubmit), stop (Stop),
      deregister (SessionEnd)
  * By Claude itself via Bash during a rally:
      send / connect / disconnect / peers / state

Identity: --name, else RG_IPC_NAME, else (hooks only) derived from the
session_id on hook stdin. Relay: RG_IPC_RELAY_URL, default
http://127.0.0.1:4180, auto-started when local and down.

Env knobs: RG_IPC_NAME, RG_IPC_RELAY_URL, RG_IPC_PORT, RG_IPC_TOKEN,
RG_IPC_MAX_HOPS (default 6), RG_IPC_WAIT (long-poll seconds, default 45),
RG_IPC_DISABLE=1 (hook subcommands become no-ops).

Hook subcommands NEVER fail loudly: any error exits 0 so a broken relay can
never wedge a Claude Code session.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode, urlparse

SELF = Path(__file__).resolve()
IPC_DIR = SELF.parent

# State must NOT live under the plugin directory: ${CLAUDE_PLUGIN_ROOT} changes
# on every plugin update and the old tree is cleaned up within weeks, which
# would silently reset the peer registry and every work claim. The shared
# flightdeck home is also where ctxmon looks for relay-state.json to join
# session telemetry against peer identity.
_CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR")
                   or (Path(os.path.expanduser("~")) / ".claude"))
FLIGHTDECK_DIR = Path(os.environ.get("FLIGHTDECK_DIR") or (_CLAUDE_DIR / "flightdeck"))
STATE_DIR = Path(os.environ.get("IPC_STATE_DIR")
                 or os.environ.get("RG_IPC_STATE_DIR")
                 or (FLIGHTDECK_DIR / "ipc"))

# How a human or Claude invokes this client from a shell. Printed into guidance
# text, so it must never be the developer's absolute path.
CLI = f'"{(IPC_DIR / "bin" / "ipc").as_posix()}"' 
HOOK_CMDS = {"register", "recv", "recv-inline", "stop", "deregister", "guard"}


# ---------------------------------------------------------------- plumbing

def relay_url() -> str:
    url = os.environ.get("RG_IPC_RELAY_URL")
    if url:
        return url.rstrip("/")
    return f"http://127.0.0.1:{os.environ.get('RG_IPC_PORT', '4180')}"


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    tok = os.environ.get("RG_IPC_TOKEN")
    if tok:
        h["X-RG-Token"] = tok
    return h


def _req(method: str, path: str, payload=None, params=None, timeout: float = 2.0):
    """Return parsed JSON (including HTTP-error bodies) or None if unreachable."""
    url = relay_url() + path
    if params:
        url += "?" + urlencode({k: v for k, v in params.items() if v is not None})
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read() or b"{}")
        except ValueError:
            return {"ok": False, "error": f"http {e.code}"}
    except (urllib.error.URLError, OSError, ValueError):
        return None


def ensure_relay() -> bool:
    """Health-check the relay; auto-spawn a detached one when local and down."""
    if _req("GET", "/health", timeout=0.8):
        return True
    u = urlparse(relay_url())
    if u.hostname not in ("127.0.0.1", "localhost"):
        return False
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
              "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x08000000  # DETACHED | NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen([sys.executable, str(IPC_DIR / "relay.py"),
                          "--port", str(u.port or 4180)], **kwargs)
    except OSError:
        return False
    deadline = time.time() + 4.0
    while time.time() < deadline:
        if _req("GET", "/health", timeout=0.5):
            return True
        time.sleep(0.15)
    return False


def _hook_input() -> dict:
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except (OSError, ValueError):
        return {}


def _log(msg: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        log = STATE_DIR / "ipc.log"
        if log.exists() and log.stat().st_size > 262144:
            log.unlink()
        with log.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, ""))
    except ValueError:
        return default


# ---------------------------------------------------------------- hop budget

def _hops_path(sid: str) -> Path:
    return STATE_DIR / f"hops-{(sid or 'anon')[:12]}.txt"


def _read_hops(sid: str) -> int:
    try:
        return int(_hops_path(sid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0


def _write_hops(sid: str, n: int) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        _hops_path(sid).write_text(str(n), encoding="utf-8")
    except OSError:
        pass


def _gc_hops() -> None:
    try:
        cutoff = time.time() - 86400
        for f in STATE_DIR.glob("hops-*.txt"):
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------- claims

def _whoami_path(sid: str) -> Path:
    return STATE_DIR / f"whoami-{(sid or 'anon')[:12]}.txt"


# Claim-pattern matching. DUPLICATED VERBATIM from relay.py, which this cannot
# import: the relay runs as its own detached process and a hook must not pay
# for importing it. test_claim_matcher_is_identical_in_both_modules asserts the
# two copies stay in step, the same way _is_ours and is_ours_statusline are
# kept in step in ctxmon.
# --- claim matcher (keep identical) ---
_ABS_PAT = re.compile(r"^(?:/|[a-z]:/)")


def _norm_path(p: str) -> str:
    return (p or "").replace("\\", "/").rstrip("/").lower()


def _pat_prefix(pat: str) -> str:
    """Literal prefix of an fnmatch pattern, normalized for comparison."""
    return pat.replace("\\", "/").lower().split("*")[0].split("?")[0].rstrip("/")


def _under(a: str, b: str) -> bool:
    """Is `a` at or beneath `b`, on path boundaries. A bare startswith made
    'src/tests' a child of 'src/test'."""
    return bool(a) and bool(b) and (a == b or a.startswith(b + "/"))


def _claim_glob(pat: str, base: str) -> str:
    """One claim pattern as an absolute, normalized glob, or '' when it cannot
    be placed on the filesystem at all.

    A relative pattern means nothing without the cwd of the session that made
    it, and matching one anyway is how a claim of 'tests/**' held by a session
    in one repo warned on every OTHER repo's tests/ too. An unplaceable
    pattern matches nothing: a guard that cries wolf across repositories is
    one people learn to ignore, which costs more than the warning is worth."""
    p = _norm_path(pat)
    if not p:
        return ""
    if _ABS_PAT.match(p):
        return p
    while p.startswith("./"):
        p = p[2:]
    base = _norm_path(base)
    return f"{base}/{p}" if base and p else ""


def _claim_covers(fp: str, pat: str, base: str) -> bool:
    """Does one claim pattern, made from `base`, cover one file path?"""
    g = _claim_glob(pat, base)
    if not g:
        return False
    f = _norm_path(fp)
    if any(ch in g for ch in "*?["):
        return fnmatch.fnmatch(f, g)
    # No glob characters: the pattern names a file or a whole directory.
    return _under(f, g)


def _claims_overlap(paths_a, base_a, paths_b, base_b) -> list:
    """Directory overlap between two claims, each pattern first resolved
    against the cwd of the session that made it."""
    hits = []
    for a in paths_a or []:
        pa = _pat_prefix(_claim_glob(a, base_a))
        if not pa:
            continue
        for b in paths_b or []:
            pb = _pat_prefix(_claim_glob(b, base_b))
            if pb and (_under(pa, pb) or _under(pb, pa)):
                hits.append(f"{a} ~ {b}")
    return hits
# --- end claim matcher ---


def _refresh_claims_cache() -> list:
    """Snapshot every peer's work claim to a local file the edit guard can
    read without an HTTP roundtrip. Best-effort."""
    r = _req("GET", "/peers", timeout=2.0)
    if not r:
        return []
    claims = [{"name": p["name"], "desc": p["claim"].get("desc", ""),
               "paths": p["claim"].get("paths", []),
               # Without this the guard has no base to resolve a relative
               # pattern against, and every relative claim goes unmatched.
               "cwd": p["claim"].get("cwd") or p.get("cwd", ""),
               "age_s": p["claim"].get("age_s"),
               "stale": bool(p["claim"].get("stale"))}
              for p in r.get("peers", []) if p.get("claim")]
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        (STATE_DIR / "claims-cache.json").write_text(
            json.dumps({"updated": time.time(), "claims": claims}), encoding="utf-8")
    except OSError:
        pass
    return claims


def _ago(sec) -> str:
    """Compact age label for claim/peer lines: '12m', '3h', '2d'."""
    try:
        s = max(0.0, float(sec))
    except (TypeError, ValueError):
        return "?"
    if s < 3600:
        return f"{int(s // 60)}m"
    if s < 86400:
        return f"{int(s // 3600)}h"
    return f"{int(s // 86400)}d"


def _revoke_hint(peer: str) -> str:
    return f"{CLI} claim --name {peer} --clear"


# ---------------------------------------------------------------- rendering

def _render(msgs: list, me: str) -> str:
    send = f"{CLI} send --name {me or '<your-ipc-name>'}"
    lines = []
    for m in msgs:
        frm = m.get("from", "?")
        kind = m.get("kind", "msg")
        text = m.get("text", "")
        if kind == "connect":
            lines.append(
                f'[connect from {frm}] Rally handshake complete. Reply in chat with '
                f'exactly "ping" and deliver it by running: {send} --to {frm} '
                f'--kind ping --text ping — then end your turn.')
        elif kind == "ping":
            lines.append(
                f'[ping from {frm}] Show the user: "🏓 {frm}: ping" (rally confirmed). '
                f'Do NOT send a reply.')
        elif kind == "disconnect":
            lines.append(
                f'[disconnect from {frm}] The rally is over. Tell the user: '
                f'"ping 🏓 (disconnected from {frm})". Do NOT send anything back.')
        else:
            lines.append(
                f'[message from {frm}] {text}\n'
                f'  If this warrants an answer, run: {send} --to {frm} '
                f'--text "<reply>" — keep it brief, then end your turn.')
    return "\n".join(lines)


# ---------------------------------------------------------------- hook cmds

def cmd_register(_args) -> int:
    d = _hook_input()
    sid = d.get("session_id", "")
    base = os.environ.get("RG_IPC_NAME") or (f"s-{sid[:8]}" if sid else f"s-{os.getpid()}")
    base = re.sub(r"[^A-Za-z0-9_-]", "-", base)[:28] or "peer"
    if not ensure_relay():
        return 0
    _gc_hops()
    name = None
    for cand in [base] + [f"{base}{i}" for i in range(2, 10)]:
        r = _req("POST", "/register", payload={
            "name": cand, "session_id": sid, "host": socket.gethostname(),
            "pid": os.getpid(), "cwd": d.get("cwd", "")}, timeout=2.5)
        if r and r.get("ok"):
            name = cand
            break
        if not (r and r.get("taken")):
            _log(f"register failed for {cand}: {r}")
            return 0
    if not name:
        return 0
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        _whoami_path(sid).write_text(name, encoding="utf-8")
    except OSError:
        pass
    pr = _req("GET", "/peers", timeout=2.0) or {}
    peers = [p["name"] for p in pr.get("peers", []) if p.get("name") != name]
    host = pr.get("host")
    host_note = (f" Host session: '{host}'." if host and host != name else "")
    claims = _refresh_claims_cache()
    mine = next((c for c in claims if c["name"] == name), None)
    theirs = [c for c in claims if c["name"] != name][:4]
    claim_note = ""
    if mine:
        claim_note += (f" Your standing work claim ({_ago(mine.get('age_s'))} old): "
                       f"«{mine['desc']}» ({', '.join(mine['paths'])}). Release it "
                       f"when the work is committed: {_revoke_hint(name)}.")
    if theirs:
        claim_note += " Other sessions' claims: " + "; ".join(
            f"{c['name']} → «{c['desc']}» ({_ago(c.get('age_s'))} old"
            + (", STALE" if c.get("stale") else "") + ")" for c in theirs) + "."
        stale = [c["name"] for c in theirs if c.get("stale")]
        if stale:
            claim_note += (f" A STALE claim's holder has gone silent — the edit "
                           f"guard already ignores it; clear an abandoned one with: "
                           f"{_revoke_hint(stale[0])}.")
    ctx = (f"Cross-session IPC: your IPC name is '{name}'. "
           f"Peers online: {', '.join(peers) if peers else 'none'}.{host_note}"
           f"{claim_note} "
           f"/ping makes this session the host; /pong connects to the host "
           f"(or a named peer) / closes the connection; async message: "
           f"{CLI} send --name {name} "
           f'--to <peer> --text "..."; declare what you are working on '
           f"(before multi-file work) with: {CLI} claim "
           f'--name {name} --desc "<plan>" --paths "<glob1>,<glob2>"')
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "SessionStart", "additionalContext": ctx}}))
    return 0


def cmd_recv(_args) -> int:
    d = _hook_input()
    sid = d.get("session_id", "")
    try:
        _hops_path(sid).unlink(missing_ok=True)  # fresh human turn resets the budget
    except OSError:
        pass
    _refresh_claims_cache()
    r = _req("GET", "/recv", params={"session": sid, "wait": 0}, timeout=2.5)
    msgs = (r or {}).get("messages") or []
    if not msgs:
        return 0
    ctx = ("🏓 Cross-session message(s) arrived while this session was idle:\n"
           + _render(msgs, (r or {}).get("name") or "")
           + "\nAlso address the user's own prompt.")
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit", "additionalContext": ctx}}))
    return 0


def cmd_recv_inline(_args) -> int:
    """PostToolUse: mid-turn delivery, rally-only, never into subagents."""
    d = _hook_input()
    if d.get("agent_id"):
        return 0  # a subagent's tool call — rally traffic stays in the main loop
    sid = d.get("session_id", "")
    r = _req("GET", "/recv", params={"session": sid, "wait": 0, "if_active": 1},
             timeout=2.0)
    msgs = (r or {}).get("messages") or []
    if not msgs:
        return 0
    ctx = ("🏓 Cross-session message(s) delivered mid-turn:\n"
           + _render(msgs, (r or {}).get("name") or "")
           + "\nHandle these per their instructions (a brief reply is fine), "
           "then continue your current work.")
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PostToolUse", "additionalContext": ctx}}))
    return 0


def cmd_guard(_args) -> int:
    """PreToolUse on Edit/Write: warn when a file falls in ANOTHER session's
    claim. Advisory, cache-only (no HTTP), fires in subagents on purpose —
    the implementer holding the file is exactly who needs the warning.

    Claims flagged stale by the relay (holder silent past CLAIM_STALE) are
    skipped: an abandoned claim that warns forever trains everyone to ignore
    the guard, which costs more than the warning ever bought."""
    cache = STATE_DIR / "claims-cache.json"
    if not cache.exists():
        return 0
    d = _hook_input()
    ti = d.get("tool_input") or {}
    fp = ti.get("file_path") or ti.get("path") or ti.get("notebook_path") or ""
    if not fp:
        return 0
    try:
        claims = json.loads(cache.read_text(encoding="utf-8")).get("claims") or []
    except (OSError, ValueError):
        return 0
    if not claims:
        return 0
    me = ""
    try:
        me = _whoami_path(d.get("session_id", "")).read_text(encoding="utf-8").strip()
    except OSError:
        return 0  # can't tell own claim from foreign ones: stay silent
    norm = _norm_path(fp)
    warned = STATE_DIR / f"guard-seen-{(d.get('session_id') or 'anon')[:12]}.txt"
    try:
        seen = set(warned.read_text(encoding="utf-8").splitlines())
    except OSError:
        seen = set()
    hits = []
    for c in claims:
        if c.get("name") == me or c.get("stale"):
            continue
        for pat in c.get("paths") or []:
            if _claim_covers(fp, pat, c.get("cwd", "")):
                key = f"{c['name']}|{norm}"
                if key not in seen:
                    hits.append((c, pat, key))
                break
    if not hits:
        return 0
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with warned.open("a", encoding="utf-8") as f:
            for _c, _p, key in hits:
                f.write(key + "\n")
    except OSError:
        pass
    lines = [f"⚠ Cross-session work claim: {fp} falls inside «{c['desc']}» "
             f"(pattern {pat}{', from ' + c['cwd'] if c.get('cwd') else ''}) "
             f"claimed by the '{c['name']}' session."
             for c, pat, _k in hits]
    ctx = ("\n".join(lines)
           + "\nIf your spec owns this file, proceed deliberately; otherwise "
           "pause this edit and coordinate (or report the conflict) first.")
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "additionalContext": ctx}}))
    return 0


def cmd_stop(_args) -> int:
    d = _hook_input()
    if d.get("stop_hook_active"):
        return 0
    sid = d.get("session_id", "")
    st = _req("GET", "/state", params={"session": sid}, timeout=1.5)
    if not st or not st.get("found") or not st.get("active"):
        return 0  # not in a rally: never block normal sessions
    maxh = max(1, _int_env("RG_IPC_MAX_HOPS", 6))
    hops = _read_hops(sid)
    if hops >= maxh:
        _log(f"hop budget exhausted ({hops}/{maxh}) sid={sid[:8]}")
        return 0
    wait = min(55, max(5, _int_env("RG_IPC_WAIT", 45)))
    r = _req("GET", "/recv", params={"session": sid, "wait": wait}, timeout=wait + 8)
    msgs = (r or {}).get("messages") or []
    if not msgs:
        return 0  # quiet rally: release; balls queue for the next turn
    _write_hops(sid, hops + 1)
    reason = ("🏓 Cross-session rally — incoming:\n"
              + _render(msgs, st.get("name") or "")
              + f"\n(hop {hops + 1}/{maxh}; the listener re-arms when your turn ends; "
              f"/pong ends the rally)")
    print(json.dumps({"decision": "block", "reason": reason}))
    return 0


def cmd_deregister(_args) -> int:
    d = _hook_input()
    sid = d.get("session_id", "")
    if sid:
        _req("POST", "/deregister", payload={"session_id": sid}, timeout=2.0)
    try:
        _hops_path(sid).unlink(missing_ok=True)
    except OSError:
        pass
    return 0


# ---------------------------------------------------------------- bash cmds

def _me(args) -> str:
    return getattr(args, "name", None) or os.environ.get("RG_IPC_NAME") or ""


def _need_me(args):
    me = _me(args)
    if not me:
        print("error: pass --name <your-ipc-name> (announced at session start) "
              "or set RG_IPC_NAME before launching claude")
    return me


def cmd_send(args) -> int:
    me = _need_me(args)
    if not me:
        return 1
    r = _req("POST", "/send", payload={
        "from": me, "to": args.to, "kind": args.kind, "text": args.text}, timeout=3.0)
    if r is None:
        print(f"error: relay unreachable at {relay_url()}")
        return 1
    if not r.get("ok"):
        print(f"error: {r.get('error', 'send failed')}")
        return 1
    print(f"sent to {args.to} ({args.kind})")
    age = r.get("to_age_s")
    if isinstance(age, (int, float)) and age > 90:
        mins = int(age // 60)
        if r.get("to_active"):
            print(f"note: {args.to} last checked in {mins}m ago — likely deep in "
                  f"a turn (e.g. a long tool call); delivery lands at its next "
                  f"tool-call seam or turn end.")
        else:
            print(f"note: {args.to} is not in a rally and last checked in "
                  f"{mins}m ago — it will see this at its next prompt.")
    return 0


def cmd_connect(args) -> int:
    me = _need_me(args)
    if not me:
        return 1
    if not ensure_relay():
        print(f"error: relay unreachable at {relay_url()}")
        return 1
    to = args.to
    if not to:  # no target: search for the declared host session
        st = _req("GET", "/state", params={"name": me}, timeout=2.5) or {}
        to = st.get("host")
        if not to or to == me:
            others = [p["name"] for p in st.get("peers", []) if p.get("name") != me]
            print("error: no host session found (run /ping in the session that "
                  "should host), and no --to given. Peers online: "
                  + (", ".join(others) if others else "none"))
            return 1
    r = _req("POST", "/connect", payload={"from": me, "to": to}, timeout=3.0)
    if not r or not r.get("ok"):
        print(f"error: {(r or {}).get('error', 'relay unreachable')}")
        return 1
    if r.get("host_join"):
        print(f"🏓 Connected to host {to} — they'll serve a ping momentarily. "
              f"End your turn; the listener takes over.")
    elif r.get("mutual"):
        print(f"🏓 Rally OPEN with {to} — they will serve a ping momentarily. "
              f"End your turn; the listener takes over.")
    else:
        print(f"🏓 Armed → invited {to}. Run /pong in their terminal to join. "
              f"This session listens for ~45s after each turn; if the window lapses, "
              f"any ball is delivered on your next prompt.")
    return 0


def cmd_disconnect(args) -> int:
    me = _need_me(args)
    if not me:
        return 1
    r = _req("POST", "/disconnect", payload={"from": me, "to": args.to}, timeout=3.0)
    if not r or not r.get("ok"):
        print(f"error: {(r or {}).get('error', 'relay unreachable')}")
        return 1
    print(f"🏓 Disconnected from {args.to} — they'll confirm with a final ping.")
    return 0


def cmd_host(args) -> int:
    me = _need_me(args)
    if not me:
        return 1
    if not ensure_relay():
        print(f"error: relay unreachable at {relay_url()}")
        return 1
    r = _req("POST", "/host", payload={"name": me, "clear": bool(args.clear)}, timeout=3.0)
    if not r or not r.get("ok"):
        print(f"error: {(r or {}).get('error', 'relay unreachable')}")
        return 1
    if args.clear:
        gone = r.get("disconnected") or []
        print("🏓 Host closed"
              + (f" — disconnected: {', '.join(gone)}" if gone else " — no guests were connected")
              + ". Each guest confirms with a ping on their side.")
    else:
        print(f"🏓 This session ('{me}') is now the HOST. /pong in any other "
              f"session connects here. Listening ~45s after each turn; "
              f"/ping again steps down.")
    return 0


def cmd_claim(args) -> int:
    me = _need_me(args)
    if not me:
        return 1
    if not ensure_relay():
        print(f"error: relay unreachable at {relay_url()}")
        return 1
    if args.clear:
        r = _req("POST", "/claim", payload={"name": me, "clear": True}, timeout=3.0)
        if not r or not r.get("ok"):
            print(f"error: {(r or {}).get('error', 'relay unreachable')}")
            return 1
        _refresh_claims_cache()
        print("claim cleared")
        return 0
    if not args.desc or not args.paths:
        print('error: claim needs --desc "<plan>" and --paths "<glob1>,<glob2>"')
        return 1
    paths = [p.strip() for p in args.paths.split(",") if p.strip()]
    r = _req("POST", "/claim", payload={"name": me, "desc": args.desc,
                                        "paths": paths}, timeout=3.0)
    if not r or not r.get("ok"):
        print(f"error: {(r or {}).get('error', 'relay unreachable')}")
        return 1
    _refresh_claims_cache()
    print(f"claimed: «{args.desc}» over {', '.join(paths)}")
    overlaps = r.get("overlaps") or []
    if overlaps:
        for o in overlaps:
            print(f"⚠ overlaps {o['name']} «{o['desc']}»: {'; '.join(o['hits'])}")
        print("Each overlapped session has been notified through its delivery "
              "seams — coordinate the shared area before editing it.")
    else:
        print("no overlaps with other sessions' claims")
    return 0


def cmd_peers(_args) -> int:
    r = _req("GET", "/peers", timeout=2.5)
    if r is None:
        print(f"error: relay unreachable at {relay_url()}")
        return 1
    peers = r.get("peers", [])
    stale = []
    for p in peers:
        line = f"{p.get('name', '?')}  seen {_ago(p.get('age_s'))} ago"
        c = p.get("claim")
        if c:
            line += f"  claim «{c.get('desc', '')}» ({_ago(c.get('age_s'))} old"
            if c.get("stale"):
                line += ", STALE"
                stale.append(p.get("name", "?"))
            line += ")"
        print(line)
    if stale:
        print(f"\nSTALE = holder silent past the claim timeout; the edit guard "
              f"ignores these. Clear an abandoned one with:\n  "
              f"{_revoke_hint(stale[0])}")
    print()
    print(json.dumps(peers, indent=2))
    return 0


def cmd_state(args) -> int:
    me = _need_me(args)
    if not me:
        return 1
    r = _req("GET", "/state", params={"name": me}, timeout=2.5)
    if r is None:
        print(f"error: relay unreachable at {relay_url()}")
        return 1
    print(json.dumps(r, indent=2))
    return 0


# ---------------------------------------------------------------- main

def main() -> int:
    try:  # emoji in human-readable output must survive any console codepage
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser(description="Claude Code cross-session IPC client")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("register", "recv", "recv-inline", "stop", "deregister", "guard"):
        sub.add_parser(c)
    p = sub.add_parser("send")
    p.add_argument("--name")
    p.add_argument("--to", required=True)
    p.add_argument("--text", required=True)
    p.add_argument("--kind", default="msg", choices=["msg", "ping"])
    p = sub.add_parser("connect")
    p.add_argument("--name")
    p.add_argument("--to")  # optional: defaults to the declared host
    p = sub.add_parser("disconnect")
    p.add_argument("--name")
    p.add_argument("--to", required=True)
    p = sub.add_parser("host")
    p.add_argument("--name")
    p.add_argument("--clear", action="store_true")
    p = sub.add_parser("claim")
    p.add_argument("--name")
    p.add_argument("--desc")
    p.add_argument("--paths")
    p.add_argument("--clear", action="store_true")
    sub.add_parser("peers")
    p = sub.add_parser("state")
    p.add_argument("--name")
    args = ap.parse_args()

    fn = {"register": cmd_register, "recv": cmd_recv, "recv-inline": cmd_recv_inline,
          "stop": cmd_stop, "deregister": cmd_deregister, "send": cmd_send,
          "connect": cmd_connect, "disconnect": cmd_disconnect, "host": cmd_host,
          "claim": cmd_claim, "guard": cmd_guard, "peers": cmd_peers,
          "state": cmd_state}[args.cmd]

    if args.cmd in HOOK_CMDS:
        if os.environ.get("RG_IPC_DISABLE") == "1":
            return 0
        try:
            return fn(args)
        except Exception as e:  # hooks must never wedge a session
            _log(f"{args.cmd} error: {e!r}")
            return 0
    return fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
