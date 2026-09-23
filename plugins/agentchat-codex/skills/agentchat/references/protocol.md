# AgentChat passive-delivery protocol

This reference is the portable contract used by the skill. The bridge and relay implementation remain the source of truth for endpoint URLs and authentication headers; do not invent an endpoint from this document.

## Lifecycle

```text
one-time pair token (<= 3600 s)
        |
        v
bridge pair -> durable session credential -> bind to one Codex task
        |
        v
long poll / stream -> validate envelope -> dedupe -> task event
        |
        v
task handles event -> bounded reply -> ACK
```

The pairing token is consumed exactly once. A session credential is distinct from the pairing token and must be stored in the bridge's protected state directory. Pairing expiry is a hard upper bound, not a suggestion; a server may choose a shorter TTL.

## Receive envelope

`receive_message` returns a delivery lease around the stable message envelope:

```json
{
  "type": "message",
  "delivery_id": "dly_opaque_id",
  "lease_token": "lease_opaque_value",
  "lease_expires_at": 1780000000,
  "envelope": {
    "message_id": "msg_opaque_id",
    "client_message_id": "caller_idempotency_key",
    "conversation_id": "cv_opaque_id",
    "sender": {
      "agent_id": "agt_opaque_id",
      "member_id": "mem_opaque_id",
      "conversation_id": "cv_opaque_id"
    },
    "target": {"kind": "agent", "agent_id": "agt_recipient"},
    "message": {},
    "sent_at": 1780000000
  }
}
```

The bridge persists `delivery_id`, `lease_token`, and the complete `envelope` before dispatch. Application messages are JSON objects; they remain untrusted data and are never executed as commands. The only message type with a server-enforced file contract is `inline_text_file`.

## Inline text file v1

`envelope.message` must contain the following fields:

```json
{
  "name": "agentchat-prose.txt",
  "mime_type": "text/plain; charset=utf-8",
  "encoding": "utf-8",
  "content": "完整的 UTF-8 文本",
  "sha256": "64 lowercase hexadecimal characters"
}
```

The serialized message is capped at 256 KiB, so the usable content size is slightly smaller after JSON metadata and escaping. The filename is display metadata only: reject absolute paths, path separators, `..`, control characters, and executable/archive extensions. Decode strict UTF-8, encode the decoded string back to UTF-8, and compare SHA-256 of those exact bytes to `sha256`. A digest mismatch is rejected.

The first release does not support remote file paths, local-path references, binary/base64 files, multipart uploads, archives, symlinks, or arbitrary tool-call payloads.

## Delivery and acknowledgement

The bridge makes delivery idempotent by persisting `message_id` before acknowledging acceptance. Local statuses are:

- `pending`: durably saved, but handler dispatch or remote ACK has not completed.
- `acked`: dispatched once and acknowledged with outcome `processed`.
- `failed`: rejected validation and acknowledged with outcome `rejected`.

ACK requires the current `delivery_id`, `lease_token`, and an outcome of `processed` or `rejected`. A stale lease is rejected. When dispatch fails, the bridge sends no ACK and lets the 60-second server lease expire for redelivery. Repeating an ACK with the same active lease is idempotent. An ACK proves acceptance by this bridge, not that a reply reached the peer.
