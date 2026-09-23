from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass, field
from typing import Any

from .config import BridgeConfig
from .errors import AuthenticationError, ProtocolError
from .store import CredentialStore
from .transport import JsonRpcTransport


def _wire(prefix: str, ident_prefix: str) -> tuple[str, str]:
    ident = ident_prefix + secrets.token_hex(16)
    raw = secrets.token_bytes(32)
    return f"{prefix}.{ident}.{base64.urlsafe_b64encode(raw).rstrip(b'=').decode()}", ident


@dataclass
class Credentials:
    access_token: str
    refresh_token: str
    instance_id: str
    agent_id: str | None = None
    member_id: str | None = None
    credential_id: str | None = None
    conversation_id: str | None = None
    endpoint: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    state: str = "active"
    pending: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Credentials":
        state = value.get("state", "active")
        if state not in {"active", "pending"}:
            raise ValueError("credentials file has invalid state")
        if not isinstance(value.get("instance_id"), str) or not value["instance_id"]:
            raise ValueError("credentials file is missing instance_id")
        pending = dict(value.get("pending") or {})
        if state == "pending":
            for key in ("recovery_token", "refresh_token", "pairing_id", "credential_id"):
                if not isinstance(pending.get(key), str) or not pending[key]:
                    raise ValueError(f"pending credentials are missing {key}")
            return cls(access_token="", refresh_token="", instance_id=value["instance_id"], endpoint=value.get("endpoint"), extra=dict(value.get("extra") or {}), state=state, pending=pending)
        required = ("access_token", "refresh_token")
        if any(not isinstance(value.get(key), str) or not value[key] for key in required):
            raise ValueError("credentials file is incomplete")
        return cls(access_token=value["access_token"], refresh_token=value["refresh_token"], instance_id=value["instance_id"], agent_id=value.get("agent_id"), member_id=value.get("member_id"), credential_id=value.get("credential_id"), conversation_id=value.get("conversation_id"), endpoint=value.get("endpoint"), extra=dict(value.get("extra") or {}), state=state, pending=pending)

    def to_dict(self) -> dict[str, Any]:
        if self.state == "pending":
            result = {"state": "pending", "instance_id": self.instance_id, "endpoint": self.endpoint, "pending": self.pending}
            if self.extra:
                result["extra"] = self.extra
            return result
        result = {"state": "active", "access_token": self.access_token, "refresh_token": self.refresh_token, "instance_id": self.instance_id}
        for key in ("agent_id", "member_id", "credential_id", "conversation_id", "endpoint"):
            value = getattr(self, key)
            if value:
                result[key] = value
        if self.extra:
            result["extra"] = self.extra
        return result


