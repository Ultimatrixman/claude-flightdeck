#!/usr/bin/env python3
"""Local message relay for Claude Code cross-session IPC.

Stdlib-only ThreadingHTTPServer. One instance per machine (or one shared on a
LAN host for cross-machine rallies). Peers register a name; messages queue per
recipient; /recv supports long-poll so an armed session can wait for the next
ball without spinning.

Endpoints (JSON):
  GET  /health                        -> {ok, pid}
  GET  /peers                         -> {ok, peers: [...]}
  GET  /state?name=|session=          -> {ok, found, name, active, connected_to, peers}
  GET  /recv?name=|session=&wait=N    -> {ok, name, messages: [...]}   (long-poll)
  POST /register {name, session_id, host, pid, cwd}  -> {ok} | {ok: false, taken}
  POST /deregister {session_id | name}               -> {ok}
  POST /send {from, to, kind, text}                  -> {ok} | {ok: false, error}
  POST /connect {from, to}                           -> {ok, mutual}
  POST /disconnect {from, to}                        -> {ok}

If RG_IPC_TOKEN is set in the relay's environment, every request must carry a
matching X-RG-Token header (use this when binding beyond localhost).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Shared with ipc.py; see the note there on why this is not the plugin dir.
_CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR")
                   or (Path(os.path.expanduser("~")) / ".claude"))
FLIGHTDECK_DIR = Path(os.environ.get("FLIGHTDECK_DIR") or (_CLAUDE_DIR / "flightdeck"))
STATE_DIR = Path(os.environ.get("IPC_STATE_DIR")
                 or os.environ.get("RG_IPC_STATE_DIR")
                 or (FLIGHTDECK_DIR / "ipc"))
SNAPSHOT = STATE_DIR / "relay-state.json"
LOG = STATE_DIR / "relay.log"
NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,32}$")
MAX_QUEUE = 200
MAX_TEXT = 8000
MAX_WAIT = 55.0
PEER_TTL = 86400.0
FRESH = 600.0  # a name is contested only if its holder was seen this recently
CLAIM_STALE = 14400.0  # a claim reads as abandoned once its holder is this silent

_lock = threading.Lock()
_peers: dict = {}    # name -> {name, session_id, host, pid, cwd, active, connected_to, last_seen}
_queues: dict = {}   # name -> [msg]
_events: dict = {}   # name -> threading.Event
_token = os.environ.get("RG_IPC_TOKEN", "")


def _log(msg: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        if LOG.exists() and LOG.stat().st_size > 262144:
            LOG.unlink()
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def _save() -> None:
    """Snapshot peers (not queues) so a relay restart keeps the registry. Call under _lock."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(json.dumps(_peers, indent=1), encoding="utf-8")
    except OSError:
        pass


def _load() -> None:
    try:
        data = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            with _lock:
                _peers.update(data)
    except (OSError, ValueError):
        pass


def _enqueue(to: str, msg: dict) -> None:
    """Under _lock."""
    q = _queues.setdefault(to, [])
    q.append(msg)
    del q[:-MAX_QUEUE]
    _events.setdefault(to, threading.Event()).set()


def _resolve(name, session):
    """Under _lock."""
    if name and name in _peers:
        return _peers[name]
    if session:
        for p in _peers.values():
            if p.get("session_id") == session:
                return p
    return None


def _host_name():
    """Under _lock. The single declared host, if any."""
    for p in _peers.values():
        if p.get("role") == "host":
            return p["name"]
    return None


def _pat_prefix(pat: str) -> str:
    """Literal prefix of an fnmatch pattern, normalized for comparison."""
    return pat.replace("\\", "/").lower().split("*")[0].split("?")[0].rstrip("/")


def _claims_overlap(paths_a, paths_b) -> list:
    """Directory-prefix overlap between two claim pattern lists."""
    hits = []
    for a in paths_a or []:
        pa = _pat_prefix(a)
        for b in paths_b or []:
            pb = _pat_prefix(b)
            if pa and pb and (pa.startswith(pb) or pb.startswith(pa)):
                hits.append(f"{a} ~ {b}")
    return hits


