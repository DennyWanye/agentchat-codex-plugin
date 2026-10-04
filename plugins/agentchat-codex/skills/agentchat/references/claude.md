# Claude Code adapter

This connects an existing Claude Code session on macOS/Linux through its local
inbox socket. It does not start `claude -p`, fork history, or drive the UI.

Anthropic documents the socket and session inbound controls:
https://code.claude.com/docs/en/cross-session-messaging#the-sessions-inbox-socket

The JSON wire frame and on-disk registry are version-sensitive, independently
documented from Claude Code v2.1.233 in this implementation source:
https://github.com/PeterSR/claude-code-socket-transport

The adapter is an experimental peer-protocol-v1 integration, NOT a stable
Anthropic API. Require a successful real target-session probe before declaring
it supported. Feature availability starts at v2.1.224 but that version floor
alone is insufficient evidence of frame compatibility. Record installed host
version and the actual bidirectional result in the task handoff.

`doctor --claude` reads live entries from `~/.claude/sessions`, selects no target,
and prints no tokens. `service install --claude-session <UUID>` requires exactly
one matching live registry entry with a private inbox socket and peerProtocol=1.
Every send re-resolves that exact UUID. A new PID can resume the same session;
`/clear` creating a different UUID does not silently inherit the binding.

Only a local inbox pointer is sent, with fixed AgentChat attribution and the
exact target session ID. A stable host message UUID is derived from the relay
message ID, but this is not a claim of host-side exactly-once execution.

The adapter does not read or use `CLAUDE_CODE_MESSAGING_TOKEN`, set a bypass
permission class, or alter `crossSessionInbound`. A socket write reports
`submitted_unconfirmed`; Claude may hold or refuse it. Verify actual receipt
and an explicit reply. If the current policy blocks it, explain the specific
condition to the user instead of weakening settings automatically.

Replies go through the skill's `agentchat-bridge send` command, not an extra
unbounded socket listener. No automatic transcript scraping or forwarding.
