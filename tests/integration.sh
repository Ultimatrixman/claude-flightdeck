#!/usr/bin/env bash
# End-to-end test of the SHELL surface: the interpreter wrappers and the
# statusline. The Python suite covers logic; this covers the layer between
# Claude Code and Python, which is where the platform differences live.
#
# Runs on Linux, macOS and Git Bash on Windows. CI executes it on all three,
# which is the only realistic way to validate this without owning each machine.
#
# Every hook must exit 0 no matter what it is fed. That is the single most
# important property here: a non-zero hook can interrupt a Claude Code session.

set -u

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
CTXMON="$ROOT/plugins/ctxmon/bin/ctxmon"
IPC="$ROOT/plugins/ipc/bin/ipc"
STATUSLINE="$ROOT/plugins/ctxmon/statusline.sh"

pass=0
fail=0

ok()   { pass=$((pass + 1)); printf '  ok    %s\n' "$1"; }
bad()  { fail=$((fail + 1)); printf '  FAIL  %s\n' "$1"; }
check() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (expected '$3', got '$2')"; fi; }

TMP=$(mktemp -d 2>/dev/null || mktemp -d -t flightdeck)
trap 'rm -rf "$TMP"' EXIT

# On Git Bash, mktemp hands back a POSIX path (/tmp/...) that native Windows
# Python cannot open, so a transcript referenced that way reads as empty and
# every assertion about its contents fails for a reason that has nothing to do
# with the code. Claude Code passes native paths, so the harness must too.
# `cygpath -m` yields C:/style/paths, which both Python and bash accept.
native() {
    if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else printf '%s' "$1"; fi
}

TMP_N=$(native "$TMP")
export FLIGHTDECK_DIR="$TMP_N/fd"
export CLAUDE_CONFIG_DIR="$TMP_N/claude"
mkdir -p "$CLAUDE_CONFIG_DIR"

SID="abcd1234-0000-0000-0000-000000000000"
TRANSCRIPT="$TMP/transcript.jsonl"
cat > "$TRANSCRIPT" <<'EOF'
{"type":"assistant","timestamp":"2026-08-10T12:00:00.000Z","message":{"id":"m1","content":[],"usage":{"input_tokens":1,"cache_creation_input_tokens":49999,"cache_read_input_tokens":0,"output_tokens":120}}}
EOF
TRANSCRIPT_N=$(native "$TRANSCRIPT")
HOOK_JSON="{\"session_id\":\"$SID\",\"transcript_path\":\"$TRANSCRIPT_N\",\"cwd\":\"$TMP_N\"}"

echo "== interpreter wrapper resolves and runs =="
out=$(printf '%s' "$HOOK_JSON" | "$CTXMON" prompt 2>/dev/null)
rc=$?
check "ctxmon prompt exits 0" "$rc" "0"
case "$out" in
  *hookSpecificOutput*) ok "ctxmon prompt emits hook JSON" ;;
  *) bad "ctxmon prompt emitted no hook JSON: $out" ;;
esac
case "$out" in
  *'"ctx]'*|*'[ctx]'*) ok "status line present" ;;
  *) bad "no [ctx] line in output" ;;
esac

echo "== every hook exits 0 on garbage, empty and absent input =="
for cmd in prompt tick stop precompact sessionstart sessionend; do
  printf 'not json {{{' | "$CTXMON" "$cmd" >/dev/null 2>&1
  check "ctxmon $cmd survives garbage" "$?" "0"
  printf '' | "$CTXMON" "$cmd" >/dev/null 2>&1
  check "ctxmon $cmd survives empty" "$?" "0"
done
for cmd in register recv recv-inline guard stop deregister; do
  printf 'not json {{{' | "$IPC" "$cmd" >/dev/null 2>&1
  check "ipc $cmd survives garbage" "$?" "0"
done

echo "== tick is silent at steady state =="
printf '%s' "$HOOK_JSON" | "$CTXMON" tick >/dev/null 2>&1   # prime the band file
out=$(printf '%s' "$HOOK_JSON" | "$CTXMON" tick 2>/dev/null)
check "tick prints nothing when nothing changed" "${#out}" "0"

echo "== tick is silent inside a subagent =="
out=$(printf '{"session_id":"%s","agent_id":"sub-1","transcript_path":"%s"}' "$SID" "$TRANSCRIPT" \
      | "$CTXMON" tick 2>/dev/null)
check "tick silent for a subagent" "${#out}" "0"

echo "== CLI subcommands run =="
for cmd in status peers doctor plan; do
  "$CTXMON" "$cmd" >/dev/null 2>&1
  rc=$?
  # plan exits 1 when quota is unknown, which is correct, not a failure.
  if [ "$cmd" = "plan" ] && [ "$rc" = "1" ]; then rc=0; fi
  check "ctxmon $cmd runs" "$rc" "0"
done

echo "== disable switch silences every hook =="
out=$(printf '%s' "$HOOK_JSON" | CTXMON_DISABLE=1 "$CTXMON" prompt 2>/dev/null)
check "CTXMON_DISABLE=1 produces no output" "${#out}" "0"

echo "== statusline captures the payload and passes it through =="
SL_JSON="{\"session_id\":\"$SID\",\"transcript_path\":\"$TRANSCRIPT_N\",\"cwd\":\"$TMP_N\",\"model\":{\"display_name\":\"Test Model\"},\"context_window\":{\"used_percentage\":18,\"context_window_size\":1000000,\"current_usage\":{\"cache_read_input_tokens\":180000}},\"rate_limits\":{\"five_hour\":{\"used_percentage\":40,\"resets_at\":9999999999},\"seven_day\":{\"used_percentage\":12}}}"
mkdir -p "$FLIGHTDECK_DIR/ctxmon"
: > "$FLIGHTDECK_DIR/ctxmon/statusline-inner.sh"     # empty = "there was none"
sl_out=$(printf '%s' "$SL_JSON" | bash "$STATUSLINE" 2>/dev/null)
check "statusline exits 0" "$?" "0"
if [ -f "$FLIGHTDECK_DIR/ctxmon/sl/abcd1234.json" ]; then
  ok "statusline captured the payload"
else
  bad "statusline did not write sl/abcd1234.json"
fi
case "$sl_out" in
  *"Test Model"*) ok "built-in fallback line rendered" ;;
  *) bad "fallback line missing model: '$sl_out'" ;;
esac

echo "== statusline delegates to a previous statusline verbatim =="
printf 'printf "INNER-RAN"' > "$FLIGHTDECK_DIR/ctxmon/statusline-inner.sh"
sl_out=$(printf '%s' "$SL_JSON" | bash "$STATUSLINE" 2>/dev/null)
case "$sl_out" in
  *INNER-RAN*) ok "delegated to the recorded inner statusline" ;;
  *) bad "did not delegate: '$sl_out'" ;;
esac

echo "== window size is taken from the captured payload, with no claude-hud =="
out=$(printf '%s' "$HOOK_JSON" | "$CTXMON" prompt 2>/dev/null)
case "$out" in
  *"1.00M"*) ok "1M window read from the statusline payload" ;;
  *) bad "window not read from payload (P0 regression): $out" ;;
esac

echo
printf 'integration: %d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ] || exit 1
