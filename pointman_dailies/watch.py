"""Changes under the dailies folder, with the standard library only (the node's watchfiles isn't here).

macOS: FSEvents through ctypes, per file, about a second after a change. Elsewhere: a poll of the
folders' modified times, which sees files added, removed or renamed; a file rewritten in place is
caught by the next full scan (`scan_interval`), or at once by `dailies.rescan`.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import sys
import threading
from collections.abc import Callable
from pathlib import Path

OnChange = Callable[[list[str]], None]       # absolute paths that changed


class FSEvents:
    """One FSEvents stream on its own run loop thread."""

    SINCE_NOW = 0xFFFFFFFFFFFFFFFF
    NO_DEFER, WATCH_ROOT, FILE_EVENTS = 0x2, 0x4, 0x10
    UTF8 = 0x08000100
    CALLBACK = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                ctypes.POINTER(ctypes.c_char_p), ctypes.POINTER(ctypes.c_uint32),
                                ctypes.POINTER(ctypes.c_uint64))

    def __init__(self, root: Path, on_change: OnChange, latency: float = 1.0):
        self.root, self.on_change, self.latency = root, on_change, latency
        cs = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreServices"))
        cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFArrayCreate.restype = ctypes.c_void_p
        cf.CFArrayCreate.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_long, ctypes.c_void_p]
        cf.CFRunLoopGetCurrent.restype = ctypes.c_void_p
        cf.CFRunLoopRun.restype = None
        cf.CFRunLoopStop.argtypes = [ctypes.c_void_p]
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        cs.FSEventStreamCreate.restype = ctypes.c_void_p
        cs.FSEventStreamCreate.argtypes = [ctypes.c_void_p, self.CALLBACK, ctypes.c_void_p, ctypes.c_void_p,
                                           ctypes.c_uint64, ctypes.c_double, ctypes.c_uint32]
        cs.FSEventStreamScheduleWithRunLoop.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        cs.FSEventStreamStart.restype = ctypes.c_bool
        cs.FSEventStreamStart.argtypes = [ctypes.c_void_p]
        for name in ("FSEventStreamStop", "FSEventStreamInvalidate", "FSEventStreamRelease"):
            getattr(cs, name).argtypes = [ctypes.c_void_p]
        self.cs, self.cf = cs, cf
        self.mode = ctypes.c_void_p.in_dll(cf, "kCFRunLoopDefaultMode")
        self.loop: int | None = None
        self.started = threading.Event()
        self.failed: str | None = None
        self._callback = self.CALLBACK(self._events)      # kept, or ctypes frees it under FSEvents

    def _events(self, _stream, _info, n, paths, _flags, _ids) -> None:
        try:
            self.on_change([paths[i].decode("utf-8", "replace") for i in range(n)])
        except Exception as e:  # noqa: BLE001 - an exception can't cross back into FSEvents
            print(f"watch: {e}", file=sys.stderr, flush=True)

    def run(self) -> None:
        """Blocks until stop(): call it on a thread of its own."""
        cf, cs = self.cf, self.cs
        path = cf.CFStringCreateWithCString(None, str(self.root).encode(), self.UTF8)
        paths = cf.CFArrayCreate(None, (ctypes.c_void_p * 1)(path), 1, None)
        stream = cs.FSEventStreamCreate(None, self._callback, None, paths, self.SINCE_NOW, self.latency,
                                        self.FILE_EVENTS | self.WATCH_ROOT | self.NO_DEFER)
        if not stream:
            self.failed = "FSEventStreamCreate failed"
            self.started.set()
            return
        self.loop = cf.CFRunLoopGetCurrent()
        cs.FSEventStreamScheduleWithRunLoop(stream, self.loop, self.mode)
        if not cs.FSEventStreamStart(stream):
            self.failed = "FSEventStreamStart failed"
            self.started.set()
            return
        self.started.set()
        try:
            cf.CFRunLoopRun()
        finally:
            cs.FSEventStreamStop(stream)
            cs.FSEventStreamInvalidate(stream)
            cs.FSEventStreamRelease(stream)
            cf.CFRelease(paths)
            cf.CFRelease(path)

    def stop(self) -> None:
        if self.loop:
            self.cf.CFRunLoopStop(self.loop)


class FolderPoll:
    """Folders' modified times, looked at every `interval` seconds: a folder that changed is listed
    again, and its files reported. Cheap (one stat per folder), but blind to files rewritten in place."""

    def __init__(self, root: Path, on_change: OnChange, ignored: Callable[[str], bool], interval: float = 3.0):
        self.root, self.on_change, self.ignored, self.interval = root, on_change, ignored, interval
        self.stopping = threading.Event()
        self.started = threading.Event()
        self.failed: str | None = None
        self.dirs: dict[str, tuple[float, list[str]]] = {}     # folder -> (mtime, its subfolders)

    def _look(self, report: bool) -> None:
        seen: dict[str, tuple[float, list[str]]] = {}
        stack = [str(self.root)]
        changed: list[str] = []
        while stack:
            d = stack.pop()
            try:
                mtime = os.stat(d).st_mtime
                known = self.dirs.get(d)
                if known and known[0] == mtime:
                    subdirs = known[1]
                else:
                    with os.scandir(d) as it:
                        entries = list(it)
                    subdirs = [e.path for e in entries if e.is_dir(follow_symlinks=False) and not self.ignored(e.name)]
                    if report:
                        changed += [e.path for e in entries if not e.is_dir(follow_symlinks=False)]
            except OSError:
                continue
            seen[d] = (mtime, subdirs)
            stack += subdirs
        self.dirs = seen
        if changed:
            self.on_change(changed)

    def run(self) -> None:
        self._look(report=False)
        self.started.set()
        while not self.stopping.wait(self.interval):
            self._look(report=True)

    def stop(self) -> None:
        self.stopping.set()


def watcher(root: Path, on_change: OnChange, ignored: Callable[[str], bool]):
    """FSEvents on macOS, the folder poll elsewhere (or if FSEvents can't start)."""
    if sys.platform == "darwin":
        try:
            return FSEvents(root, on_change)
        except (OSError, AttributeError, ValueError) as e:
            print(f"watch: FSEvents unavailable ({e}); polling folders", file=sys.stderr, flush=True)
    return FolderPoll(root, on_change, ignored)