def _claim_view(peer: dict, now: float):
    """Under _lock. A peer's claim annotated with its own age and whether its
    holder has gone silent long enough for it to read as abandoned.

    Staleness is COMPUTED from last_seen, never stored: a long plan whose
    session keeps checking in is not stale however old the claim is, and a
    session that comes back refreshes last_seen so its claim goes live again
    with no command to run.
    """
    c = peer.get("claim")
    if not c:
        return None
    return {**c, "age_s": round(now - c.get("ts", now), 1),
            "stale": (now - peer.get("last_seen", now)) > CLAIM_STALE}


def _summaries() -> list:
    """Under _lock. Prunes peers not seen within PEER_TTL."""
    now = time.time()
    for n in [n for n, p in _peers.items() if now - p.get("last_seen", 0) > PEER_TTL]:
        _peers.pop(n, None)
    return [
        {"name": p["name"], "host": p.get("host", ""), "cwd": p.get("cwd", ""),
         "active": p.get("active", False), "connected_to": p.get("connected_to"),
         "role": p.get("role"), "claim": _claim_view(p, now),
         "age_s": round(now - p.get("last_seen", now), 1)}
        for p in _peers.values()
    ]


def _register(body: dict):
    name = str(body.get("name", ""))
    sid = str(body.get("session_id", ""))
    if not NAME_RE.match(name):
        return 400, {"ok": False, "error": "bad name (use [A-Za-z0-9_-], max 32)"}
    with _lock:
        ex = _peers.get(name)
        if ex and ex.get("session_id") != sid and time.time() - ex.get("last_seen", 0) < FRESH:
            return 200, {"ok": False, "taken": True}
        # Same session re-registering (resume / /clear) starts unpaired: the
        # peer's context no longer contains the rally state. The work CLAIM
        # survives — the plan is still in flight even if the context isn't.
        _peers[name] = {
            "name": name, "session_id": sid, "host": str(body.get("host", "")),
            "pid": body.get("pid", 0), "cwd": str(body.get("cwd", "")),
            "active": False, "connected_to": None, "role": None,
            "claim": (ex or {}).get("claim"),
            "last_seen": time.time(),
        }
        _save()
    _log(f"register {name} sid={sid[:8]}")
    return 200, {"ok": True, "name": name}


def _deregister(body: dict):
    sid = str(body.get("session_id", ""))
    name = str(body.get("name", ""))
    with _lock:
        peer = _resolve(name or None, sid or None)
        if peer:
            gone = peer["name"]
            gone_to = peer.get("connected_to")
            _peers.pop(gone, None)
            for p in _peers.values():
                if p.get("connected_to") == gone:
                    p["connected_to"] = None
                    p["active"] = False
                    _enqueue(p["name"], {"from": gone, "to": p["name"],
                                         "kind": "disconnect", "text": "", "ts": time.time()})
            # A departing guest also tells its host (the host isn't connected_to it)
            if gone_to and _peers.get(gone_to, {}).get("role") == "host":
                _enqueue(gone_to, {"from": gone, "to": gone_to,
                                   "kind": "disconnect", "text": "", "ts": time.time()})
            _save()
            _log(f"deregister {gone}")
    return 200, {"ok": True}


def _send(body: dict):
    frm, to = str(body.get("from", "")), str(body.get("to", ""))
    kind = str(body.get("kind", "msg") or "msg")
    text = str(body.get("text", ""))[:MAX_TEXT]
    if not frm or not to:
        return 400, {"ok": False, "error": "from and to required"}
    if kind not in ("msg", "ping", "connect", "disconnect"):
        return 400, {"ok": False, "error": f"bad kind '{kind}'"}
    with _lock:
        if to not in _peers:
            return 404, {"ok": False, "error": f"unknown peer '{to}'"}
        _enqueue(to, {"from": frm, "to": to, "kind": kind, "text": text, "ts": time.time()})
        p = _peers[to]
        # Staleness lets the sender diagnose a silent peer (mid-turn = hooks
        # can't fire = last_seen ages).
        return 200, {"ok": True,
                     "to_active": p.get("active", False),
                     "to_age_s": round(time.time() - p.get("last_seen", time.time()), 1)}


