"""The dailies: watch the folder, let files settle, publish what a sync covers, and what's asked for.
From the Python node's agent/daemon.py (watch_loop, full_scan, consider, settle, enqueue, the
workers, syncs, review notes and `publish`/`rescan`), with core reached through the node.

Threads: the watcher (FSEvents, or a folder poll), the main loop (scans, settling, syncs, notes,
status), and the workers: "heavy" (video, sequences) and `light_workers` "light" ones (images,
audio, pages), so a long proxy doesn't hold up concept art.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import pages, sequences
from .config import Config, kind_of
from .publisher import CoreError, Node, Publisher
from .state import State
from .watch import watcher

HEAVY = {"video", "model"}
SEQUENCE_WAIT = 3   # x stable_seconds: a render writing frames must pause at least this long
SEQUENCE_MIN_INTERVAL = 600   # republish a growing sequence at most every 10 minutes
MAX_ATTEMPTS = 3
RETRY = 30          # seconds before a failed read of core's syncs or notes is tried again
STATUS_EVERY = 2


def log(*args) -> None:
    print("dailies:", *args, file=sys.stderr, flush=True)


@dataclass
class Job:
    kind: str                       # file | sequence
    rel: str
    caption: str | None = None
    by: str | None = None
    post: dict | None = None        # {"request_id": n} (shared), {"sync_id": n} (a sync's change)
    as_is: bool = False
    report: Callable[[str, float | None], None] | None = None
    done: threading.Event | None = None     # set when someone waits on it (dailies.publish)
    result: dict | None = None
    error: str | None = None
    extra: dict = field(default_factory=dict)


class Dailies:
    def __init__(self, cfg: Config, state: State, node: Node, publisher: Publisher,
                 event: Callable[[dict], None] = lambda status: None):
        self.cfg, self.state, self.node, self.publisher, self.event = cfg, state, node, publisher, event
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.dirty: set[str] = set()
        self.candidates: dict[str, tuple[int, float, float]] = {}
        self.queued: set[str] = set()
        self.kick = threading.Event()
        self.scan_now = threading.Event()
        self.syncs_now = threading.Event()     # core's syncs changed (syncs.changed), or a rescan: read them now
        self.notes_now = threading.Event()     # and its notes (notes.changed)
        self.syncs: list[dict] = []
        self._syncs_lock = threading.Lock()
        self.heavy: queue.Queue[Job] = queue.Queue()
        self.light: queue.Queue[Job] = queue.Queue()
        self.active: dict[str, dict] = {}
        self.last_scan = 0.0
        self.last_error: str | None = None
        self._index_hash = ""
        self._index_gzip = True
        self._caffeinate: subprocess.Popen | None = None
        self._awake_lock = threading.Lock()
        self.watcher = None
        self.threads: list[threading.Thread] = []
        if self.state.get("baseline") is None:
            self.state.set("baseline", time.time())
        self.baseline = float(self.state.get("baseline"))

    # ------------------------------------------------------------ scanning

    def walk(self):
        root = str(self.cfg.root)
        skip = len(root) + 1           # e.path is root + "/" + rel: Path.relative_to took half a scan's time
        stack = [root]
        while stack:
            d = stack.pop()
            try:
                it = os.scandir(d)
            except OSError:
                continue
            with it:
                for e in it:
                    if self.cfg.ignored(e.name):
                        continue
                    rel = e.path[skip:]
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                            continue
                        if not e.is_file(follow_symlinks=False) or kind_of(e.name) is None:
                            continue
                        st = e.stat()
                    except OSError:
                        continue
                    yield rel, st.st_size, st.st_mtime

    def full_scan(self) -> None:
        t0 = time.time()
        known = self.state.all_files()
        seen, index = [], []
        for rel, size, mtime in self.walk():
            index.append({"path": rel, "size": size, "mtime": round(mtime, 3), "kind": kind_of(rel)})
            row = known.get(rel)
            if row is None:
                if mtime < self.baseline:
                    seen.append((rel, size, mtime))
                else:
                    self.consider(rel, size, mtime)
            elif (row["size"], row["mtime"]) != (size, mtime):
                self.consider(rel, size, mtime)
            elif row["status"] == "failed" and row["attempts"] < MAX_ATTEMPTS \
                    and time.time() - row["updated_at"] > 600 * row["attempts"]:
                self.consider(rel, size, mtime, force=True)
            elif row["status"] in ("queued", "processing") and rel not in self.queued:
                self.consider(rel, size, mtime, force=True)   # interrupted by a restart
        if seen:
            self.state.mark_many_seen(seen)
        self.last_scan = time.time()
        log(f"scan: {len(index)} files in {time.time() - t0:.1f}s, {len(self.candidates)} waiting to settle")
        self.upload_index(index)

    def upload_index(self, index: list[dict]) -> None:
        """The folder's file list for the apps' Files tab (core keeps one per machine, gzipped)."""
        digest = hashlib.sha1(json.dumps(index, sort_keys=True).encode()).hexdigest()
        if digest == self._index_hash or not self._index_gzip:
            return
        try:
            self.node.call("PUT", "/v1/node/agent/index", {"root": str(self.cfg.root), "files": index}, gzip=True,
                           timeout=120)
            self._index_hash = digest
        except (CoreError, RuntimeError) as e:
            if "gzip" in str(e):
                self._index_gzip = False      # a node without core.call's gzip: don't ask every scan
            log(f"couldn't upload the file index: {e}")

    # ------------------------------------------------------------ syncs

    def refresh_syncs(self) -> None:
        """Core's syncs; post what's there now for new ones that ask for it."""
        syncs = self.node.call("GET", "/v1/node/syncs") or []
        with self._syncs_lock:
            self.syncs = [s for s in syncs if not s.get("paused")]
        for s in self.syncs:
            if s.get("include_existing") and not s.get("backfilled_at"):
                self.backfill_sync(s)

    def sync_for(self, rel: str) -> dict | None:
        """The most specific active sync covering a file (or a frame sequence's key)."""
        best = None
        with self._syncs_lock:
            for s in self.syncs:
                if rel == s["path"] or rel.startswith(s["path"] + "/"):
                    if best is None or len(s["path"]) > len(best["path"]):
                        best = s
        return best

    def backfill_sync(self, sync: dict) -> None:
        """A new sync that asked for what's already there: queue those files once."""
        base = self.cfg.root / sync["path"]
        files = [base] if base.is_file() else (sorted(p for p in base.rglob("*") if p.is_file()) if base.is_dir() else [])
        n = 0
        for f in files:
            rel = f.relative_to(self.cfg.root).as_posix()
            if not self.cfg.publishable(rel):
                continue
            st = f.stat()
            self.consider(rel, st.st_size, st.st_mtime, force=True)
            n += 1
        try:
            self.node.call("PATCH", f"/v1/node/syncs/{sync['id']}", {"backfilled": True})
            sync["backfilled_at"] = time.time()
        except CoreError as e:
            log(f"sync {sync['id']}: {e}")
        log(f"sync {sync['id']}: queued {n} existing files under {sync['path']}")

    # ------------------------------------------------------------ settling

    def consider(self, rel: str, size: int, mtime: float, *, force: bool = False) -> None:
        """A new or changed file: wait for it to settle, then publish it if a sync covers it."""
        if not force and not (self.cfg.publishable(rel) and self.sync_for(rel)):
            self.state.mark(rel, size, mtime, "skipped")
            return
        with self.lock:
            if rel in self.queued:
                return
            prev = self.candidates.get(rel)
            if prev is None or prev[:2] != (size, mtime):
                self.candidates[rel] = (size, mtime, time.time())

    def owned_by_page(self, rel: str, cache: dict | None = None) -> str | None:
        """Files an HTML page loads are published with the page, not on their own."""
        src = (self.cfg.root / rel).resolve()
        if kind_of(rel) == "page":
            return None
        cache = {} if cache is None else cache
        if src.parent not in cache:
            owned = {}
            for html in src.parent.glob("*.htm*"):
                for f in pages.owned_files(html):
                    owned.setdefault(f, html.name)
            cache[src.parent] = owned
        return cache[src.parent].get(src)

    def changed(self, paths: list[str]) -> None:
        """From the watcher: absolute paths that changed."""
        root = str(self.cfg.root)
        with self.lock:
            for path in paths:
                try:
                    rel = Path(path).relative_to(root).as_posix()
                except ValueError:
                    continue
                if kind_of(rel) and not self.cfg.ignored(rel):
                    self.dirty.add(rel)

    def settle(self) -> None:
        now = time.time()
        with self.lock:
            dirty, self.dirty = self.dirty, set()
        for rel in dirty:
            try:
                st = (self.cfg.root / rel).stat()
            except OSError:
                continue
            row = self.state.file(rel)
            if row is None or (row["size"], row["mtime"]) != (st.st_size, st.st_mtime):
                self.consider(rel, st.st_size, st.st_mtime)
        ready = []
        with self.lock:
            for rel, (size, mtime, first) in list(self.candidates.items()):
                try:
                    st = (self.cfg.root / rel).stat()
                except OSError:
                    del self.candidates[rel]
                    continue
                if (st.st_size, st.st_mtime) != (size, mtime):
                    self.candidates[rel] = (st.st_size, st.st_mtime, now)
                elif now - first >= self.cfg.stable_seconds and now - mtime >= self.cfg.stable_seconds:
                    del self.candidates[rel]
                    ready.append((rel, size, mtime, first))
        if not ready:
            return
        # one directory listing and one page scan per folder per tick, however many files are ready
        owned_cache: dict = {}
        listings: dict[Path, list[Path]] = {}

        def listing(folder: Path) -> list[Path]:
            if folder not in listings:
                try:
                    listings[folder] = list(folder.iterdir())
                except OSError:
                    listings[folder] = []
            return listings[folder]

        frames_by_key: dict[str, list[tuple]] = {}
        for item in ready:
            rel, size, mtime, first = item
            owner = self.owned_by_page(rel, owned_cache)
            if owner:
                self.state.mark(rel, size, mtime, "skipped", error=f"part of {owner}")
                continue
            k = sequences.key_for(rel)
            if k:
                frames_by_key.setdefault(k[0], []).append(item)
            else:
                sync = self.sync_for(rel)
                self.enqueue(Job("file", rel=rel, post={"sync_id": sync["id"]} if sync else None))
        minimum = self.cfg.stable_seconds * SEQUENCE_WAIT
        for key, items in frames_by_key.items():
            folder = self.cfg.root / key.rpartition("/")[0]
            frames = sequences.frames(self.cfg.root, key, listing(folder))
            mtimes = []
            for f in frames:
                try:
                    mtimes.append(f.stat().st_mtime)
                except OSError:
                    pass
            newest = max(mtimes, default=0.0)
            wait = now - newest < sequences.quiet_window(mtimes, minimum)
            is_sequence = len(frames) >= sequences.MIN_FRAMES
            if is_sequence and not wait:
                last = float(self.state.get(f"sequence_published:{key}", "0"))
                wait = now - last < SEQUENCE_MIN_INTERVAL
            if wait:
                # a render may still be writing frames: look again later
                with self.lock:
                    for rel, size, mtime, first in items:
                        self.candidates[rel] = (size, mtime, first)
                continue
            if is_sequence:
                for rel, size, mtime, _ in items:
                    self.state.mark(rel, size, mtime, "skipped", error=f"frame of {key}")
                sync = self.sync_for(key)
                self.enqueue(Job("sequence", rel=key, post={"sync_id": sync["id"]} if sync else None))
            else:
                for rel, size, mtime, _ in items:
                    sync = self.sync_for(rel)
                    self.enqueue(Job("file", rel=rel, post={"sync_id": sync["id"]} if sync else None))

    def enqueue(self, job: Job) -> bool:
        """Queue a job; False if the same file or sequence is already queued."""
        with self.lock:
            if job.rel in self.queued:
                return False
            self.queued.add(job.rel)
        if job.kind == "file":
            try:
                st = (self.cfg.root / job.rel).stat()
                self.state.mark(job.rel, st.st_size, st.st_mtime, "queued")
            except OSError:
                pass
        heavy = job.kind == "sequence" or kind_of(job.rel) in HEAVY
        (self.heavy if heavy else self.light).put(job)
        self.update_awake()
        self.kick.set()
        return True

    # ------------------------------------------------------------ workers

    def worker(self, name: str, q: queue.Queue) -> None:
        while not self.stop.is_set():
            try:
                job = q.get(timeout=1)
            except queue.Empty:
                continue
            self.active[name] = {"path": job.rel, "stage": "starting", "progress": None, "started": time.time()}
            self.update_awake()
            try:
                self.run_job(name, job)
            finally:
                self.active.pop(name, None)
                with self.lock:
                    self.queued.discard(job.rel)
                if job.done is not None:
                    job.done.set()
                self.update_awake()

    def reporter(self, name: str, job: Job):
        def report(stage: str, frac: float | None) -> None:
            if name in self.active:
                self.active[name].update(stage=stage, progress=frac)
            if job.report:
                job.report(stage, frac)
        return report

    def run_job(self, name: str, job: Job) -> None:
        rel = job.rel
        log(f"publishing {rel}")
        try:
            if job.kind == "sequence":
                out = self.publisher.publish_sequence(rel, caption=job.caption, by=job.by,
                                                      report=self.reporter(name, job), post=job.post)
                self.state.set(f"sequence_published:{rel}", time.time())
                if out is None:
                    return
            else:
                out = self.publisher.publish_file(rel, caption=job.caption, by=job.by,
                                                  report=self.reporter(name, job), post=job.post, as_is=job.as_is)
        except Exception as e:  # noqa: BLE001 - one bad file mustn't stop the dailies
            log(f"failed: {rel}: {e}")
            self.last_error = f"{rel}: {e}"
            job.error = str(e) or type(e).__name__
            return
        job.result = out
        log(f"published {rel} as file {out.get('asset_id')}, version {out.get('version_id')}")

    # ------------------------------------------------------------ asked for (dailies.publish)

    def publish(self, params: dict, *, request_id: int | None, report=None) -> dict:
        """share_file and `pointman push`: one file now (or a folder's files, through settling)."""
        rel = str(params.get("path", "")).strip()
        root = str(self.cfg.root.resolve())
        for prefix in (root, str(self.cfg.root)):
            if rel.startswith(prefix + "/"):          # absolute paths under the watched folder are fine
                rel = rel[len(prefix) + 1:]
                break
        rel = rel.strip("/")
        target = (self.cfg.root / rel).resolve()
        if not rel or not target.is_relative_to(self.cfg.root.resolve()) or not target.exists():
            raise ValueError(f"{rel!r} isn't under {self.cfg.root}")
        if target.is_dir() and params.get("channel_id"):
            raise ValueError("share one file at a time (or a folder from the app's Mac tab)")
        if target.is_dir():
            # through the usual settling, so sequences and page bundles are grouped
            count = 0
            for r, size, mtime in self.walk():
                if r.startswith(rel + "/"):
                    self.consider(r, size, mtime, force=True)
                    count += 1
            return {"message": f"Queued {count} files"}
        as_is = bool(params.get("as_is"))
        if kind_of(rel) is None and not as_is:
            raise ValueError(f"can't publish {target.suffix} files (share_file as_is=True sends any file as it is)")
        if kind_of(rel) == "model" and not as_is:
            raise ValueError("3D files are the Blender extension's to preview (blender.preview); "
                             "share_file as_is=True sends the file itself")
        shared = params.get("channel_id") or params.get("thread_id")
        if shared and request_id:
            post = {"request_id": request_id}           # where they asked, as them
        elif shared:
            post = {k: params[k] for k in ("channel_id", "thread_id", "title", "text") if params.get(k)}
        else:
            sync = self.sync_for(rel)
            post = {"sync_id": sync["id"]} if sync else None
        job = Job("file", rel=rel, caption=params.get("caption"), by=params.get("by"), post=post, as_is=as_is,
                  report=report, done=threading.Event())
        if not self.enqueue(job):
            return {"message": "Already being published; it'll show up on the board shortly"}
        while not job.done.wait(1):
            if self.stop.is_set():
                raise RuntimeError("stopped while publishing; ask again")
        if job.error:
            raise RuntimeError(job.error)
        return {**(job.result or {}), "message": f"Published {Path(rel).name}"}

    def rescan(self) -> dict:
        self.scan_now.set()
        self.syncs_now.set()
        self.kick.set()
        return {"message": "Rescanning"}

    # ------------------------------------------------------------ review notes

    def sync_notes(self) -> None:
        """Every project's comments, as <project>/review-notes.md for sessions to read; each note
        marked synced once it's in the file. Notes on a Resolve timeline at a timecode are left for
        Resolve's markers (the Python node's, while it drives Resolve)."""
        if not self.cfg.notes_file:
            return
        since = float(self.state.get("notes_since", "0"))
        # a little overlap: core's timestamps are taken before commit, so a note can land with a time
        # just below a cursor already moved past
        changed = self.node.call("GET", f"/v1/node/notes?since={max(0.0, since - 30)}&include_deleted=true&limit=5000") or []
        changed = [n for n in changed
                   if not ((st := self.state.note_state(n["id"])) and st["updated_at"] == n["updated_at"])
                   or n["updated_at"] > since]
        if not changed:
            return
        for n in changed:
            since = max(since, n["updated_at"])
        self.state.set("notes_since", since)
        written = self.write_notes_files()
        for n in changed:
            timeline = (n.get("asset") or {}).get("kind") == "timeline" and n.get("timecode") is not None
            if timeline:
                continue
            if not n.get("deleted") and not n.get("synced_at"):
                info = {"notes_file": written.get(n["asset"]["project"]["slug"])}
                try:
                    self.node.call("POST", f"/v1/node/notes/{n['id']}/synced", {"info": info})
                except CoreError:
                    pass
            self.state.set_note_state(n["id"], n["updated_at"], None)

    def write_notes_files(self) -> dict[str, str]:
        notes = self.node.call("GET", "/v1/node/notes?limit=5000") or []
        by_project: dict[str, list[dict]] = {}
        for n in notes:
            by_project.setdefault(n["asset"]["project"]["slug"], []).append(n)
        written = {}
        for folder in self.cfg.root.iterdir():
            if not folder.is_dir() or self.cfg.ignored(folder.name):
                continue
            path = folder / self.cfg.notes_file
            ns = by_project.get(folder.name, [])
            if not ns and not path.exists():
                continue
            text = render_notes(folder.name, ns)
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                path.write_text(text, encoding="utf-8")
            written[folder.name] = str(path.relative_to(self.cfg.root))
        return written

    # ------------------------------------------------------------ status and power

    def status(self) -> dict:
        counts = self.state.counts()
        return {"root": str(self.cfg.root), "watching": type(self.watcher).__name__ if self.watcher else None,
                "queue": {"waiting": len(self.candidates), "queued": self.heavy.qsize() + self.light.qsize(),
                          "active": list(self.active.values()), "published": counts.get("published", 0),
                          "failed": counts.get("failed", 0)},
                "failures": self.state.failures(5), "syncs": len(self.syncs),
                "last_scan": self.last_scan, "baseline": self.baseline, "last_error": self.last_error}

    def update_awake(self) -> None:
        """caffeinate while there's publishing to do (macOS), so the Mac doesn't sleep mid-upload."""
        if sys.platform != "darwin":
            return
        busy = bool(self.active) or not self.heavy.empty() or not self.light.empty()
        want = self.cfg.keep_awake == "always" or (self.cfg.keep_awake == "while_busy" and busy)
        with self._awake_lock:
            running = self._caffeinate is not None and self._caffeinate.poll() is None
            if want and not running:
                self._caffeinate = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())],
                                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                    stderr=subprocess.DEVNULL)
            elif not want and running:
                self._caffeinate.terminate()
                self._caffeinate = None

    # ------------------------------------------------------------ main

    def _step(self, name: str, fn) -> bool:
        """One part of the main loop; failures are logged, never the end of it. Whether it went."""
        try:
            fn()
            return True
        except CoreError as e:
            log(f"{name}: {e}")
        except Exception as e:  # noqa: BLE001
            import traceback
            log(f"{name} failed: {traceback.format_exc()}")
            self.last_error = f"{name}: {e}"
        return False

    def start(self) -> None:
        if not self.cfg.root.is_dir():
            self.last_error = f"{self.cfg.root} isn't a folder"
            log(self.last_error)
        self.cfg.root = self.cfg.root.resolve()      # FSEvents names files by their real paths
        self.watcher = watcher(self.cfg.root, self.changed, self.cfg.ignored)
        names = [("heavy", self.heavy), *[(f"light-{i + 1}" if i else "light", self.light)
                                          for i in range(self.cfg.light_workers)]]
        self.threads = [threading.Thread(target=self.worker, args=(n, q), name=n, daemon=True) for n, q in names]
        self.threads += [threading.Thread(target=self.watcher.run, name="watch", daemon=True),
                         threading.Thread(target=self.loop, name="dailies", daemon=True)]
        for t in self.threads:
            t.start()
        log(f"watching {self.cfg.root} ({type(self.watcher).__name__}), publishing changes since "
            f"{datetime.fromtimestamp(self.baseline).isoformat(timespec='minutes')}")

    def loop(self) -> None:
        again = {"syncs": 0.0, "notes": 0.0}       # when a failed read is tried again (0: it didn't fail)

        def read(name: str, asked: threading.Event, fn) -> None:
            """Core's syncs or notes: read at the start, then only when the node says they changed (syncs.changed,
            notes.changed: core pushes them, so nothing polls them) or a rescan asks. A failed read goes again after
            RETRY."""
            if asked.is_set() or (again[name] and time.time() >= again[name]):
                asked.clear()
                again[name] = 0.0 if self._step(name, fn) else time.time() + RETRY

        self.syncs_now.set()
        self.notes_now.set()
        read("syncs", self.syncs_now, self.refresh_syncs)     # what to publish by itself, before the first scan
        self._step("scan", self.full_scan)
        last_status = 0.0
        while not self.stop.is_set():
            read("syncs", self.syncs_now, self.refresh_syncs)
            if self.scan_now.is_set() or time.time() - self.last_scan > self.cfg.scan_interval:
                self.scan_now.clear()
                self._step("scan", self.full_scan)
                self.last_scan = time.time()
            self._step("settle", self.settle)
            read("notes", self.notes_now, self.sync_notes)
            if time.time() - last_status > STATUS_EVERY:
                last_status = time.time()
                self._step("status", lambda: self.event(self.status()))
            self.kick.wait(1)
            self.kick.clear()

    def close(self) -> None:
        self.stop.set()
        self.kick.set()
        if self.watcher is not None:
            self.watcher.stop()
        if self._caffeinate is not None:
            self._caffeinate.terminate()


