from __future__ import annotations

import argparse
import dataclasses
import getpass
import json
import shlex
import sys
from pathlib import Path

from .client import AgentChatClient
from .config import BridgeConfig
from .daemon import BridgeDaemon, Handler
from .messages import decode_message_json, inline_text_file
from .service import LaunchAgentService, service_fingerprint
from .store import InboxStore


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
    run = sub.add_parser("run", help="run the passive long-poll daemon")
    run.add_argument("--poll-timeout-ms", type=int)
    run.add_argument("--handler-command", help="fixed local command; message is JSON on stdin")
    run.add_argument("--codex-thread")
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
    service = sub.add_parser("service", help="manage the macOS background bridge")
    service_sub = service.add_subparsers(dest="service_command", required=True)
    service_install = service_sub.add_parser("install", help="install and start the user LaunchAgent")
    service_install.add_argument("--codex-thread", required=True, help="bound Codex task UUID or exact task name")
    service_sub.add_parser("status")
    service_sub.add_parser("uninstall")
    args = parser.parse_args(argv)
    config = _config(args)
    client = AgentChatClient(config)

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
            _json(result)
        finally:
            inbox.close()
        return 0
    if args.command == "inbox":
        inbox_store = InboxStore(config.inbox_path)
        try:
            if args.inbox_command == "list":
                _json([dataclasses.asdict(item) for item in inbox_store.list(limit=args.limit)])
            else:
                item = inbox_store.get(args.delivery_id)
                if item is None:
                    print(f"delivery not found: {args.delivery_id}", file=sys.stderr)
                    return 1
                _json(dataclasses.asdict(item))
        finally:
            inbox_store.close()
        return 0
    if args.command == "revoke-local":
        client.revoke_local()
        _json({"revoked_local": True})
        return 0
    if args.command == "leave":
        result = client.leave()
        client.revoke_local()
        _json({"left": True, "remote": result, "credentials_removed": True})
        return 0
    if args.command == "service":
        manager = LaunchAgentService(config)
        if args.service_command == "install":
            status = manager.install(args.codex_thread)
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
        handler = Handler(command=command, codex_thread=args.codex_thread or config.codex_thread, inbox_path=config.inbox_path)
        inbox = InboxStore(config.inbox_path)
        daemon = BridgeDaemon(client, inbox, handler)
        try:
            if args.once:
                daemon.process_once(timeout_ms=args.poll_timeout_ms)
            else:
                daemon.run_forever(timeout_ms=args.poll_timeout_ms)
        finally:
            inbox.close()
        return 0
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
