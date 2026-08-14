"""Cross-platform exclusive project execution lease."""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Callable
from typing import Any

from manju.production.models import ProductionError, ReasonCode, utc_now


def _process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(
            process_query_limited_information, False, pid
        )
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return ctypes.get_last_error() == 5
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class ProjectLock:
    def __init__(
        self,
        path: str,
        *,
        attempts: int = 2,
        on_acquired: Callable[["ProjectLock"], None] | None = None,
        on_released: Callable[["ProjectLock"], None] | None = None,
    ):
        self.path = os.path.abspath(path)
        self.attempts = max(1, attempts)
        self.on_acquired = on_acquired
        self.on_released = on_released
        self._owned = False
        self.lease_id = uuid.uuid4().hex
        self.created_at = utc_now()
        self.recovered: dict[str, Any] | None = None

    def __enter__(self) -> "ProjectLock":
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        payload: dict[str, Any] = {
            "pid": os.getpid(),
            "lease_id": self.lease_id,
            "created_at": self.created_at,
        }
        encoded = json.dumps(payload, ensure_ascii=True).encode("ascii")
        for attempt in range(self.attempts):
            try:
                descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    os.write(descriptor, encoded)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                self._owned = True
                try:
                    if self.on_acquired is not None:
                        self.on_acquired(self)
                except Exception:
                    try:
                        os.unlink(self.path)
                    except OSError:
                        pass
                    self._owned = False
                    raise
                return self
            except FileExistsError as exc:
                owner_pid = 0
                owner: dict[str, Any] = {}
                try:
                    with open(self.path, "r", encoding="ascii") as handle:
                        value = json.load(handle)
                        owner = value if isinstance(value, dict) else {}
                        owner_pid = int(owner.get("pid", 0))
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    pass
                if owner_pid and not _process_exists(owner_pid) and attempt + 1 < self.attempts:
                    self.recovered = owner
                    try:
                        os.unlink(self.path)
                    except OSError:
                        pass
                    time.sleep(0.01)
                    continue
                raise ProductionError(
                    ReasonCode.PROJECT_LOCKED.value,
                    f"项目正由进程 {owner_pid or 'unknown'} 推进",
                ) from exc
        raise ProductionError(ReasonCode.PROJECT_LOCKED.value)

    def __exit__(self, exc_type, exc, traceback) -> None:
        if not self._owned:
            return
        release_error: Exception | None = None
        try:
            if self.on_released is not None:
                try:
                    self.on_released(self)
                except Exception as callback_error:
                    release_error = callback_error
            try:
                with open(self.path, "r", encoding="ascii") as handle:
                    owner = json.load(handle)
                if owner.get("pid") == os.getpid() and owner.get("lease_id") == self.lease_id:
                    os.unlink(self.path)
            except (OSError, TypeError, json.JSONDecodeError):
                pass
        finally:
            self._owned = False
        if release_error is not None and exc_type is None:
            raise release_error