def _connect(body: dict):
    frm, to = str(body.get("from", "")), str(body.get("to", ""))
    with _lock:
        a, b = _peers.get(frm), _peers.get(to)
        if not a:
            return 404, {"ok": False, "error": f"'{frm}' is not registered"}
        if not b:
            return 404, {"ok": False, "error": f"unknown peer '{to}'"}
        a["active"] = True
        a["connected_to"] = to
        mutual = b.get("connected_to") == frm
        # Joining an armed host is instantly mutual: the host serves the ping.
        host_join = (not mutual) and b.get("role") == "host" and b.get("active", False)
        if mutual:
            # Completion serve goes to the side that armed FIRST — it prints "ping".
            b["active"] = True
        if mutual or host_join:
            _enqueue(to, {"from": frm, "to": to, "kind": "connect", "text": "", "ts": time.time()})
        _save()
    _log(f"connect {frm}->{to} mutual={mutual} host_join={host_join}")
    return 200, {"ok": True, "mutual": mutual or host_join, "host_join": host_join}


def _disconnect(body: dict):
    frm, to = str(body.get("from", "")), str(body.get("to", ""))
    with _lock:
        a, b = _peers.get(frm), _peers.get(to)
        was_connected = a is not None and a.get("connected_to") == to
        if a:
            a["active"] = False
            a["connected_to"] = None
        if b and b.get("connected_to") == frm:
            b["connected_to"] = None
            b["active"] = False
            _enqueue(to, {"from": frm, "to": to, "kind": "disconnect", "text": "", "ts": time.time()})
        elif b and was_connected and b.get("role") == "host":
            # A guest leaving a host: notify it; the host stays armed for others.
            _enqueue(to, {"from": frm, "to": to, "kind": "disconnect", "text": "", "ts": time.time()})
        _save()
    _log(f"disconnect {frm}->{to}")
    return 200, {"ok": True}


def _sethost(body):
    name = str(body.get("name", ""))
    clear = bool(body.get("clear"))
    notified = []
    with _lock:
        p = _peers.get(name)
        if not p:
            return 404, {"ok": False, "error": f"'{name}' is not registered"}
        if clear:
            if p.get("role") == "host":
                p["role"] = None
                p["active"] = False
                for g in _peers.values():
                    if g.get("connected_to") == name:
                        g["connected_to"] = None
                        g["active"] = False
                        notified.append(g["name"])
                        _enqueue(g["name"], {"from": name, "to": g["name"],
                                             "kind": "disconnect", "text": "", "ts": time.time()})
            _save()
            host = None
        else:
            for q in _peers.values():  # single host at a time
                if q.get("role") == "host" and q["name"] != name:
                    q["role"] = None
            p["role"] = "host"
            p["active"] = True
            p["connected_to"] = None
            _save()
            host = name
    _log(f"host {'cleared by ' + name if clear else '= ' + name}"
         + (f" (disconnected {', '.join(notified)})" if notified else ""))
    return 200, {"ok": True, "host": host, "disconnected": notified}


def _claim(body):
    name = str(body.get("name", ""))
    with _lock:
        p = _peers.get(name)
        if not p:
            return 404, {"ok": False, "error": f"'{name}' is not registered"}
        if body.get("clear"):
            p["claim"] = None
            _save()
            return 200, {"ok": True, "claim": None, "overlaps": []}
        desc = str(body.get("desc", ""))[:200]
        paths = [str(x)[:300] for x in (body.get("paths") or [])][:40]
        p["claim"] = {"desc": desc, "paths": paths, "ts": time.time()}
        overlaps = []
        for q in _peers.values():
            if q["name"] == name or not q.get("claim"):
                continue
            hits = _claims_overlap(paths, q["claim"].get("paths"))
            if hits:
                overlaps.append({"name": q["name"], "desc": q["claim"].get("desc", ""),
                                 "hits": hits})
                _enqueue(q["name"], {
                    "from": name, "to": q["name"], "kind": "msg", "ts": time.time(),
                    "text": (f"⚠ Work-claim overlap: I just claimed «{desc}» covering "
                             f"{', '.join(paths)} — which overlaps your claim "
                             f"«{q['claim'].get('desc', '')}» ({'; '.join(hits)}). "
                             f"Let's coordinate before either of us edits the shared area.")})
        _save()
    _log(f"claim {name}: {desc} ({len(paths)} paths, {len(overlaps)} overlaps)")
    return 200, {"ok": True, "claim": {"desc": desc, "paths": paths}, "overlaps": overlaps}


