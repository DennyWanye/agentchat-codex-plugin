from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import sys
from pathlib import Path

import pytest

from bridge.client import AgentChatClient, Credentials, _instance_name
from bridge.cli import main as cli_main
from bridge.config import BridgeConfig
from bridge.daemon import BridgeDaemon, Handler
from bridge.errors import MessageValidationError, RemoteError
from bridge.messages import inline_text_file, validate_inline_text_file
from bridge.store import CredentialStore, InboxStore
from bridge.service import LaunchAgentService
from bridge.transport import JsonRpcTransport


class FakeClient:
    def __init__(self, response, *, repeat=False):
        self.response = response
        self.repeat = repeat
        self.receives = 0
        self.acks = []

    def ensure_registered(self):
        pass

    def receive(self, *, timeout_ms=None):
        self.receives += 1
        return self.response if self.repeat or self.receives == 1 else {}

    def ack(self, delivery_id, lease_token, outcome):
        self.acks.append((delivery_id, outcome))
        return {"ok": True}


def test_credentials_are_atomic_and_0600(tmp_path: Path):
    path = tmp_path / "credentials.json"
    CredentialStore(path).save({"access_token": "a"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert CredentialStore(path).load()["access_token"] == "a"


def test_inline_file_is_utf8_bounded_and_hashed(tmp_path: Path):
    path = tmp_path / "散文.txt"
    path.write_text("窗外的雨停了。", encoding="utf-8")
    value = inline_text_file(path)
    assert value["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    validate_inline_text_file(value)
    value["sha256"] = "0" * 64
    with pytest.raises(MessageValidationError):
        validate_inline_text_file(value)


def test_inline_file_rejects_over_256_kib():
    raw = {"type": "inline_text_file", "name": "a.txt", "encoding": "utf-8", "content": "x" * (256 * 1024 + 1), "sha256": ""}
    with pytest.raises(MessageValidationError, match="256 KiB"):
        validate_inline_text_file(raw)


def test_inline_file_rejects_wrong_mime_and_unsafe_extension():
    content = "safe text"
    digest = hashlib.sha256(content.encode()).hexdigest()
    with pytest.raises(MessageValidationError, match="mime_type"):
        validate_inline_text_file({"type": "inline_text_file", "name": "a.txt", "mime_type": "text/html", "encoding": "utf-8", "content": content, "sha256": digest})
    with pytest.raises(MessageValidationError, match="blocked"):
        validate_inline_text_file({"type": "inline_text_file", "name": "run.sh", "mime_type": "text/plain", "encoding": "utf-8", "content": content, "sha256": digest})


def test_daemon_persists_and_acks_once(tmp_path: Path):
    content = "一段来自远方的散文。"
    response = {"type": "message", "delivery_id": "del_1", "lease_token": "lease_1", "envelope": {"message_id": "msg_1", "sender": {"agent_id": "agent-b"}, "message": {"type": "inline_text_file", "name": "prose.txt", "mime_type": "text/plain; charset=utf-8", "encoding": "utf-8", "content": content, "sha256": hashlib.sha256(content.encode()).hexdigest()}}}
    client = FakeClient(response, repeat=True)
    store = InboxStore(tmp_path / "inbox.sqlite3")
    seen = []
    handler = Handler(command=("python", "-c", "import sys; sys.stdin.read()"))
    original = handler.run
    handler.run = lambda envelope, message: seen.append(message) or {"status": "ok"}  # type: ignore[method-assign]
    daemon = BridgeDaemon(client, store, handler)
    first = daemon.process_once(timeout_ms=100)
    second = daemon.process_once(timeout_ms=100)
    assert first and first.status == "acked"
    assert second and second.status == "acked"
    assert len(seen) == 1
    assert client.acks == [("del_1", "processed"), ("del_1", "processed")]
    assert store.counts() == {"pending": 0, "acked": 1, "failed": 0}
    store.close()


def test_invalid_message_is_rejected_and_acked(tmp_path: Path):
    response = {"type": "message", "delivery_id": "del_bad", "lease_token": "lease_bad", "envelope": {"message_id": "msg_bad", "message": {"type": "inline_text_file", "name": "bad.txt", "mime_type": "text/plain", "encoding": "utf-8", "content": "bad", "sha256": "0" * 64}}}
    client = FakeClient(response)
    store = InboxStore(tmp_path / "inbox.sqlite3")
    daemon = BridgeDaemon(client, store, Handler())
    result = daemon.process_once(timeout_ms=100)
    assert result and result.status == "failed"
    assert client.acks == [("del_bad", "rejected")]
    store.close()


def test_handler_failure_leaves_pending_without_ack(tmp_path: Path):
    response = {"type": "message", "delivery_id": "del_retry", "lease_token": "lease_retry", "envelope": {"message_id": "msg_retry", "message": {"text": "retry"}}}
    client = FakeClient(response)
    store = InboxStore(tmp_path / "inbox.sqlite3")
    handler = Handler(command=("python", "-c", "raise SystemExit(7)"))
    daemon = BridgeDaemon(client, store, handler)
    with pytest.raises(RuntimeError):
        daemon.process_once(timeout_ms=100)
    assert client.acks == []
    assert store.get("del_retry").status == "pending"
    store.close()


def test_lost_ack_redelivery_does_not_dispatch_handler_twice(tmp_path: Path):
    response = {"type": "message", "delivery_id": "del_ack_lost", "lease_token": "lease_current", "envelope": {"message_id": "msg_ack_lost", "sender": {"agent_id": "agent-b"}, "message": {"text": "once"}}}

    class LostAckClient(FakeClient):
        def __init__(self):
            super().__init__(response, repeat=True)
            self.ack_attempts = 0

        def ack(self, delivery_id, lease_token, outcome):
            self.ack_attempts += 1
            if self.ack_attempts == 1:
                raise RemoteError("ACK response lost")
            return {"ok": True}

    client = LostAckClient()
    store = InboxStore(tmp_path / "inbox.sqlite3")
    seen = []
    handler = Handler()
    handler.run = lambda envelope, message: seen.append(message) or {"status": "queued"}  # type: ignore[method-assign]
    daemon = BridgeDaemon(client, store, handler)
    with pytest.raises(RemoteError, match="ACK response lost"):
        daemon.process_once(timeout_ms=1_000)
    pending = store.get("del_ack_lost")
    assert pending and pending.status == "pending" and pending.outcome == {"status": "dispatched"}
    completed = daemon.process_once(timeout_ms=1_000)
    assert completed and completed.status == "acked"
    assert seen == [{"text": "once"}]
    assert client.ack_attempts == 2
    store.close()


def test_handler_does_not_interpolate_remote_message(tmp_path: Path):
    output = tmp_path / "output.json"
    handler = Handler(command=("python", "-c", "import sys; sys.stdin.read()"))
    result = handler.run({"delivery_id": "x"}, {"text": "$(touch SHOULD_NOT_EXIST); `bad`"})
    assert result["status"] == "handled"
    assert not (tmp_path / "SHOULD_NOT_EXIST").exists()


def test_client_send_contract_and_refresh_register(tmp_path: Path):
    config = BridgeConfig.from_env(state_dir=tmp_path, endpoint="https://example.invalid")
    client = AgentChatClient(config)
    client.credentials = Credentials("old", "refresh", "instance-a", agent_id="agent-a")
    calls = []

    class Transport:
        def call(self, method, arguments=None, *, bearer=None):
            calls.append((method, arguments, bearer))
            if method == "send_message":
                return {"ok": True}
            return {"data": {"access_token": "new", "refresh_token": "refresh2", "identity": {"agent_id": "agent-a"}}}

    client.transport = Transport()
    result = client.send(target={"kind": "agent", "agent_id": "agent-b"}, message={"text": "hi"}, client_message_id="cm_fixed")
    assert result == {"ok": True}
    assert calls[0][0] == "send_message"
    assert calls[0][1]["client_message_id"] == "cm_fixed"


def test_ack_contract_includes_current_lease(tmp_path: Path):
    config = BridgeConfig.from_env(state_dir=tmp_path)
    client = AgentChatClient(config)
    client.credentials = Credentials("access", "refresh", "instance")
    seen = []

    class Transport:
        def call(self, method, arguments=None, *, bearer=None):
            seen.append((method, arguments))
            return {"acked": True}

    client.transport = Transport()
    client.ack("delivery-1", "lease-1", "processed")
    assert seen == [("ack_message", {"delivery_id": "delivery-1", "lease_token": "lease-1", "outcome": "processed"})]


def test_codex_queue_gets_short_pointer_not_message_body(tmp_path: Path, monkeypatch):
    calls = []

    class Completed:
        returncode = 0
        stdout = "queued"
        stderr = ""

    monkeypatch.setattr("bridge.daemon.subprocess.run", lambda *args, **kwargs: calls.append((args, kwargs)) or Completed())
    handler = Handler(codex_thread="thread-1", inbox_path=tmp_path / "inbox.sqlite3")
    body = "x" * (256 * 1024)
    result = handler.run({"delivery_id": "del_42", "sender": {"agent_id": "agent-b"}}, {"type": "inline_text_file", "content": body})
    assert result["status"] == "queued"
    args, kwargs = calls[0]
    command = args[0]
    assert command[:4] == ["codex", "queue", "--thread", "thread-1"]
    assert "--message" in command
    prompt = command[command.index("--message") + 1]
    assert "del_42" in prompt and "agent-b" in prompt
    assert "Use $agentchat" in prompt
    assert "--target-agent-id agent-b" in prompt
    assert f"{sys.executable} -m bridge" in prompt
    assert body not in prompt
    assert kwargs["input"] is None


def test_default_transport_adds_system_ca_bundle(monkeypatch):
    class EmptyContext:
        def __init__(self):
            self.loaded = None

        def get_ca_certs(self):
            return [{"subject": "existing"}]

        def load_verify_locations(self, *, cafile):
            self.loaded = cafile

    context = EmptyContext()
    monkeypatch.setattr("bridge.transport.ssl.create_default_context", lambda: context)
    result = JsonRpcTransport._default_context()
    assert result is context
    assert result.loaded == "/etc/ssl/cert.pem"


def test_pair_persists_pending_then_recover(tmp_path: Path):
    config = BridgeConfig.from_env(state_dir=tmp_path, endpoint="https://example.invalid")
    first = AgentChatClient(config)
    calls = []

    class LostPairTransport:
        def call(self, method, arguments=None, *, bearer=None):
            calls.append((method, arguments, bearer))
            if method == "pair_agent":
                raise RemoteError("response lost")
            raise AssertionError(method)

    first.transport = LostPairTransport()
    with pytest.raises(RemoteError):
        first.pair("pt2.test", display_name="Recovery test")
    saved = json.loads(config.credentials_path.read_text(encoding="utf-8"))
    assert saved["state"] == "pending"
    assert saved["pending"]["recovery_token"].startswith("pr2.")

    recovered = AgentChatClient(config)

    class RecoveryTransport:
        def call(self, method, arguments=None, *, bearer=None):
            calls.append((method, arguments, bearer))
            if method == "pairing_result":
                return {"data": {"state": "paired", "identity": {"agent_id": "agent-a", "conversation_id": "cv-a"}, "credential": {"credential_id": "cred-a"}}}
            if method == "exchange_credential":
                return {"data": {"access_token": "access-a", "refresh_token": "refresh-a"}}
            if method == "register_agent":
                return {"ok": True}
            raise AssertionError(method)

    recovered.transport = RecoveryTransport()
    credentials = recovered.recover_pair()
    assert credentials.state == "active"
    assert credentials.agent_id == "agent-a"
    assert json.loads(config.credentials_path.read_text(encoding="utf-8"))["state"] == "active"
    assert calls[-3][0] == "pairing_result"


def test_pending_credentials_cannot_send(tmp_path: Path):
    config = BridgeConfig.from_env(state_dir=tmp_path)
    store = CredentialStore(config.credentials_path)
    store.save({"state": "pending", "instance_id": "i", "endpoint": config.endpoint, "pending": {"recovery_token": "pr2.x", "refresh_token": "ar2.x", "pairing_id": "p", "credential_id": "c"}})
    client = AgentChatClient(config)
    with pytest.raises(ValueError, match="pair --recover"):
        client.send(target={"kind": "agent", "agent_id": "a"}, message={"text": "x"})


def test_inbox_show_reads_persisted_envelope(tmp_path: Path, capsys):
    config = BridgeConfig.from_env(state_dir=tmp_path)
    store = InboxStore(config.inbox_path)
    store.put("d1", "m1", "l1", {"message_id": "m1", "message": {"text": "hello"}})
    store.close()
    assert cli_main(["--state-dir", str(tmp_path), "inbox", "show", "--delivery-id", "d1"]) == 0
    output = capsys.readouterr().out
    assert '"delivery_id": "d1"' in output
    assert '"hello"' in output


def test_config_caps_poll_timeout(tmp_path: Path):
    config = BridgeConfig.from_env(state_dir=tmp_path, poll_timeout_ms=25_000)
    assert config.poll_timeout_ms == 25_000
    assert BridgeConfig.from_env(state_dir=tmp_path, poll_timeout_ms=-1).poll_timeout_ms == 1_000


def test_default_instance_ids_do_not_collide():
    first = _instance_name("Codex Bridge")
    second = _instance_name("Codex Bridge")
    assert first != second
    assert first.startswith("codex-bridge-")


def test_launch_agent_install_uses_bound_thread_and_protected_plist(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("AGENTCHAT_LAUNCH_AGENTS_DIR", str(tmp_path / "LaunchAgents"))
    monkeypatch.setattr("bridge.service.shutil.which", lambda name: "/opt/codex/bin/codex" if name == "codex" else None)
    calls = []

    class Completed:
        def __init__(self, returncode=0):
            self.returncode = returncode
            self.stdout = ""
            self.stderr = ""

    def fake_run(command, **kwargs):
        calls.append(command)
        return Completed(0 if "print" not in command else 0)

    monkeypatch.setattr("bridge.service.subprocess.run", fake_run)
    config = BridgeConfig.from_env(state_dir=tmp_path / "state", endpoint="https://relay.example/mcp")
    service = LaunchAgentService(config)
    status = service.install("thread-123")
    assert status.installed and status.loaded
    assert stat.S_IMODE(service.plist_path.stat().st_mode) == 0o600
    import plistlib

    payload = plistlib.loads(service.plist_path.read_bytes())
    args = payload["ProgramArguments"]
    assert args[-2:] == ["--codex-thread", "thread-123"]
    assert payload["EnvironmentVariables"]["PATH"].startswith("/opt/codex/bin:")
    assert any("bootstrap" in command for command in calls)
