"""HTML review pages (the A/B audio pages, edit reviews, ...) load their media by relative path.
Publishing one means bundling the page with every local file it mentions, and rewriting paths that
point outside its folder so they still resolve under the page's signed URL on the server.
From the Python node's agent/pages.py."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from . import media
from .config import kind_of
from .media import Build, OutFile, Progress

EXT = r"wav|mp3|m4a|aac|flac|ogg|opus|mp4|mov|m4v|webm|png|jpe?g|gif|webp|svg|json|css|js|txt|vtt|csv|pdf"
# quoted strings may contain spaces and brackets ("2-current (broadcast).wav"); CSS url() may be bare
QUOTED = re.compile(r"""(["'`])([^"'`<>\n]*?\.(?:%s))(?:[?#][^"'`<>\n]*)?\1""" % EXT, re.IGNORECASE)
CSS_URL = re.compile(r"""url\(\s*([^)"'\s]+?\.(?:%s))(?:[?#][^)\s]*)?\s*\)""" % EXT, re.IGNORECASE)
HEAVY_AUDIO = 20 * 1024 * 1024
HEAVY_VIDEO = 50 * 1024 * 1024


def references(html: str) -> list[str]:
    found = [(m.start(), m.group(2)) for m in QUOTED.finditer(html)]
    found += [(m.start(), m.group(1)) for m in CSS_URL.finditer(html)]
    refs = []
    for _, ref in sorted(found):
        ref = ref.strip()
        external = ref.startswith(("http:", "https:", "data:", "//", "blob:", "mailto:"))
        fragment = ref.startswith(".") and not ref.startswith(("./", "../"))   # e.g. "x" + ".wav"
        if not ref or external or fragment:
            continue
        if ref not in refs:
            refs.append(ref)
    return refs


def owned_files(page: Path) -> set[Path]:
    """The local files a page uses (so they aren't also published on their own)."""
    try:
        html = page.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    out = set()
    for ref in references(html):
        target = (page.parent / ref.split("?")[0]).resolve()
        if target.is_file():
            out.add(target)
    return out


def _rewrite(html: str, mapping: dict[str, str]) -> str:
    """Replace exactly the matched references (not every substring that looks alike:
    rewriting `mix.wav` mustn't touch `old-mix.wav`)."""
    def sub(pattern: re.Pattern, group: int, text: str) -> str:
        def repl(m: re.Match) -> str:
            new = mapping.get(m.group(group).strip())
            if not new:
                return m.group(0)
            a, b = m.span(group)
            return m.group(0)[: a - m.start()] + new + m.group(0)[b - m.start():]
        return pattern.sub(repl, text)
    return sub(CSS_URL, 1, sub(QUOTED, 2, html))


def build_page(src: Path, root: Path, work: Path, progress: Progress) -> Build:
    """The page with the local files it mentions, and a thumb from Quick Look (macOS)."""
    progress("page", None)
    html = src.read_text(encoding="utf-8", errors="replace")
    root = root.resolve()
    files: list[OutFile] = []
    rewrites: dict[str, str] = {}
    bundled: set[str] = set()
    total = 0
    for ref in references(html):
        target = (src.parent / ref.split("?")[0]).resolve()
        if not target.is_file() or not target.is_relative_to(root):
            continue
        if target.is_relative_to(src.parent.resolve()):
            bundle = target.relative_to(src.parent.resolve()).as_posix()
        else:
            bundle = "_up/" + target.relative_to(root).as_posix()
        size = target.stat().st_size
        kind = kind_of(target)
        path = target
        if kind == "audio" and size > HEAVY_AUDIO and target.suffix.lower() in (".wav", ".aif", ".aiff", ".flac"):
            bundle = bundle.rsplit(".", 1)[0] + ".m4a"
            path = work / "bundle" / bundle   # mirrors the bundle path, so same-named files don't collide
            path.parent.mkdir(parents=True, exist_ok=True)
            codec = "aac_at" if "aac_at" in media.encoders() else "aac"
            subprocess.run(["ffmpeg", "-hide_banner", "-y", "-i", str(target), "-c:a", codec, "-b:a", "192k",
                            str(path)], capture_output=True, check=True, stdin=subprocess.DEVNULL)
        elif kind == "video" and size > HEAVY_VIDEO:
            bundle = bundle.rsplit(".", 1)[0] + ".mp4"
            out_dir = work / "bundle-video" / bundle
            out_dir.mkdir(parents=True, exist_ok=True)
            proxy = media.build_video(target, out_dir, progress, height=720, bitrate="2500k")
            path = proxy.files[0].path
        if bundle in bundled:
            continue
        bundled.add(bundle)
        if bundle != ref.split("?")[0]:
            rewrites[ref] = bundle
        files.append(OutFile("page_file", path, path.name, media.mime_of(path), rel=bundle))
        total += path.stat().st_size
    html = _rewrite(html, rewrites)
    page = work / src.name
    page.write_text(html, encoding="utf-8")
    files.insert(0, OutFile("page", page, src.name, "text/html", rel=src.name))
    if thumb := _snapshot(page, work):
        files.append(thumb)
    title = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    meta = {"title": title.group(1).strip() if title else None, "files": len(files) - 1, "bytes": total}
    return Build("page", files, meta)


def _snapshot(page: Path, work: Path) -> OutFile | None:
    """Quick Look renders HTML with WebKit; good enough for a thumbnail. None off macOS."""
    if not media.MAC:
        return None
    try:
        subprocess.run(["qlmanage", "-t", "-s", "1200", "-o", str(work), str(page)], capture_output=True,
                       timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    png = work / (page.name + ".png")
    if not png.exists():
        return None
    try:
        # Quick Look renders a square; the top of a page is what identifies it
        return media.save_thumb(png, work / "thumb.jpg", crop_top=0.625)
    except RuntimeError:
        return None
