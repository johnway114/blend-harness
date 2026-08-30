"""Owned subprocess execution, cancellation, offline policy, and cleanup."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .errors import BlendError, ErrorCategory


_SAFE_ENVIRONMENT = {
    "HOME", "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TEMP", "TMP",
    "USER", "LOGNAME", "SHELL", "XDG_CACHE_HOME", "XDG_CONFIG_HOME",
    "BLENDER_USER_CONFIG", "BLENDER_USER_SCRIPTS", "BLENDER_USER_DATAFILES",
    "METAL_DEVICE_WRAPPER_TYPE", "CUDA_VISIBLE_DEVICES", "HIP_VISIBLE_DEVICES",
}
_SECRET_MARKERS = ("SECRET", "TOKEN", "PASSWORD", "PASSWD", "API_KEY", "PRIVATE_KEY", "CREDENTIAL")


@dataclass(slots=True)
class CompletedOwnedProcess:
    args: list[str]
    returncode: int
    stdout: str
    duration_seconds: float
    log_path: Path
    timed_out: bool = False
    interrupted: bool = False


class ProcessSupervisor:
    """Tracks process groups and guarantees terminal cleanup on every path."""

    def __init__(self) -> None:
        self._processes: dict[int, subprocess.Popen[str]] = {}
        self._lock = threading.RLock()
        self._interrupted = threading.Event()
        self._previous_handlers: dict[int, object] = {}

    @property
    def interrupted(self) -> bool:
        return self._interrupted.is_set()

    @property
    def active_pids(self) -> list[int]:
        with self._lock:
            return sorted(self._processes)

    def __enter__(self) -> "ProcessSupervisor":
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                self._previous_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, self._signal_handler)
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.terminate_all(grace_seconds=3.0)
        if threading.current_thread() is threading.main_thread():
            for signum, handler in self._previous_handlers.items():
                signal.signal(signum, handler)  # type: ignore[arg-type]
            self._previous_handlers.clear()

    def _signal_handler(self, signum: int, frame: object) -> None:
        self._interrupted.set()
        self.interrupt_all()

    def interrupt_all(self) -> None:
        with self._lock:
            processes = list(self._processes.values())
        for process in processes:
            self._signal_group(process, signal.SIGINT)

    def terminate_all(self, *, grace_seconds: float) -> None:
        with self._lock:
            processes = list(self._processes.values())
        if not processes:
            return
        deadline = time.monotonic() + grace_seconds
        for process in processes:
            if process.poll() is None:
                self._signal_group(process, signal.SIGTERM)
        for process in processes:
            if process.poll() is None:
                try:
                    process.wait(timeout=max(0.0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    self._signal_group(process, signal.SIGKILL)
        for process in processes:
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        with self._lock:
            for process in processes:
                self._processes.pop(process.pid, None)

    @staticmethod
    def _signal_group(process: subprocess.Popen[str], signum: signal.Signals) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signum)
            else:
                process.send_signal(signum)
        except ProcessLookupError:
            return

    def run(self, args: Sequence[str], *, cwd: Path, log_path: Path,
            timeout_seconds: float | None = None, environment: Mapping[str, str] | None = None,
            offline: bool = False, enforce_offline: bool = False) -> CompletedOwnedProcess:
        if self.interrupted:
            raise BlendError(
                code="PROCESS_CANCELLED_BEFORE_START",
                category=ErrorCategory.INTERRUPTED,
                message="Operation was cancelled before the next process started.",
                remediation="Run the reported safe resume command.",
            )
        command = list(args)
        if offline:
            wrapper = offline_wrapper()
            if wrapper:
                command = [*wrapper, *command]
            elif enforce_offline:
                raise BlendError(
                    code="SECURITY_OFFLINE_UNAVAILABLE",
                    category=ErrorCategory.SECURITY,
                    message="Offline mode was requested but no enforceable host network wrapper is available.",
                    remediation="Install bubblewrap on Linux, enable unprivileged network namespaces, or explicitly permit network access.",
                )
        env = sanitized_environment(environment)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        timed_out = False
        interrupted = False
        with log_path.open("w", encoding="utf-8", errors="replace") as log:
            process = subprocess.Popen(
                command,
                cwd=str(cwd),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            with self._lock:
                self._processes[process.pid] = process
            try:
                process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._signal_group(process, signal.SIGINT)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._signal_group(process, signal.SIGTERM)
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        self._signal_group(process, signal.SIGKILL)
                        process.wait()
            finally:
                interrupted = self.interrupted
                with self._lock:
                    self._processes.pop(process.pid, None)
        stdout = _bounded_log(log_path)
        return CompletedOwnedProcess(
            args=command,
            returncode=process.returncode,
            stdout=stdout,
            duration_seconds=time.monotonic() - started,
            log_path=log_path,
            timed_out=timed_out,
            interrupted=interrupted,
        )


def sanitized_environment(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key in _SAFE_ENVIRONMENT}
    env.setdefault("PATH", os.defpath)
    env.setdefault("LANG", "C.UTF-8")
    for key, value in (extra or {}).items():
        upper = key.upper()
        if any(marker in upper for marker in _SECRET_MARKERS):
            raise BlendError(
                code="SECURITY_SECRET_ENVIRONMENT_REJECTED",
                category=ErrorCategory.SECURITY,
                message=f"Environment variable {key!r} looks secret-bearing and was not passed to Blender.",
                remediation="Use a declared non-secret asset or explicitly redesign the project to avoid runtime secrets.",
            )
        if not key.startswith("BLEND_"):
            raise BlendError(
                code="SECURITY_ENVIRONMENT_NOT_ALLOWLISTED",
                category=ErrorCategory.SECURITY,
                message=f"Environment variable {key!r} is not allowlisted for Blender.",
                remediation="Prefix declared non-secret project variables with BLEND_.",
            )
        env[key] = value
    return env


def offline_wrapper() -> list[str] | None:
    if sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file():
        # Default host access stays available; all socket/network operations are denied.
        return ["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)(deny network*)"]
    bwrap = shutil.which("bwrap")
    if bwrap:
        return [bwrap, "--unshare-net", "--dev-bind", "/", "/"]
    unshare = shutil.which("unshare")
    if unshare and _linux_unprivileged_namespaces_enabled():
        return [unshare, "--net", "--"]
    return None


def offline_capability() -> dict[str, object]:
    wrapper = offline_wrapper()
    return {
        "available": wrapper is not None,
        "method": Path(wrapper[0]).name if wrapper else None,
        "claim": "Host network syscalls are denied for the owned process group." if wrapper else
                 "No complete Blender Python sandbox is claimed; network isolation is unavailable.",
    }


def _linux_unprivileged_namespaces_enabled() -> bool:
    path = Path("/proc/sys/kernel/unprivileged_userns_clone")
    if not path.is_file():
        return False
    try:
        return path.read_text(encoding="ascii").strip() == "1"
    except OSError:
        return False


def _bounded_log(path: Path, max_bytes: int = 64 * 1024) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
            content = handle.read().decode("utf-8", errors="replace")
        return content
    except OSError:
        return ""
