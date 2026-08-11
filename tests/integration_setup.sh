#!/usr/bin/env bash
# End-to-end test of `ctxmon setup`, which edits the user's settings.json.
#
# Kept separate from integration.sh because it needs its own throwaway
# CLAUDE_CONFIG_DIR per case, and because a bug here corrupts the one file a
# user cannot easily reconstruct.

set -u

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
CTXMON="$ROOT/plugins/ctxmon/bin/ctxmon"

pass=0
fail=0
ok()   { pass=$((pass + 1)); printf '  ok    %s\n' "$1"; }
bad()  { fail=$((fail + 1)); printf '  FAIL  %s\n' "$1"; }
check() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (expected '$3', got '$2')"; fi; }

TMP=$(mktemp -d 2>/dev/null || mktemp -d -t fdsetup)
trap 'rm -rf "$TMP"' EXIT
native() {
    if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else printf '%s' "$1"; fi
}
TMP_N=$(native "$TMP")

mkdir -p "$TMP/claude"
SETTINGS="$TMP/claude/settings.json"
printf '{"model":"opus","statusLine":{"type":"command","command":"PREVIOUS-LINE"}}' > "$SETTINGS"

setup() {
    CLAUDE_CONFIG_DIR="$TMP_N/claude" FLIGHTDECK_DIR="$TMP_N/fd" "$CTXMON" setup "$@"
}

echo "== --dry-run changes nothing =="
setup --dry-run >/dev/null 2>&1
check "exits 0" "$?" "0"
if grep -q PREVIOUS-LINE "$SETTINGS"; then ok "settings untouched"
else bad "--dry-run modified settings.json"; fi

echo "== install =="
setup >/dev/null 2>&1
check "exits 0" "$?" "0"
grep -q "flightdeck/statusline.sh" "$SETTINGS" \
  && ok "statusLine points at the version-stable wrapper" \
  || bad "statusLine not installed"
grep -q '"model"' "$SETTINGS" \
  && ok "unrelated settings keys preserved" \
  || bad "setup dropped unrelated keys"
[ -f "$TMP/fd/statusline.sh" ] \
  && ok "wrapper copied outside the plugin directory" \
  || bad "wrapper not copied"
inner=$(cat "$TMP/fd/ctxmon/statusline-inner.sh" 2>/dev/null)
check "previous statusline recorded verbatim" "$inner" "PREVIOUS-LINE"
ls "$TMP/claude/"settings.json.bak.flightdeck-* >/dev/null 2>&1 \
  && ok "backup written" || bad "no backup written"

echo "== re-running is safe =="
setup >/dev/null 2>&1
inner=$(cat "$TMP/fd/ctxmon/statusline-inner.sh" 2>/dev/null)
# If a second run recorded OUR command as "the previous one", uninstall would
# restore the wrapper as its own inner command and loop forever.
check "record not overwritten with our own command" "$inner" "PREVIOUS-LINE"

echo "== uninstall =="
setup --uninstall >/dev/null 2>&1
check "exits 0" "$?" "0"
cur=$(grep -o 'PREVIOUS-LINE' "$SETTINGS" | head -1)
check "previous statusline restored" "$cur" "PREVIOUS-LINE"
setup --uninstall >/dev/null 2>&1
check "uninstalling twice is harmless" "$?" "0"

echo "== a settings.json we cannot parse is refused, not overwritten =="
printf '{ this is not json' > "$SETTINGS"
setup >/dev/null 2>&1
check "exits non-zero" "$?" "1"
grep -q "this is not json" "$SETTINGS" \
  && ok "left the unparseable file untouched" \
  || bad "overwrote a file it could not parse"

echo "== installs cleanly when there was no statusline at all =="
printf '{"model":"opus"}' > "$SETTINGS"
rm -rf "$TMP/fd"
setup >/dev/null 2>&1
check "exits 0" "$?" "0"
if [ -f "$TMP/fd/ctxmon/statusline-inner.sh" ]; then
  # Empty file means "there was none", which is not the same as absent
  # (absent tells the wrapper to look for claude-hud instead).
  inner=$(cat "$TMP/fd/ctxmon/statusline-inner.sh")
  check "inner record created empty" "${#inner}" "0"
else
  bad "inner record not created"
fi

echo "== a statusline already pointing at one of our wrappers is never recorded =="
# 0.1.1 shipped setup as prose telling Claude to point statusLine straight at
# the plugin's own copy, and install.py wires plugins/ctxmon/statusline.sh.
# Recording either as "the statusline you had before" would hand the payload to
# a wrapper that reads this same record: recursion on every tick, and the real
# previous command gone.
for ours in \
    "$TMP_N/cache/claude-flightdeck/flightdeck/0.1.1/statusline.sh" \
    "$TMP_N/cache/claude-flightdeck/ctxmon/0.1.1/statusline.sh" \
    "$TMP_N/repo/plugins/ctxmon/statusline.sh"
