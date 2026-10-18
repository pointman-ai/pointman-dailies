"""The extension's settings (extension.toml's [settings], as the node gives them) and which files
are what. Copied from the Python node's agent/config.py, which is going away with it."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path

EXTENSIONS = {
    "video": {"mp4", "mov", "m4v", "mkv", "webm", "avi", "mts", "mxf"},
    "image": {"png", "jpg", "jpeg", "heic", "heif", "webp", "gif", "tif", "tiff", "psd", "bmp", "svg", "ai", "exr"},
    "audio": {"wav", "mp3", "m4a", "aac", "flac", "aif", "aiff", "ogg", "opus"},
    "model": {"blend", "glb", "gltf", "usdz", "usd", "usda", "usdc", "fbx", "obj"},
    "page": {"html", "htm"},
    "document": {"pdf", "md", "txt"},
}
KIND_OF = {ext: kind for kind, exts in EXTENSIONS.items() for ext in exts}


def kind_of(path: str | Path) -> str | None:
    return KIND_OF.get(Path(path).suffix.lower().lstrip("."))


@dataclass
class VideoProfile:
    height: int = 1080   # the proxy's short side, so vertical shorts get the same detail
    bitrate: str = "4M"
    audio_bitrate: str = "160k"


@dataclass
class Rule:
    match: str
    skip: bool = False
    video: VideoProfile | None = None


@dataclass
class Config:
    root: Path
    ignore: list[str]
    stable_seconds: float = 20
    scan_interval: float = 300
    auto_documents: set[str] = field(default_factory=lambda: {"pdf"})
    video: VideoProfile = field(default_factory=VideoProfile)
    rules: list[Rule] = field(default_factory=list)
    notes_file: str = "review-notes.md"
    min_free_gb: float = 10.0
    light_workers: int = 6
    keep_awake: str = "while_busy"
    host: str = ""

    @classmethod
    def from_settings(cls, s: dict, *, host: str = "") -> Config:
        video = _profile(s.get("video"), VideoProfile())
        return cls(root=Path(str(s.get("root") or "~/Documents/youtube")).expanduser(),
                   ignore=[*s.get("ignore", []), *s.get("extra_ignore", [])],
                   stable_seconds=float(s.get("stable_seconds", 20)),
                   scan_interval=float(s.get("scan_interval", 300)),
                   auto_documents={str(x).lower().lstrip(".") for x in s.get("auto_documents", ["pdf"])},
                   video=video,
                   rules=[Rule(match=r["match"], skip=bool(r.get("skip", False)),
                               video=_profile(r["video"], video) if r.get("video") else None)
                          for r in s.get("rules", [])],
                   notes_file=str(s.get("notes_file", "review-notes.md")),
                   min_free_gb=float(s.get("min_free_gb", 10)),
                   light_workers=max(1, int(s.get("light_workers", 6))),
                   keep_awake=str(s.get("keep_awake", "while_busy")), host=host)

    def ignored(self, rel: str) -> bool:
        return any(fnmatch.fnmatch(part, pat) for part in rel.split("/") for pat in self.ignore)

    def rule_for(self, rel: str) -> Rule | None:
        for rule in self.rules:
            if fnmatch.fnmatch(rel, rule.match):
                return rule
        return None

    def video_profile(self, rel: str) -> VideoProfile:
        rule = self.rule_for(rel)
        return rule.video if rule and rule.video else self.video

    def publishable(self, rel: str) -> bool:
        """A file this extension can turn into something reviewable (and isn't ignored or skipped).
        What's actually published by itself is decided by syncs. 3D files are the Blender
        extension's (blender.preview), so they're left out here."""
        if self.ignored(rel):
            return False
        rule = self.rule_for(rel)
        if rule and rule.skip:
            return False
        kind = kind_of(rel)
        if kind == "document":      # PDFs yes; notes, READMEs and the like stay out of threads
            return Path(rel).suffix.lower().lstrip(".") in self.auto_documents
        return kind is not None and kind != "model"


def _profile(d: dict | None, base: VideoProfile) -> VideoProfile:
    d = d or {}
    return VideoProfile(height=int(d.get("height", base.height)), bitrate=str(d.get("bitrate", base.bitrate)),
                        audio_bitrate=str(d.get("audio_bitrate", base.audio_bitrate)))
