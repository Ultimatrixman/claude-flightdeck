#!/usr/bin/env python3
"""Standalone installer for claude-flightdeck.

Most people should install the plugins instead:

    /plugin marketplace add Ultimatrixman/claude-flightdeck
    /plugin install flightdeck@claude-flightdeck

This script exists for the case the plugin system does not cover: wiring the
hooks straight into settings.json from a clone, which is how you run a modified
copy, pin a version, or use it somewhere plugins are unavailable.

    python3 install.py                 # both components + statusline capture
    python3 install.py --only ctxmon   # one component
    python3 install.py --no-statusline # skip the quota capture
    python3 install.py --uninstall     # remove everything this added
    python3 install.py --dry-run       # print the resulting settings, write nothing

Every write is preceded by a timestamped backup. Existing hooks are appended
to, never replaced: this file will not touch an entry it did not create.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
COMPONENTS = {
    "ctxmon": [
        ("UserPromptSubmit", None, "prompt", 15),
        ("PostToolUse",
         "Read|Bash|PowerShell|Grep|Glob|Agent|Task|WebFetch|WebSearch|Workflow",
         "tick", 10),
        ("Stop", None, "stop", 15),
        ("PreCompact", None, "precompact", 45),
        ("SessionStart", None, "sessionstart", 15),
        ("SessionEnd", None, "sessionend", 10),
    ],
    "ipc": [
        ("SessionStart", None, "register", 15),
        ("UserPromptSubmit", None, "recv", 10),
        ("PostToolUse", "Bash|PowerShell|Agent|Task", "recv-inline", 8),
        ("PreToolUse", "Edit|Write|NotebookEdit", "guard", 8),
        ("Stop", None, "stop", 60),
        ("SessionEnd", None, "deregister", 10),
    ],
}
# Ordering law, same as the bundle's: ctxmon's Stop hook records the idle
# transition in milliseconds while IPC's holds a rally window of up to 55
# seconds. ctxmon first, or the phase timestamp other sessions read is late.
ORDER = ["ctxmon", "ipc"]


def settings_path() -> Path:
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR")
                or (Path(os.path.expanduser("~")) / ".claude"))
    return base / "settings.json"


def wrapper_for(component: str) -> str:
    return (ROOT / "plugins" / component / "bin" / component).as_posix()


def is_ours(cmd: str) -> bool:
    """True for a command this installer wrote, for any clone location."""
    return "/plugins/ctxmon/bin/ctxmon" in cmd or "/plugins/ipc/bin/ipc" in cmd


def load_settings(p: Path) -> dict:
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError as e:
        sys.exit(f"{p} is not valid JSON ({e}). Fix or move it first; "
                 f"this installer will not overwrite a file it cannot parse.")


def backup(p: Path) -> Path | None:
    if not p.is_file():
        return None
    dst = p.with_suffix(p.suffix + f".bak.flightdeck-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(p, dst)
    return dst


def strip_ours(settings: dict) -> int:
    removed = 0
    hooks = settings.get("hooks") or {}
    for event in list(hooks):
        kept = []
        for group in hooks[event]:
            inner = [h for h in group.get("hooks", [])
                     if not is_ours(h.get("command", ""))]
            removed += len(group.get("hooks", [])) - len(inner)
            if inner:
                group["hooks"] = inner
                kept.append(group)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if hooks:
        settings["hooks"] = hooks
    else:
        settings.pop("hooks", None)
    return removed


def add_hooks(settings: dict, components: list[str]) -> int:
    hooks = settings.setdefault("hooks", {})
    added = 0
    for comp in [c for c in ORDER if c in components]:
        for event, matcher, sub, timeout in COMPONENTS[comp]:
            entry = {"hooks": [{
                "type": "command",
                "command": f'"{wrapper_for(comp)}" {sub}',
                "timeout": timeout,
            }]}
            if matcher:
                entry["matcher"] = matcher
            arr = hooks.setdefault(event, [])
            # ctxmon's Stop must precede any pre-existing long-poll listener.
            if comp == "ctxmon" and event == "Stop":
                arr.insert(0, entry)
            else:
                arr.append(entry)
            added += 1
    return added


def wire_statusline(settings: dict, state_dir: Path, dry: bool) -> str:
    wrapper = (ROOT / "plugins" / "ctxmon" / "statusline.sh").as_posix()
    new_cmd = f'bash "{wrapper}"'
    current = (settings.get("statusLine") or {}).get("command", "")
    inner_file = state_dir / "ctxmon" / "statusline-inner.sh"

    # A record holding one of our own wrappers was written by a version that did
    # not recognise its own command, and leaving it means the wrapper keeps
    # handing the payload to a wrapper: unbounded recursion, both of them
    # reading this same file. Repair it before anything else, including before
    # the already-wired exit, or a rerun would never reach the repair.
    healed = False
    if not dry:
        try:
            if is_ours_statusline(inner_file.read_text(encoding="utf-8")):
                inner_file.unlink()
                healed = True
        except OSError:
            pass
    repaired = ("; discarded a recorded statusline that was one of our own "
                "wrappers" if healed else "")

    if current.strip() == new_cmd:
        return f"statusline: already wired{repaired}"

    # `ctxmon setup` wires a different path than this script does, so "already
    # ours" is not "already wired". Recording our own command would either point
    # a wrapper at a wrapper or, written empty, throw away the only copy of what
    # the user had before.
    ours = is_ours_statusline(current)
    if not dry and not ours:
        inner_file.parent.mkdir(parents=True, exist_ok=True)
        # An EMPTY file means "there was no statusline", which the wrapper
        # treats differently from the file being absent (absent = fall back to
        # detecting claude-hud).
        #
        # newline="\n" is not cosmetic: this file's contents are handed to
        # `sh -c`, and a CRLF here would append a stray \r to the command.
        with open(inner_file, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(current)
    settings["statusLine"] = {"type": "command", "command": new_cmd}
    if ours:
        return (f"statusline: repointed here from another of our own paths; "
                f"{inner_file} kept as it was{repaired}")
    return (f"statusline: wrapped (previous command saved to {inner_file})"
            f"{repaired}" if current else
            f"statusline: installed (no previous statusline; {inner_file} "
            f"left empty){repaired}")


def is_ours_statusline(cmd: str) -> bool:
    """True if cmd already runs one of our wrappers, wherever it sits.

    Wider than the path this script wires, because `ctxmon setup` installs the
    wrapper to `~/.claude/flightdeck/statusline.sh` and 0.1.1's setup prose
    pointed straight at a versioned plugin directory. Missing either of those
    is what lets a wrapper be recorded as its own inner command.

    Kept in step with `_is_ours` in plugins/ctxmon/ctxmon.py.
    """
    path = (cmd or "").replace("\\", "/")
    if "statusline.sh" not in path:
        return False
    return any(f"/{d}/" in path for d in ("ctxmon", "flightdeck"))


def main() -> int:
    ap = argparse.ArgumentParser(description="Install claude-flightdeck hooks")
    ap.add_argument("--only", choices=sorted(COMPONENTS), action="append",
                    help="install just this component (repeatable)")
    ap.add_argument("--no-statusline", action="store_true",
                    help="skip quota capture (context telemetry still works)")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    p = settings_path()
    settings = load_settings(p)
    state_dir = Path(os.environ.get("FLIGHTDECK_DIR")
                     or (p.parent / "flightdeck"))
    notes = []

    removed = strip_ours(settings)
    if removed:
        notes.append(f"removed {removed} existing flightdeck hook(s)")

    if args.uninstall:
        cur = (settings.get("statusLine") or {}).get("command", "")
        if is_ours_statusline(cur):
            inner = state_dir / "ctxmon" / "statusline-inner.sh"
            prev = inner.read_text(encoding="utf-8").strip() if inner.is_file() else ""
            # A record holding one of our own wrappers was written by a version
            # that did not recognise its own command. Handing it back would
            # point the wrapper at a wrapper reading this same file.
            if is_ours_statusline(prev):
                prev = ""
                notes.append("statusline: the recorded command was ours, so it "
                             "was not restored; set yours again by hand")
            if prev:
                settings["statusLine"] = {"type": "command", "command": prev}
                notes.append("statusline: restored your previous command")
            else:
                settings.pop("statusLine", None)
                notes.append("statusline: removed")
        notes.append("uninstalled")
    else:
        components = args.only or list(COMPONENTS)
        notes.append(f"added {add_hooks(settings, components)} hook(s) for "
                     f"{', '.join(components)}")
        if "ctxmon" in components and not args.no_statusline:
            notes.append(wire_statusline(settings, state_dir, args.dry_run))
        elif not args.no_statusline:
            notes.append("statusline: skipped (ctxmon not selected)")

    if args.dry_run:
        print(json.dumps(settings, indent=2))
        print("\n-- dry run, nothing written --", file=sys.stderr)
        for n in notes:
            print(f"   {n}", file=sys.stderr)
        return 0

    b = backup(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {p}")
    if b:
        print(f"backup {b}")
    for n in notes:
        print(f"   {n}")
    print("\nRestart Claude Code for the changes to take effect.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
