from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _default_state_dir() -> Path:
    override = os.environ.get("AGENTCHAT_STATE_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "agentchat-codex"


@dataclass(frozen=True)
class BridgeConfig:
    endpoint: str = "https://note.chinzy.com/relay/mcp"
    state_dir: Path = _default_state_dir()
    request_timeout: float = 35.0
    poll_timeout_ms: int = 25_000
    retry_count: int = 3
    retry_backoff: float = 0.5
    codex_thread: str | None = None
    codex_command: tuple[str, ...] = ("codex", "queue")
    handler_command: tuple[str, ...] | None = None

    @property
    def credentials_path(self) -> Path:
        return self.state_dir / "credentials.json"

    @property
    def inbox_path(self) -> Path:
        return self.state_dir / "inbox.sqlite3"

    @classmethod
    def from_env(cls, **overrides: object) -> "BridgeConfig":
        values: dict[str, object] = {
            "endpoint": os.environ.get("AGENTCHAT_ENDPOINT", cls.endpoint),
            "state_dir": Path(os.environ.get("AGENTCHAT_STATE_DIR", str(_default_state_dir()))).expanduser(),
            "request_timeout": float(os.environ.get("AGENTCHAT_REQUEST_TIMEOUT", cls.request_timeout)),
            "poll_timeout_ms": min(25_000, max(1_000, int(os.environ.get("AGENTCHAT_POLL_TIMEOUT_MS", cls.poll_timeout_ms)))),
            "retry_count": max(0, int(os.environ.get("AGENTCHAT_RETRY_COUNT", cls.retry_count))),
            "retry_backoff": float(os.environ.get("AGENTCHAT_RETRY_BACKOFF", cls.retry_backoff)),
            "codex_thread": os.environ.get("AGENTCHAT_CODEX_THREAD"),
        }
        values.update(overrides)
        values["poll_timeout_ms"] = min(25_000, max(1_000, int(values["poll_timeout_ms"])))
        values["retry_count"] = max(0, int(values["retry_count"]))
        values["retry_backoff"] = max(0.0, float(values["retry_backoff"]))
        values["request_timeout"] = max(1.0, float(values["request_timeout"]))
        state_dir = Path(values["state_dir"]).expanduser()
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            state_dir.chmod(0o700)
        except OSError:
            pass
        return cls(**values)  # type: ignore[arg-type]
