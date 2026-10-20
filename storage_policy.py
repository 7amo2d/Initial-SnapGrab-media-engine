from __future__ import annotations

import shutil
import threading
from pathlib import Path
from typing import Callable


class StorageBudgetError(RuntimeError):
    pass


class StorageBudget:
    """Thread-safe reservation accounting for downloads and temporary mux files."""

    def __init__(
        self,
        download_dir: Path,
        max_total_bytes: int,
        min_free_bytes: int,
        max_file_bytes: int,
        disk_usage: Callable[[Path], object] | None = None,
    ) -> None:
        self.download_dir = Path(download_dir)
        self.max_total_bytes = max(1, int(max_total_bytes))
        self.min_free_bytes = max(0, int(min_free_bytes))
        self.max_file_bytes = max(1, int(max_file_bytes))
        self.max_working_bytes = self.max_file_bytes * 2
        self._disk_usage = disk_usage or shutil.disk_usage
        self._lock = threading.RLock()
        self._reservations: dict[str, int] = {}

    @staticmethod
    def _directory_size(directory: Path) -> int:
        total = 0
        if not directory.exists():
            return 0
        for item in directory.rglob("*"):
            try:
                if item.is_file():
                    total += item.stat().st_size
            except OSError:
                continue
        return total

    def _snapshot(self) -> tuple[int, int]:
        used = self._directory_size(self.download_dir)
        free = int(self._disk_usage(self.download_dir).free)
        return used, free

    def _validate_capacity(self, used: int, free: int, reserved: int) -> None:
        if used + reserved > self.max_total_bytes:
            raise StorageBudgetError("Media engine storage quota is full")
        if free - reserved < self.min_free_bytes:
            raise StorageBudgetError("Media engine has too little free disk space")

    def reserve(self, job_id: str) -> None:
        with self._lock:
            if job_id in self._reservations:
                return
            used, free = self._snapshot()
            reserved = sum(self._reservations.values()) + self.max_working_bytes
            self._validate_capacity(used, free, reserved)
            self._reservations[job_id] = self.max_working_bytes

    def refresh(self, job_id: str) -> None:
        with self._lock:
            if job_id not in self._reservations:
                return
            job_dir_size = self._directory_size(self.download_dir / job_id)
            if job_dir_size > self.max_working_bytes:
                raise StorageBudgetError("Media engine active job storage cap exceeded")
            self._reservations[job_id] = max(0, self.max_working_bytes - job_dir_size)
            used, free = self._snapshot()
            self._validate_capacity(used, free, sum(self._reservations.values()))

    def release(self, job_id: str) -> None:
        with self._lock:
            self._reservations.pop(job_id, None)

    @property
    def reserved_bytes(self) -> int:
        with self._lock:
            return sum(self._reservations.values())
