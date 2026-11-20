"""Shared memory, wake-ups and the tap's clock on Windows and Linux.

The frame tap (docs/frame-tap.md) was written against Windows' named file mappings, named auto-reset events and
QueryPerformanceCounter. This module gives the same three things on both systems:

- open_mapping(name, size): a shared mmap. Windows: a named file mapping (`Local\\chiaki-frametap`). Linux: a POSIX
  shared memory object in /dev/shm, named after the Windows name without its `Local\\` prefix
  (`/dev/shm/chiaki-frametap`), created if missing and grown to `size`.
- Event: one writer wakes one reader. Windows: the named auto-reset event. Linux: a futex on a u32 counter inside the
  mapping (EVENT_OFFSET, in the header's reserved bytes); the writer adds 1 and wakes, the reader sleeps until the
  counter moves on from the last value it saw, which behaves like an auto-reset event.
- qpc(), qpc_freq(): the tap's timestamps. Windows: QueryPerformanceCounter. Linux: CLOCK_MONOTONIC in nanoseconds
  (frequency 1e9), the clock time.perf_counter() uses there too, and the one chiaki_tap/frametap.c writes.
"""
from __future__ import annotations

import ctypes
import mmap
import os
import platform
import sys
import time

WINDOWS = sys.platform == "win32"
LINUX = sys.platform.startswith("linux")
EVENT_OFFSET = 96  # Linux: the header's u32 wake counter (frametap.h); reserved (zero) on Windows

if WINDOWS:
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateEventW.restype = ctypes.c_void_p
    _k32.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p]
    _k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    _k32.SetEvent.argtypes = [ctypes.c_void_p]
    _k32.CloseHandle.argtypes = [ctypes.c_void_p]

    def qpc() -> int:
        v = ctypes.c_int64()
        _k32.QueryPerformanceCounter(ctypes.byref(v))
        return v.value

    def qpc_freq() -> int:
        v = ctypes.c_int64()
        _k32.QueryPerformanceFrequency(ctypes.byref(v))
        return v.value
else:
    def qpc() -> int:
        return time.clock_gettime_ns(time.CLOCK_MONOTONIC)

    def qpc_freq() -> int:
        return 1_000_000_000


def posix_name(name: str) -> str:
    """The /dev/shm name for a Windows mapping name: `Local\\chiaki-frametap` -> `chiaki-frametap`."""
    for prefix in ("Local\\", "Global\\"):
        if name.startswith(prefix):
            name = name[len(prefix):]
    name = name.lstrip("/").replace("\\", "_").replace("/", "_")
    if not name:
        raise ValueError("empty shared memory name")
    return name


def shm_path(name: str) -> str:
    return os.path.join("/dev/shm", posix_name(name))


def open_mapping(name: str, size: int) -> mmap.mmap:
    """The shared memory called `name`, at least `size` bytes, created if neither side has yet."""
    if WINDOWS:
        return mmap.mmap(-1, size, tagname=name)
    if not LINUX:
        raise RuntimeError("the frame tap needs Windows or Linux")
    fd = os.open(shm_path(name), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.fstat(fd).st_size < size:
            os.ftruncate(fd, size)  # new pages read as zero: magic 0, no stream yet
        return mmap.mmap(fd, size, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
    finally:
        os.close(fd)


# ----------------------------------------------------------------------------- Linux futex
_FUTEX_WAIT, _FUTEX_WAKE = 0, 1  # not FUTEX_PRIVATE_FLAG: the word is shared between processes
_SYS_FUTEX = {"x86_64": 202, "aarch64": 98, "arm64": 98}.get(platform.machine())


class _Timespec(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]


_libc = None
if LINUX:
    _libc = ctypes.CDLL(None, use_errno=True)
    _libc.syscall.restype = ctypes.c_long


class Event:
    """Wakes the reader after each frame. `name` is the Windows event name; `mm` the mapping that holds the Linux
    counter at `offset`."""

    def __init__(self, name: str, mm: mmap.mmap, offset: int = EVENT_OFFSET) -> None:
        self._mm = mm
        if WINDOWS:
            self._h = _k32.CreateEventW(None, 0, 0, name)
            return
        self._word = ctypes.c_uint32.from_buffer(mm, offset)
        self._addr = ctypes.addressof(self._word)
        self._seen = self._word.value

    def set(self) -> None:
        if WINDOWS:
            _k32.SetEvent(self._h)
            return
        self._word.value = (self._word.value + 1) & 0xFFFFFFFF  # one writer: no lost updates
        if _SYS_FUTEX is not None:
            _libc.syscall(_SYS_FUTEX, ctypes.c_void_p(self._addr), _FUTEX_WAKE, 1, None, None, 0)

    def wait(self, timeout_s: float) -> None:
        if WINDOWS:
            _k32.WaitForSingleObject(self._h, int(timeout_s * 1000))
            return
        v = self._word.value
        if v == self._seen:
            if _SYS_FUTEX is None:  # an unknown CPU: poll
                t_end = time.perf_counter() + timeout_s
                while self._word.value == v and time.perf_counter() < t_end:
                    time.sleep(0.0005)
            else:
                ts = _Timespec(int(timeout_s), int((timeout_s % 1) * 1e9))
                _libc.syscall(_SYS_FUTEX, ctypes.c_void_p(self._addr), _FUTEX_WAIT, ctypes.c_uint32(v),
                              ctypes.byref(ts), None, 0)
        self._seen = self._word.value

    def close(self) -> None:
        if WINDOWS:
            _k32.CloseHandle(self._h)
        else:
            del self._word  # releases the buffer export, so the mmap can close
