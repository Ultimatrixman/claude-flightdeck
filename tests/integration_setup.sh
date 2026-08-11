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

echo
printf 'setup integration: %d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ] || exit 1