def _tc(seconds: float | None) -> str:
    if seconds is None:
        return ""
    s = int(seconds)
    return f"[{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}] " if s >= 3600 else f"[{s // 60}:{s % 60:02d}] "


def render_notes(project: str, notes: list[dict]) -> str:
    open_ = [n for n in notes if not n["resolved"]]
    done = [n for n in notes if n["resolved"]]
    lines = [f"# Review notes: {project}", "",
             "Notes left in the Message Board app. This file is rewritten by the Mac agent whenever",
             "a note changes, so don't edit it here; resolve or reply in the app instead.", "",
             f"Open: {len(open_)} · Resolved: {len(done)}", ""]
    for title, group in (("Open", open_), ("Resolved", done)):
        if not group:
            continue
        lines += [f"## {title}", ""]
        by_asset: dict[str, list[dict]] = {}
        for n in group:
            by_asset.setdefault(n["asset"]["path"], []).append(n)
        for path, ns in by_asset.items():
            kind = ns[0]["asset"]["kind"]
            lines.append(f"### {path} ({kind})")
            lines.append("")
            for n in sorted(ns, key=lambda n: (n["timecode"] is None, n["timecode"] or 0, n["created_at"])):
                when = datetime.fromtimestamp(n["created_at"]).strftime("%d %b %H:%M")
                ver = f"v{n['version_number']}, " if n.get("version_number") else ""
                body = n["body"].replace("\n", "\n  ")
                lines.append(f"- {_tc(n['timecode'])}{body}  \n  _{ver}{n['author']}, {when}_")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"
