#!/usr/bin/env bash
# Statusline wrapper: capture Claude Code's statusline payload for ctxmon, then
# hand the identical bytes on to whatever statusline you already had.
#
# Why this exists: rate_limits (the 5-hour and 7-day quota windows) and the
# authoritative context_window block are delivered ONLY to the statusline
# command. Hooks never see them. This is the single point where that payload is
# observable, so it is the only place quota can be captured. A plugin cannot
# ship a statusLine of its own, which is why `/ctxmon:setup` installs this.
#
# Cost: zero added process spawns. Stdin capture, session-id extraction and the
# completeness check are all bash builtins. A Python tee measured 91ms per
# spawn on the development machine, unaffordable at the ~300ms cadence.
#
# Fails open everywhere. The worst a bug here can cost is one blank status
# frame, which self-corrects on the next tick.

cfg="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
fd="${FLIGHTDECK_DIR:-$cfg/flightdeck}"
state="${CTXMON_STATE_DIR:-$fd/ctxmon}"

# Width handling, preserved from Claude Code's default statusline contract.
cols=${COLUMNS:-}
case "$cols" in ""|*[!0-9]*) cols=$( { stty size </dev/tty | awk '{print $2}'; } 2>/dev/null );; esac
case "$cols" in ""|*[!0-9]*) cols=120;; esac
export COLUMNS=$(( cols > 4 ? cols - 4 : 1 ))

# Read the entire payload. -d '' reads to EOF (no NUL byte appears in JSON);
# -t 2 bounds the wait so a stdin that never closes cannot hang the status
# line. A non-zero return at EOF is normal and must not be treated as an error:
# the variable is fully populated either way.
payload=""
IFS= read -r -d '' -t 2 payload

# Trim the trailing newline before the completeness check. `read -d ''` keeps
# it, so a naive `*'}'` test never matches a payload that is in fact complete.
ws="${payload##*[!$'\n\r\t ']}"
core="${payload%"$ws"}"

case "$core" in
  *'"session_id"'*)
    case "$core" in
      *'}')
        t="${core#*\"session_id\":\"}"
        s="${t:0:8}"
        case "$s" in
          [0-9a-f][0-9a-f][0-9a-f][0-9a-f]*)
            mkdir -p "$state/sl" 2>/dev/null
            printf '%s' "$core" > "$state/sl/$s.json" 2>/dev/null
            ;;
        esac
        ;;
    esac
    ;;
esac

# Delegate to whatever the user had before. `/ctxmon:setup` records it here; an
# empty file means "there was no statusline", which is not the same as "we have
# not looked yet".
inner_file="$state/statusline-inner.sh"
if [ -r "$inner_file" ]; then
  inner=$(cat "$inner_file" 2>/dev/null)
  if [ -n "$inner" ]; then
    printf '%s' "$payload" | sh -c "$inner"
    exit 0
  fi
else
  # No setup record: fall back to claude-hud if it happens to be installed.
  plugin_dir=$(ls -1d "$cfg"/plugins/cache/*/claude-hud/*/ 2>/dev/null | sort -V | tail -1)
  node_bin="/c/Program Files/nodejs/node.exe"
  [ -x "$node_bin" ] || node_bin=$(command -v node 2>/dev/null)
  if [ -n "$plugin_dir" ] && [ -f "${plugin_dir}dist/index.js" ] && [ -n "$node_bin" ]; then
    printf '%s' "$payload" | "$node_bin" "${plugin_dir}dist/index.js"
    exit 0
  fi
fi

# Nothing to delegate to: render a minimal line rather than leaving the user
# with a blank bar they did not ask for. Pure bash regex, no spawns.
model=""; ctx=""; five=""; seven=""
[[ $core =~ \"display_name\":\"([^\"]+)\" ]] && model="${BASH_REMATCH[1]}"
[[ $core =~ \"used_percentage\":([0-9]+) ]] && ctx="${BASH_REMATCH[1]}"
if [[ $core =~ \"five_hour\":\{\"used_percentage\":([0-9]+) ]]; then five="${BASH_REMATCH[1]}"; fi
if [[ $core =~ \"seven_day\":\{\"used_percentage\":([0-9]+) ]]; then seven="${BASH_REMATCH[1]}"; fi
line=""
[ -n "$model" ] && line="$model"
[ -n "$ctx" ] && line="$line │ ctx ${ctx}%"
[ -n "$five" ] && line="$line │ 5h ${five}%"
[ -n "$seven" ] && line="$line │ 7d ${seven}%"
printf '%s' "${line:-flightdeck}"
