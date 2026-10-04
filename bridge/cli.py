from __future__ import annotations

import argparse
import dataclasses
import getpass
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import shlex
import sys
from pathlib import Path

from .client import AgentChatClient
from .config import BridgeConfig
from .daemon import BridgeDaemon, Handler
from .messages import decode_message_json, inline_text_file
from .service import LaunchAgentService, service_fingerprint
from .store import InboxStore, CredentialStore
from .claude import sessions
from .runtime import profile_lock, stop_signals, StopBridge


def _config(args: argparse.Namespace) -> BridgeConfig:
    overrides: dict[str, object] = {}
    if args.endpoint:
        overrides["endpoint"] = args.endpoint
    if args.state_dir:
        overrides["state_dir"] = Path(args.state_dir).expanduser()
    return BridgeConfig.from_env(**overrides)


def _json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentchat-bridge")
    parser.add_argument("--endpoint", default=None)
    parser.add_argument("--state-dir", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    pair = sub.add_parser("pair", help="consume a one-time pair token")
    pair.add_argument("--token")
    pair.add_argument("--token-file", type=Path)
    pair.add_argument("--display-name", default="Codex Bridge")
    pair.add_argument("--instance-id")
    pair.add_argument("--recover", action="store_true", help="recover the persisted pairing after a lost response")

    sub.add_parser("status")
    doctor = sub.add_parser("doctor", help="read-only adapter and local resource checks")
    doctor.add_argument("--claude", action="store_true", help="list this user's live Claude registry entries")
    run = sub.add_parser("run", help="run the passive long-poll daemon")
    run.add_argument("--poll-timeout-ms", type=int)
    run_target = run.add_mutually_exclusive_group()
    run_target.add_argument("--handler-command", help="fixed local command; message is JSON on stdin")
    run_target.add_argument("--codex-thread")
    run_target.add_argument("--claude-session", help="exact Claude Code session UUID")
    run.add_argument("--once", action="store_true")

    send = sub.add_parser("send")
    target = send.add_mutually_exclusive_group(required=True)
    target.add_argument("--target-agent-id")
    target.add_argument("--target-member-id")
    target.add_argument("--broadcast", action="store_true")
    source = send.add_mutually_exclusive_group(required=True)
    source.add_argument("--text")
    source.add_argument("--message-json")
    source.add_argument("--file", type=Path)
    send.add_argument("--client-message-id")
    send.add_argument("--reply-to", help="original AgentChat message ID")
    sub.add_parser("agents", help="list active members in this conversation")

    receive = sub.add_parser("receive")
    receive.add_argument("--timeout-ms", type=int)
    ack = sub.add_parser("ack")
    ack.add_argument("--delivery-id", required=True)
    ack.add_argument("--lease-token", required=True)
    ack.add_argument("--outcome", choices=("processed", "rejected"), default="processed")
    sub.add_parser("leave", help="leave the conversation, revoke this member, and remove local credentials")
    sub.add_parser("revoke-local")
    inbox = sub.add_parser("inbox", help="inspect durable locally received messages")
    inbox_sub = inbox.add_subparsers(dest="inbox_command", required=True)
    inbox_list = inbox_sub.add_parser("list")
    inbox_list.add_argument("--limit", type=int, default=100)
    inbox_show = inbox_sub.add_parser("show")
    inbox_show.add_argument("--delivery-id", required=True)
    inbox_resolve = inbox_sub.add_parser("resolve", help="resolve an uncertain submission after checking the target")
    inbox_resolve.add_argument("--delivery-id", required=True)
    inbox_resolve.add_argument("--action", required=True, choices=("accepted", "retry"))
    service = sub.add_parser("service", help="manage the macOS background bridge")
    service_sub = service.add_subparsers(dest="service_command", required=True)
    service_install = service_sub.add_parser("install", help="install and start the user LaunchAgent")
    install_target = service_install.add_mutually_exclusive_group(required=True)
    install_target.add_argument("--codex-thread", help="bound Codex task UUID")
    install_target.add_argument("--claude-session", help="bound Claude Code session UUID")
    service_sub.add_parser("status")
    service_sub.add_parser("uninstall")
    args = parser.parse_args(argv)
    config = _config(args)
    client = AgentChatClient(config)

    if args.command == "doctor":
        binding = CredentialStore(config.state_dir / "binding.json").load()
        result = {"profile": str(config.state_dir), "binding": binding, "paired": client.credentials is not None,
                  "limits": {"http_response_bytes": 4194304, "handler_output_bytes": 65536,
                             "inbox_rows": 10000, "inbox_payload_bytes": 134217728,
                             "log_bytes": 1048576, "log_backups": 2},
                  "disk_bytes": sum(p.stat().st_size for p in config.state_dir.iterdir() if p.is_file())}
        if args.claude:
            result["claude_sessions"] = sessions()
        _json(result)
        return 0

    if args.command == "pair":
        if args.recover:
            credentials = client.recover_pair()
            _json({"paired": True, "recovered": True, "instance_id": credentials.instance_id, "agent_id": credentials.agent_id, "credential_id": credentials.credential_id, "credentials_path": str(config.credentials_path)})
            return 0
        token = args.token
        if args.token_file:
            token = args.token_file.read_text(encoding="utf-8").strip()
        if not token:
            token = getpass.getpass("Pair token (input hidden): ")
        credentials = client.pair(token, display_name=args.display_name, instance_id=args.instance_id)
        _json({"paired": True, "instance_id": credentials.instance_id, "agent_id": credentials.agent_id, "credential_id": credentials.credential_id, "credentials_path": str(config.credentials_path)})
        return 0
    if args.command == "status":
        inbox = InboxStore(config.inbox_path)
        try:
            result = client.status()
            result["inbox"] = inbox.counts()
            result["binding"] = CredentialStore(config.state_dir / "binding.json").load()
            _json(result)
        finally:
            inbox.close()
        return 0
    if args.command == "inbox":
        inbox_store = InboxStore(config.inbox_path)
        try:
            if args.inbox_command == "list":
                _json([dataclasses.asdict(item) for item in inbox_store.list(limit=args.limit)])
            elif args.inbox_command == "resolve":
                with profile_lock(config.state_dir):
                    inbox_store.resolve(args.delivery_id, retry=args.action == "retry")
                _json({"resolved": args.action, "delivery_id": args.delivery_id})
            else:
                item = inbox_store.get(args.delivery_id)
                if item is None:
                    print(f"delivery not found: {args.delivery_id}", file=sys.stderr)
                    return 1
                value = dataclasses.asdict(item)
                value.pop("lease_token", None)
                value["payload"].pop("lease_token", None)
                _json(value)
        finally:
            inbox_store.close()
        return 0
    if args.command == "revoke-local":
        with profile_lock(config.state_dir):
            client.revoke_local()
        _json({"revoked_local": True})
        return 0
    if args.command == "leave":
        with profile_lock(config.state_dir):
            result = client.leave()
            client.revoke_local()
        _json({"left": True, "remote": result, "credentials_removed": True})
        return 0
    if args.command == "service":
        manager = LaunchAgentService(config)
        if args.service_command == "install":
            client._require_active()
            status = manager.install(args.codex_thread, claude_session=args.claude_session)
        elif args.service_command == "uninstall":
            status = manager.uninstall()
        else:
            status = manager.status()
        result = dataclasses.asdict(status)
        result["profile"] = service_fingerprint(config)
        _json(result)
        return 0
    if args.command == "send":
        if args.text is not None:
            message = {"text": args.text}
        elif args.file:
            message = inline_text_file(args.file)
        else:
            message = decode_message_json(args.message_json)
        if args.reply_to:
            message["reply_to"] = args.reply_to
        if args.target_agent_id:
            target_value = {"kind": "agent", "agent_id": args.target_agent_id}
        elif args.target_member_id:
            target_value = {"kind": "member", "member_id": args.target_member_id}
        else:
            target_value = {"kind": "broadcast"}
        _json(client.send(target=target_value, message=message, client_message_id=args.client_message_id))
        return 0
    if args.command == "agents":
        _json(client.list_agents())
        return 0
    if args.command == "receive":
        _json(client.receive(timeout_ms=args.timeout_ms))
        return 0
    if args.command == "ack":
        _json(client.ack(args.delivery_id, args.lease_token, args.outcome))
        return 0
    if args.command == "run":
        command = tuple(shlex.split(args.handler_command)) if args.handler_command else None
        codex_thread = args.codex_thread
        claude_session = args.claude_session
        if not any((command, codex_thread, claude_session)):
            binding = CredentialStore(config.state_dir / "binding.json").load() or {}
            codex_thread = binding.get("target") if binding.get("adapter") == "codex" else config.codex_thread
            claude_session = binding.get("target") if binding.get("adapter") == "claude" else None
        if not any((command, codex_thread, claude_session)):
            parser.error("run requires a bound target; use --codex-thread or --claude-session")
        handler = Handler(command=command, codex_thread=codex_thread, claude_session=claude_session, inbox_path=config.inbox_path)
        previous_umask = os.umask(0o077)
        logger = logging.getLogger("agentchat")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        log_handler = RotatingFileHandler(config.state_dir / "bridge.log", maxBytes=1048576, backupCount=2)
        log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(log_handler)
        try:
            with profile_lock(config.state_dir), stop_signals():
                inbox = InboxStore(config.inbox_path)
                try:
                    daemon = BridgeDaemon(client, inbox, handler)
                    if args.once:
                        daemon.process_once(timeout_ms=args.poll_timeout_ms)
                    else:
                        daemon.run_forever(timeout_ms=args.poll_timeout_ms)
                finally:
                    inbox.close()
                    try:
                        client.release_registration()
                    except Exception as exc:
                        logger.warning("presence release failed: %s", type(exc).__name__)
        except StopBridge:
            logger.info("bridge stopped; handles released")
        finally:
            logger.removeHandler(log_handler)
            log_handler.close()
            os.umask(previous_umask)
        return 0
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