class AgentChatClient:
    def __init__(self, config: BridgeConfig):
        self.config = config
        self.credential_store = CredentialStore(config.credentials_path)
        self.transport = JsonRpcTransport(config.endpoint, timeout=config.request_timeout, retries=config.retry_count, backoff=config.retry_backoff)
        self.credentials: Credentials | None = self._load_credentials()

    def _load_credentials(self) -> Credentials | None:
        raw = self.credential_store.load()
        return Credentials.from_dict(raw) if raw else None

    def _save(self) -> None:
        if self.credentials is None:
            return
        self.credential_store.save(self.credentials.to_dict())

    def _require_active(self) -> Credentials:
        if self.credentials is None:
            raise ValueError("not paired")
        if self.credentials.state != "active":
            raise ValueError("pairing is incomplete; run `pair --recover` first")
        return self.credentials

    def pair(self, pair_token: str, *, display_name: str = "Codex Bridge", description: str = "local Codex passive bridge", instance_id: str | None = None) -> Credentials:
        if self.credentials is not None and self.credentials.state == "pending":
            raise ValueError("a pairing is pending; run `pair --recover` or `revoke-local` first")
        recovery, pairing_id = _wire("pr2", "pair_")
        refresh, credential_id = _wire("ar2", "cred_")
        # Persist every recovery secret before consuming the one-time token.
        # If the HTTP response is lost, the receipt can be looked up without
        # consuming the invitation again.
        self.credentials = Credentials(access_token="", refresh_token="", instance_id=instance_id or _instance_name(display_name), endpoint=self.config.endpoint, state="pending", pending={"recovery_token": recovery, "refresh_token": refresh, "pairing_id": pairing_id, "credential_id": credential_id, "display_name": display_name, "description": description})
        self._save()
        result = self.transport.call("pair_agent", {"pairing_id": pairing_id, "credential_id": credential_id, "recovery_digest": hashlib.sha256(recovery.encode()).hexdigest(), "refresh_digest": hashlib.sha256(refresh.encode()).hexdigest(), "profile": {"display_name": display_name, "description": description}}, bearer=pair_token)
        return self._finish_pairing(_data(result))

    def recover_pair(self) -> Credentials:
        if self.credentials is None or self.credentials.state != "pending":
            raise ValueError("no pending pairing to recover")
        pending = self.credentials.pending
        result = self.transport.call("pairing_result", bearer=pending["recovery_token"])
        return self._finish_pairing(_data(result))

    def _finish_pairing(self, data: dict[str, Any]) -> Credentials:
        if self.credentials is None or self.credentials.state != "pending":
            raise ValueError("no pending pairing")
        pending = self.credentials.pending
        identity = data.get("identity") if isinstance(data.get("identity"), dict) else {}
        credential = data.get("credential") if isinstance(data.get("credential"), dict) else {}
        self.credentials = Credentials(access_token="", refresh_token=pending["refresh_token"], instance_id=self.credentials.instance_id, agent_id=_text(identity.get("agent_id")), member_id=_text(identity.get("member_id")), credential_id=_text(credential.get("credential_id")) or _text(identity.get("credential_id")) or pending["credential_id"], conversation_id=_text(identity.get("conversation_id")) or _text(data.get("conversation_id")), endpoint=self.config.endpoint, extra={"pairing_id": pending["pairing_id"]})
        self._exchange_refresh(save=True)
        return self.credentials

    def _exchange_refresh(self, *, save: bool = True) -> Credentials:
        credentials = self._require_active()
        result = self.transport.call("exchange_credential", bearer=credentials.refresh_token)
        data = _data(result)
        access = _text(data.get("access_token"))
        refresh = _text(data.get("refresh_token")) or credentials.refresh_token
        if not access:
            raise ProtocolError("exchange_credential returned no access_token")
        credentials.access_token = access
        credentials.refresh_token = refresh
        identity = data.get("identity") if isinstance(data.get("identity"), dict) else {}
        credentials.agent_id = _text(identity.get("agent_id")) or credentials.agent_id
        credentials.member_id = _text(identity.get("member_id")) or credentials.member_id
        credentials.credential_id = _text(identity.get("credential_id")) or credentials.credential_id
        # Registration is required after every new access token, including a
        # refresh after a bridge restart.
        self.transport.call("register_agent", {"action": "upsert", "instance_id": credentials.instance_id}, bearer=credentials.access_token)
        if save:
            self._save()
        return credentials

    def ensure_registered(self) -> None:
        credentials = self._require_active()
        try:
            self.transport.call("register_agent", {"action": "upsert", "instance_id": credentials.instance_id}, bearer=credentials.access_token)
        except AuthenticationError:
            self._exchange_refresh()

    def _call(self, method: str, arguments: dict[str, Any] | None = None) -> Any:
        credentials = self._require_active()
        try:
            return self.transport.call(method, arguments, bearer=credentials.access_token)
        except AuthenticationError:
            self._exchange_refresh()
            return self.transport.call(method, arguments, bearer=credentials.access_token)

    def send(self, *, target: dict[str, Any], message: dict[str, Any], client_message_id: str | None = None) -> Any:
        import uuid

        payload = {"client_message_id": client_message_id or f"cm_{uuid.uuid4().hex}", "target": target, "message": message}
        return self._call("send_message", payload)

    def list_agents(self) -> Any:
        return self._call("list_agents", {})

    def receive(self, *, timeout_ms: int | None = None) -> Any:
        credentials = self._require_active()
        timeout = self.config.poll_timeout_ms if timeout_ms is None else min(25_000, max(1_000, int(timeout_ms)))
        return self._call("receive_message", {"instance_id": credentials.instance_id, "timeout_ms": timeout})

    def ack(self, delivery_id: str, lease_token: str, outcome: str) -> Any:
        if outcome not in {"processed", "rejected"}:
            raise ValueError("ACK outcome must be processed or rejected")
        return self._call("ack_message", {"delivery_id": delivery_id, "lease_token": lease_token, "outcome": outcome})

    def leave(self) -> Any:
        return self._call("leave_conversation", {})

    def status(self) -> dict[str, Any]:
        result: dict[str, Any] = {"paired": self.credentials is not None, "endpoint": self.config.endpoint, "credentials_path": str(self.config.credentials_path)}
        if self.credentials:
            result.update({"state": self.credentials.state, "instance_id": self.credentials.instance_id, "agent_id": self.credentials.agent_id, "member_id": self.credentials.member_id, "credential_id": self.credentials.credential_id, "conversation_id": self.credentials.conversation_id})
        return result

    def revoke_local(self) -> None:
        self.credentials = None
        self.credential_store.revoke_local()


def _data(value: Any) -> dict[str, Any]:
    if isinstance(value, dict) and isinstance(value.get("data"), dict):
        return value["data"]
    if isinstance(value, dict):
        return value
    raise ProtocolError("remote response is not an object")


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _instance_name(display_name: str) -> str:
    value = "".join(char.lower() if char.isalnum() else "-" for char in display_name).strip("-")
    value = value[:48] or "codex"
    return f"{value}-{secrets.token_hex(8)}"
