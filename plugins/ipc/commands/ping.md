---
description: Toggle this session as cross-session HOST — other sessions' /pong connects here to chat
argument-hint: ""
---

Toggle this session's HOST role for cross-session IPC.

**Your IPC name and the exact command to run were both announced at session
start**, in the line beginning "Cross-session IPC: your IPC name is '<name>'".
That announcement contains the full path to the client for this install — use
it verbatim as `<ipc>` below, and the announced name as `<you>`.

If no such announcement exists, the IPC plugin's SessionStart hook did not run.
Tell the user to check `/plugin` and stop; do not guess a path.

Follow this exactly:

1. Run: `<ipc> state --name <you>`
   If it errors with "relay unreachable", tell the user and stop.

2. **If `role` is `"host"`** → step down:
   - Run: `<ipc> host --name <you> --clear`
   - Relay the output to the user verbatim. Stop.

3. **Otherwise** → become the host:
   - Run: `<ipc> host --name <you>`
   - Relay the output to the user verbatim, so they know this session is now
     the host and that `/pong` in any other session connects here.
   - **End your turn immediately.** The Stop listener holds a ~45s receptive
     window after each of your turns and injects each incoming ball with exact
     handling instructions — follow those when they arrive (connect → reply
     "ping"; ping → display only; msg → answer briefly; disconnect → say
     "ping 🏓 (disconnected)").

To see who else is online at any time: `<ipc> peers`
