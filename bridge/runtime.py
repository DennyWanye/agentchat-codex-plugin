"""Bounded local process execution and exclusive profile ownership."""
from __future__ import annotations

import fcntl
import os
import selectors
import signal
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path


class DispatchUncertain(RuntimeError):
    """The host may have accepted input; automatic resubmission is unsafe."""


class StopBridge(BaseException):
    pass


def run_bounded(command, *, input=None, timeout=30.0, output_limit=65536):
    """Drain both pipes without threads; bound RAM and kill the process group.

    stdin uses an anonymous file so a child that never reads cannot block us.
    All paths, including SIGTERM and KeyboardInterrupt, reap the direct child.
    """
    with tempfile.TemporaryFile() as source, selectors.DefaultSelector() as selector:
        if input is not None:
            source.write(input.encode("utf-8"))
            source.seek(0)
        proc = subprocess.Popen(command, stdin=source if input is not None else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        total = 0
        deadline = time.monotonic() + timeout
        try:
            for stream, name in ((proc.stdout, "stdout"), (proc.stderr, "stderr")):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            while selector.get_map() or proc.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DispatchUncertain("handler timed out; inspect the target before retrying")
                for key, _ in selector.select(min(0.1, remaining)):
                    chunk = os.read(key.fd, 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    if total > output_limit:
                        raise DispatchUncertain("handler output limit exceeded; inspect target before retrying")
                    buffers[key.data].extend(chunk)
            return subprocess.CompletedProcess(command, proc.wait(),
                buffers["stdout"].decode("utf-8", errors="replace"),
                buffers["stderr"].decode("utf-8", errors="replace"))
        finally:
            # Descendants may keep pipes open or survive their parent.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            proc.stdout.close()
            proc.stderr.close()


@contextmanager
def profile_lock(state_dir: Path):
    path = state_dir / "receiver.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("this profile already has a receiver; stop it before starting another") from None
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        yield
    finally:
        os.close(fd)


@contextmanager
def stop_signals():
    def stop(signum, frame):
        raise StopBridge()
    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
