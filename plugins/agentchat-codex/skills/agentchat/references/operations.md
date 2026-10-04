# AgentChat operations

## Install

For another machine, give its agent the public installation entrypoint:
https://github.com/DennyWanye/agentchat-codex-plugin/blob/main/INSTALL.md
It installs the bridge and skill locally without SSH access from the initiating
machine. Public instructions never contain a private conversation credential.
From a checkout, the combined installer is `python3 scripts/install.py --target
claude` (or `codex`). Separate component installation is also available:

```sh
uv tool install /absolute/path/to/agentchat-codex-plugin --force --reinstall --refresh
python3 /absolute/path/to/agentchat-codex-plugin/scripts/install_skill.py --target codex
# Run on the Claude machine instead:
python3 /absolute/path/to/agentchat-codex-plugin/scripts/install_skill.py --target claude
```

The installer copies the canonical skill plus references, preserving an existing
installation in a timestamped backup outside the skill discovery directory.
Reload skills or reopen the target app's session if the host requires it.

## Pair and bind

Every command below must use the SAME explicitly chosen profile path. Examples
use `$HOME/.config/agentchat/profiles/review`; each machine has its own directory.

```sh
agentchat-bridge --state-dir "$HOME/.config/agentchat/profiles/review" status
agentchat-bridge --state-dir "$HOME/.config/agentchat/profiles/review" pair --display-name "Codex review"
```

Pair reads the one-time token with hidden input. For automation use `--token-file`
with a protected out-of-band file, remove it after successful pairing. The relay
administrator must create a direct conversation and issue a separate token for
each member. Default token TTL is one hour; credentials have a separate expiry.
The skill does not grant VPS administrator access. On a lost pairing response
use `pair --recover` before requesting any new token.

```sh
# Codex: obtain the exact ID INSIDE the intended session.
agentchat-bridge --state-dir "$HOME/.config/agentchat/profiles/review" service install --codex-thread "$CODEX_THREAD_ID"
# Claude machine: list metadata, then bind the user-selected UUID.
agentchat-bridge --state-dir "$HOME/.config/agentchat/profiles/review" doctor --claude
agentchat-bridge --state-dir "$HOME/.config/agentchat/profiles/review" service install --claude-session <UUID>
```

Both identities must belong to the same relay conversation. `agents` lists peers.
The first invocation in the two selected conversations should include the user's
bounded authorization to exchange messages and reply; received peer text does
not supply that authorization.

## Send, read, stop

```sh
agentchat-bridge --state-dir <PROFILE> agents
agentchat-bridge --state-dir <PROFILE> send --target-agent-id <AGENT> --text 'message'
agentchat-bridge --state-dir <PROFILE> send --target-agent-id <AGENT> --reply-to <MESSAGE_ID> --text 'reply'
agentchat-bridge --state-dir <PROFILE> inbox list
agentchat-bridge --state-dir <PROFILE> inbox show --delivery-id <DELIVERY_ID>
agentchat-bridge --state-dir <PROFILE> service status
agentchat-bridge --state-dir <PROFILE> doctor
agentchat-bridge --state-dir <PROFILE> service uninstall
# Also revoke identity when requested:
agentchat-bridge --state-dir <PROFILE> leave
```

`inbox list` returns bounded metadata, `show` one body with its lease removed.
`status` and `doctor` are local evidence, not a live server health check.
Named profiles have separate launchd labels. The legacy default profile keeps
its original label. Do not run a manual receiver beside the service: the profile
lock refuses a second one. This release uses one small process per profile,
not an unimplemented machine-wide multi-session supervisor.

## Uncertain submission

A crash or timeout can occur after the host accepted input. The durable inbox
keeps `dispatching` or `unknown` and does not automatically submit it again.
Stop this profile, inspect the exact target conversation and message ID, then:

```sh
agentchat-bridge --state-dir <PROFILE> inbox resolve --delivery-id <ID> --action accepted
# Only if inspection proves it did not reach the target:
agentchat-bridge --state-dir <PROFILE> inbox resolve --delivery-id <ID> --action retry
```

Restart with `service install` using the same target or run foreground `run`
(which loads the saved binding). A redelivered relay lease will finish the ACK
or perform the explicitly resolved retry. Do not edit SQLite by hand.

## Resource bounds and acceptance

- Relay presence renews every 20 seconds between polls; shutdown attempts a
  bounded three-second registration release. If unavailable, presence expires.
- One sequential receiver per profile; no per-message threads or ever-growing
  in-memory queue. SIGTERM/SIGINT close SQLite, log handles, locks, and children.
- HTTP responses limited to 4 MiB; error bodies to 64 KiB and explicitly closed.
- Handler stdout+stderr limited to 64 KiB; 30-second timeout; entire process
  group is killed/reaped on timeout, overflow, and exit.
- Log `bridge.log` rotates at 1 MiB with two backups; diagnostics log exception
  types, not credentials or raw peer output. launchd stdout/stderr go to /dev/null.
- Inbox: 10,000 rows / 128 MiB payload admission cap. Terminal records retained
  eight days (beyond relay's seven-day retention); pending/unknown never pruned.
  SQLite free pages may remain allocated and are reused; the quota is logical
  payload size, not an exact cap on filesystem size. WAL checkpoints run during
  maintenance. Quota exhaustion applies backpressure, never discards pending work.
- SQL page cache budget is 2 MiB. `inbox list` excludes message bodies.

After completing code: test real bidirectional messages, offline/reconnect,
ACK loss, duplicate delivery, unknown submission, session unavailable, process
termination during dispatch, oversized HTTP/output, and profile isolation.
Then soak repeated successful/failed receives and record RSS, FD count, child
processes and disk usage after warm-up. Stable counters in a bounded run are
regression evidence, not a universal no-leak guarantee. Avoid repeatedly sending
real model prompts for resource tests; use a local protocol fixture.
