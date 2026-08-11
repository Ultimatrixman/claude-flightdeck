#!/usr/bin/env python3
"""Validate every manifest in the repo and check they agree with the tree.

A broken manifest does not fail loudly at install time — it usually means a
component silently does not load, which is far harder to notice than a crash.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
errors: list[str] = []


def err(msg: str) -> None:
    errors.append(msg)


def load(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        err(f"{p.relative_to(ROOT)}: unreadable or invalid JSON: {e}")
        return None


market = load(ROOT / ".claude-plugin" / "marketplace.json") or {}
entries = market.get("plugins") or []
if not entries:
    err("marketplace.json lists no plugins")

seen_names = set()
for entry in entries:
    name, source = entry.get("name"), entry.get("source")
    if not name or not source:
        err(f"marketplace entry missing name/source: {entry}")
        continue
    if name in seen_names:
        err(f"duplicate marketplace entry: {name}")
    seen_names.add(name)

    pdir = (ROOT / source).resolve()
    if not pdir.is_dir():
        err(f"{name}: source {source} is not a directory")
        continue

    manifest_path = pdir / ".claude-plugin" / "plugin.json"
    if not manifest_path.is_file():
        err(f"{name}: missing .claude-plugin/plugin.json")
        continue
    man = load(manifest_path) or {}

    if man.get("name") != name:
        err(f"{name}: plugin.json name is {man.get('name')!r}, "
            f"marketplace says {name!r}")
    for field in ("description", "version", "license"):
        if not man.get(field):
            err(f"{name}: plugin.json missing {field}")

    for rel in man.get("commands") or []:
        if not (pdir / rel).is_file():
            err(f"{name}: commands entry {rel} does not exist")

    hooks_rel = man.get("hooks")
    if isinstance(hooks_rel, str):
        hpath = pdir / hooks_rel
        if not hpath.is_file():
            err(f"{name}: hooks {hooks_rel} does not exist")
        else:
            cfg = load(hpath) or {}
            hooks = cfg.get("hooks") or {}
            if not hooks:
                err(f"{name}: hooks file declares no events")
            for event, groups in hooks.items():
                for group in groups:
                    for h in group.get("hooks", []):
                        cmd = h.get("command", "")
                        if "${CLAUDE_PLUGIN_ROOT}" not in cmd:
                            err(f"{name}/{event}: command does not use "
                                f"${{CLAUDE_PLUGIN_ROOT}}: {cmd}")
                        # The referenced wrapper must actually be shipped.
                        tail = cmd.split("}")[-1].strip('"').strip()
                        target = tail.split()[0].lstrip("/")
                        if target and not (pdir / target).is_file():
                            err(f"{name}/{event}: command targets missing "
                                f"file {target}")
                        if not isinstance(h.get("timeout"), int):
                            err(f"{name}/{event}: hook has no integer timeout")

# Executable bits matter on POSIX; git preserves them, so check the source tree.
for wrapper in ROOT.glob("plugins/*/bin/*"):
    text = wrapper.read_text(encoding="utf-8", errors="replace")
    if not text.startswith("#!"):
        err(f"{wrapper.relative_to(ROOT)}: missing shebang")

if errors:
    print(f"{len(errors)} manifest problem(s):")
    for e in errors:
        print(f"  - {e}")
    sys.exit(1)
print(f"manifests OK ({len(seen_names)} plugins: {', '.join(sorted(seen_names))})")
