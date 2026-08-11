---
description: "Cross-session chat: first /pong searches for an existing session (the host) and connects; second /pong closes the connection"
argument-hint: "[peer-name]"
---

Toggle this session's cross-session connection. Argument (optional peer name):
"$ARGUMENTS"

**Your IPC name and the exact command to run were both announced at session
start**, in the line beginning "Cross-session IPC: your IPC name is '<name>'".
That announcement contains the full path to the client for this install — use
it verbatim as `<ipc>` below, and the announced name as `<you>`.

If no such announcement exists, the IPC plugin's SessionStart hook did not run.
Tell the user to check `/plugin` and stop; do not guess a path.

Follow this exactly:

1. Run: `<ipc> state --name <you>`
   If it errors with "relay unreachable", tell the user and stop.

2. **If `role` is `"host"`**: this session is the host — guests connect HERE
   with /pong; it doesn't dial out. Tell the user that, and that `/ping` steps
   down. Stop.

3. **If `connected_to` is set** (second /pong → close the active connection):
   - Run: `<ipc> disconnect --name <you> --to <connected_to>`
   - Tell the user: `🏓 Disconnected from <peer> — they'll confirm with a final ping.`
   - End your turn.

4. **Otherwise** (first /pong → search for an existing session and connect):
   - If "$ARGUMENTS" is non-empty, run: `<ipc> connect --name <you> --to $ARGUMENTS`
   - Else run (it finds the declared host automatically): `<ipc> connect --name <you>`
   - Relay the command's output to the user verbatim. If it errored with "no
     host session found", it lists the peers online — tell the user to either
     run /ping in the session that should host, or re-run `/pong <peer-name>`.
   - **End your turn immediately.** The Stop listener takes over: it holds a
     ~45s receptive window after each of your turns and injects each incoming
     ball with exact handling instructions — follow those instructions when
     they arrive (connect → reply "ping"; ping → display only; msg → answer
     briefly; disconnect → say "ping 🏓 (disconnected)").

While connected, the user can hand you content to deliver ("ask the host about
X") — send it with:
`<ipc> send --name <you> --to <peer> --text "..."`
