"""Claude Code local peer-protocol v1 adapter (version-sensitive).

Socket existence is documented by Anthropic. The wire frame is independently
documented by PeterSR/claude-code-socket-transport; it is NOT a stable public API.
Never use the target's authentication token or impersonate its child process.
"""
from __future__ import annotations

import json
import os
import socket
import stat
import uuid
from pathlib import Path

from .runtime import DispatchUncertain


def _private(path: Path, *, socket_file=False):
    info = path.lstat()
    expected = stat.S_ISSOCK(info.st_mode) if socket_file else stat.S_ISREG(info.st_mode)
    if not expected or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError("Claude registry/socket must be owned by this user and not writable by others")
    return info


def sessions(registry: Path | None = None):
    registry = registry or Path.home() / ".claude" / "sessions"
    result = []
    if not registry.exists():
        return result
    for index, path in enumerate(registry.glob("*.json")):
        if index >= 1024:
            raise ValueError("Claude session registry exceeds scan limit")
        try:
            _private(path)
            with path.open("rb") as handle:
                raw = handle.read(65537)
            if len(raw) > 65536:
                continue
            data = json.loads(raw)
            pid = data.get("pid")
            if not isinstance(pid, int) or pid <= 1:
                continue
            os.kill(pid, 0)
            target = data.get("messagingSocketPath")
            ready = False
            if isinstance(target, str) and Path(target).is_absolute():
                _private(Path(target), socket_file=True)
                parent = Path(target).parent.stat()
                ready = parent.st_uid == os.getuid() and not parent.st_mode & 0o077
            result.append({key: data.get(key) for key in
                ("sessionId", "pid", "name", "cwd", "version", "peerProtocol", "messagingSocketPath")}
                | {"socket_available": ready})
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return result


class ClaudeAdapter:
    def __init__(self, session_id: str, registry: Path | None = None):
        self.session_id = str(uuid.UUID(session_id))
        self.registry = registry

    def preflight(self):
        candidates = [s for s in sessions(self.registry) if s["sessionId"] == self.session_id and s["socket_available"]]
        if len(candidates) != 1:
            raise RuntimeError("Claude target is offline, has no inbox, or is ambiguous; binding was not changed")
        target = candidates[0]
        if target["peerProtocol"] != 1:
            raise RuntimeError("unsupported Claude peer protocol; verify the installed version")
        return target

    def send(self, prompt: str, message_id: str):
        target = self.preflight()
        identity = str(uuid.uuid5(uuid.NAMESPACE_URL, f"agentchat:{self.session_id}:{message_id}"))
        # Fixed attribution, no remote body, no permission-class assertion.
        content = '<cross-session-message from-name="AgentChat">\n' + prompt.replace("</cross-session-message", "<\\/cross-session-message") + '\n</cross-session-message>'
        frame = {"msgV": 1, "msg_id": identity, "uuid": identity, "type": "user",
                 "message": {"role": "user", "content": content}, "priority": "later",
                 "session_id": self.session_id}
        raw = json.dumps(frame, ensure_ascii=False).encode() + b"\n"
        if len(raw) > 16384:
            raise ValueError("Claude inbox pointer exceeds 16 KiB")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.settimeout(5)
            peer.connect(target["messagingSocketPath"])
            try:
                peer.sendall(raw)
                peer.shutdown(socket.SHUT_WR)
                # No synchronous semantic ACK exists. EOF only proves that the
                # peer closed the connection, not model receipt or completion.
                response = peer.recv(1025)
                if response:
                    raise DispatchUncertain("unexpected Claude response; verify before resubmitting")
            except OSError as exc:
                raise DispatchUncertain("Claude socket write outcome unknown; inspect target") from exc
        return {"status": "submitted_unconfirmed", "host_message_id": identity,
                "session_id": self.session_id, "claude_version": target["version"]}