do
    mkdir -p "$TMP/fd/ctxmon"
    printf 'ORIGINAL-LINE' > "$TMP/fd/ctxmon/statusline-inner.sh"
    printf '{"statusLine":{"type":"command","command":"bash \\"%s\\""}}' \
        "$ours" > "$SETTINGS"
    setup >/dev/null 2>&1
    check "exits 0 for ${ours#"$TMP_N/"}" "$?" "0"
    inner=$(cat "$TMP/fd/ctxmon/statusline-inner.sh" 2>/dev/null)
    check "record left alone" "$inner" "ORIGINAL-LINE"
    # No trailing quote in the pattern: json.dumps escapes it as \" and the
    # versioned paths carry a version between the directory and the filename,
    # so the bare substring already tells the two apart.
    if grep -q 'flightdeck/statusline\.sh' "$SETTINGS"; then
        ok "migrated onto the version-stable path"
    else
        bad "left statusLine on a path that moves with the plugin version"
    fi
    setup --uninstall >/dev/null 2>&1
    cur=$(grep -o 'ORIGINAL-LINE' "$SETTINGS" | head -1)
    check "uninstall restores the real previous statusline" "$cur" "ORIGINAL-LINE"
done

echo "== a record poisoned by an earlier version is repaired, not just left =="
# 0.1.6 and earlier recorded our own wrapper as "the previous statusline" for
# any user who had followed 0.1.1's prose. Repairing settings.json is not
# enough: that file is on disk and would loop on the next tick or uninstall.
poison() {
    printf 'bash "%s/cache/claude-flightdeck/flightdeck/0.1.1/statusline.sh"' \
        "$TMP_N" > "$TMP/fd/ctxmon/statusline-inner.sh"
}
rm -rf "$TMP/fd"
printf '{"model":"opus","statusLine":{"type":"command","command":"PREVIOUS-LINE"}}' > "$SETTINGS"
setup >/dev/null 2>&1
poison
setup >/dev/null 2>&1
check "exits 0" "$?" "0"
[ -f "$TMP/fd/ctxmon/statusline-inner.sh" ] \
  && bad "kept a record that hands the wrapper to itself" \
  || ok "poisoned record discarded"
grep -q 'flightdeck/statusline\.sh' "$SETTINGS" \
  && ok "statusline still installed" \
  || bad "heal dropped the statusline"

echo "== uninstall never restores one of our own wrappers =="
setup >/dev/null 2>&1
poison
setup --uninstall >/dev/null 2>&1
check "exits 0" "$?" "0"
if grep -q statusLine "$SETTINGS"; then
  bad "restored a wrapper as the statusline"
else
  ok "statusLine removed rather than pointed at itself"
fi
grep -q '"model"' "$SETTINGS" \
  && ok "unrelated settings keys preserved" \
  || bad "uninstall dropped unrelated keys"

echo "== install.py agrees with setup about what counts as ours =="
# The standalone installer wires a different path than `ctxmon setup` does, so
# each has to recognise the other's wrapper. Whichever ran second would
# otherwise record the first one's command as "the statusline you had before".
PY=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then PY="$candidate"; break; fi
done
if [ -z "$PY" ]; then
    echo "  SKIPPED: no python3/python on PATH (install.py not exercised)"
else
    installer() {
        CLAUDE_CONFIG_DIR="$TMP_N/claude" FLIGHTDECK_DIR="$TMP_N/fd" \
            "$PY" "$ROOT/install.py" "$@"
    }
    rm -rf "$TMP/fd"
    printf '{"statusLine":{"type":"command","command":"PREVIOUS-LINE"}}' > "$SETTINGS"
    setup >/dev/null 2>&1
    installer >/dev/null 2>&1
    check "exits 0" "$?" "0"
    inner=$(cat "$TMP/fd/ctxmon/statusline-inner.sh" 2>/dev/null)
    check "install.py did not record setup's wrapper" "$inner" "PREVIOUS-LINE"

    poison
    installer >/dev/null 2>&1
    [ -f "$TMP/fd/ctxmon/statusline-inner.sh" ] \
      && bad "install.py left a record that would loop" \
      || ok "install.py discarded a poisoned record"

    # Its own wrapper is already wired at this point, which is the path that
    # used to return before reaching the repair.
    poison
    installer >/dev/null 2>&1
    [ -f "$TMP/fd/ctxmon/statusline-inner.sh" ] \
      && bad "already-wired rerun skipped the repair" \
      || ok "already-wired rerun still repairs the record"

    poison
    installer --uninstall >/dev/null 2>&1
    check "exits 0" "$?" "0"
    if grep -q statusLine "$SETTINGS"; then
      bad "install.py --uninstall restored a wrapper"
    else
      ok "install.py --uninstall removed it rather than looping"
    fi
fi

echo
printf 'setup integration: %d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ] || exit 1
