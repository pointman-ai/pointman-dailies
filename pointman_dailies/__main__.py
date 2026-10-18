"""The dailies extension's process: `python -m pointman_dailies`, started and spoken to by the node.

- Watches the dailies folder (`root`), lets files settle, and publishes what a sync covers.
- dailies.publish: share_file and `pointman push` (core's `publish` job): one file now, or a folder's
  files through the usual settling.
- dailies.rescan: core's `rescan` job: a full scan and core's syncs again, now.
- dailies.syncs_changed (a method): the node saw core's syncs change; look at them now rather than
  within SYNCS_EVERY.

Files go through the node (files.publish) and so does every other call to core (core.call): the
extension never holds a token.
"""

from __future__ import annotations

import re
import sys
import threading
from pathlib import Path

try:
    from pointman_extension import Extension
except ImportError:                 # a node from before the rename (thread 2255 [3561])
    from mb_extension import Extension  # type: ignore[no-redef]

from .config import Config
from .dailies import Dailies, log
from .publisher import Node, Publisher
from .state import State

ext = Extension()
ready = threading.Event()
now: dict = {}          # the running Dailies, its State and what was taken over


def python_node_state(state_dir: Path) -> Path | None:
    """The Python node's state.db, if there is one to take over: `takeover` in the settings, or
    where that node keeps it (its extensions' state folders are inside its own)."""
    given = str(ext.settings.get("takeover") or "").strip()
    if given:
        return Path(given).expanduser()
    candidates = [state_dir.parent.parent / "state.db",
                  Path("~/Library/Application Support/message-board/agent/state.db").expanduser()
                  if sys.platform == "darwin" else Path("~/.local/state/message-board/agent/state.db").expanduser()]
    return next((c for c in candidates if c.is_file()), None)


def start() -> None:
    state_dir = Path(ext.node.get("state_dir") or ".").resolve()
    work_dir = Path(ext.node.get("work_dir") or state_dir / "work")
    state = State(state_dir / "state.db")
    old = python_node_state(state_dir)
    taken = {"found": False}
    if old is not None and old.resolve() != (state_dir / "state.db").resolve():
        try:
            taken = state.take_over(old)
            if taken["found"]:
                log(f"took over {taken['files']} files from {old}")
        except Exception as e:  # noqa: BLE001 - a bad old file mustn't stop the dailies
            taken = {"from": str(old), "error": str(e)}
            log(f"couldn't take over {old}: {e}")
    cfg = Config.from_settings(ext.settings, host=str(ext.node.get("machine") or ""))
    node = Node(ext.request)
    publisher = Publisher(cfg, node, state, work_dir / "jobs")

    def event(status: dict) -> None:
        ext.event("dailies.status", {**status, "takeover": taken, "allowed": inside_roots(cfg.root)})

    d = Dailies(cfg, state, node, publisher, event=event)
    now.update(dailies=d, state=state)
    d.start()


def inside_roots(root: Path) -> bool | None:
    """Whether the node lets files under `root` be published (its permissions.folders), or None if
    it didn't say which folders those are."""
    if not ext.node.get("roots"):
        return None
    try:
        ext.allowed(root)
        return True
    except PermissionError:
        return False


@ext.on_start
def started() -> None:
    try:
        start()
    finally:
        ready.set()


@ext.on_settings
def settings_changed(_settings: dict) -> None:
    """New settings (another root, other ignore patterns): start again with them. A publish in
    progress finishes on its old worker."""
    old = now.get("dailies")
    if old is not None:
        old.close()
    start()


def running() -> Dailies:
    if not ready.wait(60) or "dailies" not in now:
        raise RuntimeError("the dailies haven't started (the extension's log says why)")
    return now["dailies"]


def request_id(job) -> int | None:
    """The board's request this command is for: given by the node, or its id (`req-<n>`)."""
    rid = job.params.get("request_id")
    if rid:
        return int(rid)
    m = re.fullmatch(r"req-(\d+)", str(job.id))
    return int(m.group(1)) if m else None


@ext.command("dailies.publish")
def publish(job):
    return running().publish(job.params, request_id=request_id(job),
                             report=lambda stage, frac: job.progress(frac, stage))


@ext.command("dailies.rescan")
def rescan(job):
    return running().rescan()


@ext.method("dailies.syncs_changed")
def syncs_changed(_params: dict) -> dict:
    d = running()
    d.syncs_now.set()
    d.kick.set()
    return {}


ext.run()
d = now.get("dailies")
if d is not None:
    d.close()
