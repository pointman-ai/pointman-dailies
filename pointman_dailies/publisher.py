"""Build a file's proxies and have the node publish them (files.publish): it uploads each file and
sends core /v1/node/publish. From the Python node's agent/publisher.py."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

from . import media, pages, sequences
from .config import Config, kind_of
from .media import Build
from .state import State

Report = Callable[[str, float | None], None]
PUBLISH_WAIT = 6 * 3600       # a long video over a slow line: the node uploads it in parts


def _quiet(stage: str, frac: float | None) -> None:
    pass


class CoreError(RuntimeError):
    pass


class Node:
    """What the node does for us: it holds the token, so every call to core goes through it."""

    def __init__(self, request: Callable[..., dict]):
        self._request = request          # Extension.request

    def publish(self, body: dict) -> dict:
        return self._request("files.publish", {"body": body}, PUBLISH_WAIT)

    def call(self, method: str, path: str, body=None, *, gzip: bool = False, timeout: float = 60):
        params = {"method": method, "path": path}
        if body is not None:
            params["body"] = body
        if gzip:
            params["gzip"] = True
        r = self._request("core.call", params, timeout) or {}
        status = int(r.get("status") or 0)
        if status >= 400:
            raise CoreError(f"{method} {path} -> {status}: {str(r.get('body'))[:300]}")
        return r.get("body")


class Publisher:
    def __init__(self, cfg: Config, node: Node, state: State, work_root: Path):
        self.cfg, self.node, self.state, self.work_root = cfg, node, state, work_root
        work_root.mkdir(parents=True, exist_ok=True)

    def project_for(self, rel: str) -> dict:
        parts = rel.split("/")
        slug = parts[0] if len(parts) > 1 else "misc"
        return {"slug": slug, "name": slug}

    def workdir(self) -> Path:
        return Path(tempfile.mkdtemp(prefix="job-", dir=self.work_root))

    def check_disk(self) -> None:
        free = shutil.disk_usage(self.work_root).free / 1e9
        if free < self.cfg.min_free_gb:
            raise RuntimeError(f"only {free:.0f} GB free (min_free_gb = {self.cfg.min_free_gb:g}); "
                               "skipping so the disk doesn't fill")

    def build(self, src: Path, rel: str, work: Path, report: Report) -> Build:
        kind = kind_of(src)
        if kind == "video":
            self.check_disk()
            p = self.cfg.video_profile(rel)
            return media.build_video(src, work, report, height=p.height, bitrate=p.bitrate,
                                     audio_bitrate=p.audio_bitrate)
        if kind == "image":
            return media.build_image(src, work, report)
        if kind == "audio":
            return media.build_audio(src, work, report)
        if kind == "page":
            return pages.build_page(src, self.cfg.root, work, report)
        if kind == "document":
            return media.build_document(src, work, report)
        if kind == "model":
            raise RuntimeError("3D files are the Blender extension's to preview (blender.preview); "
                               "as_is shares the file itself")
        raise RuntimeError(f"don't know how to publish {src.suffix} files")

    def publish_build(self, build: Build, *, project: dict, asset_path: str, title: str, kind: str | None = None,
                      caption: str | None = None, by: str | None = None, source: dict | None = None,
                      report: Report = _quiet, post: dict | None = None) -> dict:
        files = [{"role": f.role, "file": str(f.path), "name": f.name, "path": f.rel, "mime": f.mime,
                  "size": f.path.stat().st_size, "width": f.width, "height": f.height, "duration": f.duration}
                 for f in build.files]
        report("upload", None)
        return self.node.publish({
            "project": project, "asset": {"path": asset_path, "title": title, "kind": kind or build.kind},
            "caption": caption, "source": source, "meta": build.meta, "files": files, "by": by, "post": post,
        })

    # ------------------------------------------------------------ files under root

    def publish_file(self, rel: str, *, caption: str | None = None, by: str | None = None,
                     report: Report = _quiet, post: dict | None = None, as_is: bool = False) -> dict:
        """post: {"request_id": n} for a file someone shared on the board, so core posts it where
        they asked, as them; {"sync_id": n} for a synced folder's change."""
        src = self.cfg.root / rel
        st = src.stat()
        self.state.mark(rel, st.st_size, st.st_mtime, "processing")
        work = self.workdir()
        try:
            build = media.build_file(src, report, work) if as_is else self.build(src, rel, work, report)
            out = self.publish_build(
                build, project=self.project_for(rel), asset_path=rel, title=src.name, caption=caption, by=by,
                source={"path": rel, "size": st.st_size, "mtime": st.st_mtime, "host": self.cfg.host},
                report=report, post=post)
        except Exception as e:
            self.state.mark(rel, st.st_size, st.st_mtime, "failed", error=str(e)[:500], bump_attempts=True)
            raise
        finally:
            shutil.rmtree(work, ignore_errors=True)
        self.state.mark(rel, st.st_size, st.st_mtime, "published", asset_id=out.get("asset_id"),
                        version_id=out.get("version_id"))
        return out

    # ------------------------------------------------------------ image sequences

    def publish_sequence(self, key: str, *, caption: str | None = None, by: str | None = None,
                         report: Report = _quiet, post: dict | None = None) -> dict | None:
        """Publish a numbered frame sequence as one video. None if nothing changed since the last
        time (several frames settling at once all point here)."""
        frames = sequences.frames(self.cfg.root, key)
        if not frames:
            return None
        count, newest = len(frames), max(p.stat().st_mtime for p in frames)
        row = self.state.file(key)
        if row and row["status"] == "published" and (row["size"], row["mtime"]) == (count, newest):
            return None
        self.state.mark(key, count, newest, "processing")
        work = self.workdir()
        try:
            self.check_disk()
            p = self.cfg.video_profile(key)
            build = sequences.build_sequence(self.cfg.root, key, work, report, height=p.height)
            out = self.publish_build(
                build, project=self.project_for(key), asset_path=key, title=key.rsplit("/", 1)[-1],
                caption=caption, by=by, source={"path": key, "frames": count, "mtime": newest, "host": self.cfg.host},
                report=report, post=post)
        except Exception as e:
            self.state.mark(key, count, newest, "failed", error=str(e)[:500], bump_attempts=True)
            raise
        finally:
            shutil.rmtree(work, ignore_errors=True)
        self.state.mark(key, count, newest, "published", asset_id=out.get("asset_id"),
                        version_id=out.get("version_id"))
        # frames published one by one before the sequence formed: remove them from the board
        for frame in frames:
            rel = frame.relative_to(self.cfg.root).as_posix()
            row = self.state.file(rel)
            if row and row["status"] == "published" and row["asset_id"]:
                try:
                    self.node.call("DELETE", f"/v1/node/assets/{row['asset_id']}")
                except Exception:  # noqa: BLE001 - leaving a stray frame is harmless
                    continue
            if row:
                self.state.mark(rel, row["size"], row["mtime"], "skipped", error=f"frame of {key}")
        return out
