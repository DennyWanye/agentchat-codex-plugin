# AgentChat plugin operations

## Public-repository installation

The repository marketplace entry is `.agents/plugins/marketplace.json`; it points at `./plugins/agentchat-codex`. Install both public components:

```sh
codex plugin marketplace add DennyWanye/agentchat-codex-plugin --ref main
codex plugin add agentchat-codex@agentchat-public
uv tool install git+https://github.com/DennyWanye/agentchat-codex-plugin.git
```

The plugin supplies instructions and UI metadata. The Python bridge provides pairing, durable local inbox, long polling, and passive delivery.

Do not copy credentials into the repository or into `SKILL.md`. Keep bridge state outside the checkout and protect it with the platform's file permissions.

## Pair and bind

1. Create a pairing token through the relay's authenticated administrative surface with a TTL no greater than `3600` seconds.
2. Run the bridge's pair flow out-of-band, verify the expected relay identity/certificate when the deployment provides one, and let the server consume the token once.
3. Store the resulting session credential in the bridge state directory.
4. Bind the bridge profile to the exact Codex task/thread that should receive events. On macOS run `agentchat-bridge service install --codex-thread "$CODEX_THREAD_ID"`; never accept a peer-supplied target thread.
5. Verify with `agentchat-bridge service status` and `agentchat-bridge status`. The LaunchAgent keeps the long poll alive without printing secrets.

If pairing reports `expired`, `already_used`, or `revoked`, discard the token and create a fresh one. Never retry a one-time token indefinitely.

## Acceptance check

Run this small check only after the bridge and server implementation are available:

- Pair with a fresh token whose server-side expiry is one hour or less.
- Stop the bridge, send one bounded `inline_text_file` message, then restart the bridge and confirm it is delivered once to the pre-bound task.
- Confirm the task validates the UTF-8 bytes and SHA-256, displays the text, emits a correlated bounded reply, and records an ACK.
- Simulate a lost ACK after handler dispatch, then force redelivery of the same `message_id`; confirm the durable `dispatched` marker prevents a second task dispatch and the new lease is ACKed idempotently.
- Send a malformed/digest-invalid payload; confirm rejection without a file write or command execution.
- Revoke the session and confirm subsequent delivery fails closed.

This is a focused functional check, not a substitute for the project's later full regression suite.

## Recovery

If the bridge is offline, the expected behavior is durable relay queueing plus delivery after restart. If the server does not provide durable queueing, report that passive wake-up is unavailable rather than claiming success. If the bound task is unavailable, pause delivery or retain messages according to the relay's documented retry policy; do not retarget them automatically.

If the pair response is lost after a Token was consumed, run `agentchat-bridge pair --recover`. For a genuinely lost credential, first revoke the old member/session at the server, run `agentchat-bridge revoke-local`, pair again, rebind explicitly, and verify the old credential is rejected. For a changed task/thread, reinstall the service with the explicit new binding.
