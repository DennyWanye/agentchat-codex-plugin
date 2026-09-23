from __future__ import annotations

import hashlib
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import BridgeConfig


@dataclass(frozen=True)
class ServiceStatus:
    installed: bool
    loaded: bool
    label: str
    plist_path: str


class LaunchAgentService:
    """Install one user-scoped macOS LaunchAgent for the passive bridge."""

    label = "com.dennywanye.agentchat-codex-bridge"

    def __init__(self, config: BridgeConfig):
        if sys.platform != "darwin":
            raise RuntimeError("background service installation currently supports macOS only")
        self.config = config
        self.uid = os.getuid()
        self.domain = f"gui/{self.uid}"
        launch_agents = Path(os.environ.get("AGENTCHAT_LAUNCH_AGENTS_DIR", str(Path.home() / "Library" / "LaunchAgents"))).expanduser()
        self.plist_path = launch_agents / f"{self.label}.plist"

    def install(self, codex_thread: str) -> ServiceStatus:
        if not codex_thread or any(ch.isspace() for ch in codex_thread):
            raise ValueError("--codex-thread must be a non-empty task id or exact task name")
        codex = shutil.which("codex")
        if not codex:
            raise RuntimeError("codex executable was not found in PATH")
        self.config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.config.state_dir.chmod(0o700)
        self.plist_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "Label": self.label,
            "ProgramArguments": [
                sys.executable,
                "-m",
                "bridge",
                "--endpoint",
                self.config.endpoint,
                "--state-dir",
                str(self.config.state_dir),
                "run",
                "--codex-thread",
                codex_thread,
            ],
            "EnvironmentVariables": {
                "PATH": self._service_path(codex),
                "PYTHONUNBUFFERED": "1",
            },
            "RunAtLoad": True,
            "KeepAlive": True,
            "ProcessType": "Background",
            "ThrottleInterval": 10,
            "Umask": 63,
            "StandardOutPath": str(self.config.state_dir / "bridge.stdout.log"),
            "StandardErrorPath": str(self.config.state_dir / "bridge.stderr.log"),
        }
        self._write_plist(payload)
        self._run(["launchctl", "bootout", self.domain, str(self.plist_path)], allow_failure=True)
        self._run(["launchctl", "bootstrap", self.domain, str(self.plist_path)])
        self._run(["launchctl", "kickstart", "-k", f"{self.domain}/{self.label}"])
        return self.status()

    def uninstall(self) -> ServiceStatus:
        self._run(["launchctl", "bootout", self.domain, str(self.plist_path)], allow_failure=True)
        try:
            self.plist_path.unlink()
        except FileNotFoundError:
            pass
        return self.status()

    def status(self) -> ServiceStatus:
        completed = self._run(
            ["launchctl", "print", f"{self.domain}/{self.label}"],
            allow_failure=True,
        )
        return ServiceStatus(
            installed=self.plist_path.is_file(),
            loaded=completed.returncode == 0,
            label=self.label,
            plist_path=str(self.plist_path),
        )

    def _write_plist(self, payload: dict[str, object]) -> None:
        fd, temporary = tempfile.mkstemp(prefix=f".{self.label}.", dir=self.plist_path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                plistlib.dump(payload, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.plist_path)
            self.plist_path.chmod(0o600)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    @staticmethod
    def _service_path(codex: str) -> str:
        entries = [str(Path(codex).parent), str(Path(sys.executable).parent), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
        unique: list[str] = []
        for entry in entries:
            if entry not in unique:
                unique.append(entry)
        return ":".join(unique)

    @staticmethod
    def _run(command: list[str], *, allow_failure: bool = False) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(command, text=True, capture_output=True, check=False, shell=False)
        if completed.returncode and not allow_failure:
            detail = (completed.stderr or completed.stdout).strip()[-500:]
            raise RuntimeError(f"service command failed ({completed.returncode}): {detail}")
        return completed


def service_fingerprint(config: BridgeConfig) -> str:
    """Stable non-secret identifier useful in diagnostics."""

    value = f"{config.endpoint}\0{config.state_dir}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()[:12]