def _state(name, session):
    with _lock:
        p = _resolve(name, session)
        peers = _summaries()
        host = _host_name()
        if not p:
            return {"ok": True, "found": False, "name": None, "active": False,
                    "connected_to": None, "role": None, "host": host,
                    "claim": None, "guests": [], "peers": peers}
        p["last_seen"] = time.time()
        guests = ([g["name"] for g in _peers.values()
                   if g.get("connected_to") == p["name"] and g.get("active")]
                  if p.get("role") == "host" else [])
        return {"ok": True, "found": True, "name": p["name"],
                "active": p.get("active", False),
                "connected_to": p.get("connected_to"),
                "role": p.get("role"), "host": host, "guests": guests,
                "claim": _claim_view(p, time.time()), "peers": peers}


def _recv(name, session, wait: float, if_active: bool = False):
    deadline = time.time() + wait
    while True:
        with _lock:
            p = _resolve(name, session)
            if not p:
                return {"ok": True, "name": None, "messages": []}
            p["last_seen"] = time.time()
            nm = p["name"]
            if if_active and not p.get("active", False):
                # Mid-turn delivery is rally-only: leave the queue for the
                # next prompt/turn-end seam.
                return {"ok": True, "name": nm, "messages": []}
            q = _queues.get(nm) or []
            if q:
                msgs = list(q)
                q.clear()
                return {"ok": True, "name": nm, "messages": msgs}
            if time.time() >= deadline:
                return {"ok": True, "name": nm, "messages": []}
            ev = _events.setdefault(nm, threading.Event())
            ev.clear()
        ev.wait(min(1.0, max(0.05, deadline - time.time())))


class Handler(BaseHTTPRequestHandler):
    server_version = "RGIPCRelay/1.0"

    def log_message(self, fmt, *args):
        pass

    def _json(self, code: int, obj: dict) -> None:
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except OSError:
            pass

    def _auth_ok(self) -> bool:
        return not _token or self.headers.get("X-RG-Token", "") == _token

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            return json.loads(raw) if raw else {}
        except (ValueError, OSError):
            return None

    def do_GET(self):
        if not self._auth_ok():
            return self._json(401, {"ok": False, "error": "bad token"})
        u = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/health":
            return self._json(200, {"ok": True, "pid": os.getpid()})
        if u.path == "/peers":
            with _lock:
                peers = _summaries()
                host = _host_name()
            return self._json(200, {"ok": True, "peers": peers, "host": host})
        if u.path == "/state":
            return self._json(200, _state(qs.get("name"), qs.get("session")))
        if u.path == "/recv":
            try:
                wait = min(MAX_WAIT, max(0.0, float(qs.get("wait", "0"))))
            except ValueError:
                wait = 0.0
            return self._json(200, _recv(qs.get("name"), qs.get("session"), wait,
                                         qs.get("if_active") == "1"))
        return self._json(404, {"ok": False, "error": "unknown path"})

    def do_POST(self):
        if not self._auth_ok():
            return self._json(401, {"ok": False, "error": "bad token"})
        body = self._body()
        if body is None:
            return self._json(400, {"ok": False, "error": "bad json"})
        path = urlparse(self.path).path
        routes = {"/register": _register, "/deregister": _deregister, "/send": _send,
                  "/connect": _connect, "/disconnect": _disconnect, "/host": _sethost,
                  "/claim": _claim}
        fn = routes.get(path)
        if not fn:
            return self._json(404, {"ok": False, "error": "unknown path"})
        code, obj = fn(body)
        return self._json(code, obj)


def main() -> int:
    global STATE_DIR, SNAPSHOT, LOG
    ap = argparse.ArgumentParser(description="Claude Code cross-session IPC relay")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("RG_IPC_PORT", "4180")))
    ap.add_argument("--state-dir", default=None,
                    help="isolated state dir (tests); default: <script dir>/state")
    args = ap.parse_args()
    if args.state_dir:
        STATE_DIR = Path(args.state_dir)
        SNAPSHOT = STATE_DIR / "relay-state.json"
        LOG = STATE_DIR / "relay.log"
    _load()
    try:
        srv = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as e:
        _log(f"bind failed on {args.host}:{args.port}: {e} (already running?)")
        return 0  # a healthy twin on the port is the normal cause; not an error
    srv.daemon_threads = True
    _log(f"relay up on {args.host}:{args.port} pid={os.getpid()}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _log("relay down")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
