"""Cross-process file locking on Unix and Windows.

Provides sync FileLock (for scripts) and AsyncFileLock (for async bot code).
Lock file is {path}.lock — separate from the target file to avoid
conflicts with os.replace() during atomic writes.

"""

from __future__ import annotations

import asyncio
import importlib
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import TracebackType
from typing import IO

if os.name == "nt":
    import msvcrt
else:
    fcntl = importlib.import_module("fcntl")


def _lock_file(fd: IO[bytes], *, blocking: bool = True) -> None:
    if os.name != "nt":
        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        fcntl.flock(fd, flags)
        return
    fd.seek(0)
    while True:
        try:
            msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError as exc:
            if not blocking:
                raise BlockingIOError("File is already locked") from exc
            time.sleep(0.1)


def _unlock_file(fd: IO[bytes]) -> None:
    if os.name != "nt":
        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    fd.seek(0)
    msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)


class FileLock:
    """Sync cross-process file lock.

    Usage::

        with FileLock("/path/to/data.json"):
            data = json.loads(Path("/path/to/data.json").read_text())
            data["key"] = "value"
            Path("/path/to/data.json").write_text(json.dumps(data))
    """

    def __init__(self, path: str | Path, *, blocking: bool = True) -> None:
        self._lock_path = Path(path).with_suffix(Path(path).suffix + ".lock")
        self._fd: IO[bytes] | None = None
        self._blocking = blocking

    def __enter__(self) -> FileLock:
        self._fd = open(self._lock_path, "a+b")
        if self._fd.tell() == 0:
            self._fd.write(b"\0")
            self._fd.flush()
        try:
            _lock_file(self._fd, blocking=self._blocking)
        except BlockingIOError:
            # Non-blocking acquire failed: another process holds the lock.
            self._fd.close()
            self._fd = None
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._fd is not None:
            _unlock_file(self._fd)
            self._fd.close()
            self._fd = None
            # Lock file is intentionally NOT unlinked: removing it while
            # another process already open()'d the same path lets a third
            # process create a fresh inode and flock it concurrently —
            # two holders of "the same" lock (stale-inode race).


class AsyncFileLock:
    """Async cross-process file lock via run_in_executor.

    Uses a dedicated ThreadPoolExecutor (not the default) to avoid
    blocking the executor pool during long lock waits.

    Usage::

        async with AsyncFileLock("/path/to/data.json"):
            # read-modify-write under lock
            ...
    """

    _shared_executor: ThreadPoolExecutor | None = None

    def __init__(self, path: str | Path, executor: ThreadPoolExecutor | None = None) -> None:
        self._lock_path = Path(path).with_suffix(Path(path).suffix + ".lock")
        self._executor = executor
        self._fd: IO[bytes] | None = None

    def _get_executor(self) -> ThreadPoolExecutor:
        if self._executor is not None:
            return self._executor
        if AsyncFileLock._shared_executor is None:
            AsyncFileLock._shared_executor = ThreadPoolExecutor(
                max_workers=4, thread_name_prefix="flock"
            )
        return AsyncFileLock._shared_executor

    def _acquire(self) -> None:
        self._fd = open(self._lock_path, "a+b")  # noqa: SIM115
        if self._fd.tell() == 0:
            self._fd.write(b"\0")
            self._fd.flush()
        _lock_file(self._fd)

    def _release(self) -> None:
        if self._fd is not None:
            _unlock_file(self._fd)
            self._fd.close()
            self._fd = None
            # No unlink — see FileLock.__exit__ (stale-inode race).

    async def __aenter__(self) -> AsyncFileLock:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._get_executor(), self._acquire)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        # Release runs inline: LOCK_UN and close() never block, and routing it
        # through the 4-worker pool can deadlock — four tasks blocked in
        # _acquire fill every worker, the holder's release queues behind them,
        # and nobody ever gets the lock.
        self._release()
