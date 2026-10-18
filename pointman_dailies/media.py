"""Turn a file on disk into what the phone needs: small, streamable proxies, posters and thumbnails.
From the Python node's agent/media.py, with ffmpeg (and on macOS sips and Quick Look) in place of
Pillow, so the extension needs nothing beyond the standard library. Everything is written into a
scratch folder; the caller publishes it and deletes the folder."""

from __future__ import annotations

import json
import mimetypes
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from .config import kind_of

Progress = Callable[[str, float | None], None]

THUMB = 640
POSTER = 1600
IMAGE_MAX = 4096
IMAGE_PASSTHROUGH_BYTES = 15 * 1024 * 1024
DARK = "0x1c1c21"           # (28, 28, 33): what transparent parts of a thumb sit on
MAC = sys.platform == "darwin"


@dataclass
class OutFile:
    role: str
    path: Path
    name: str
    mime: str
    rel: str | None = None          # page files: path relative to the page
    width: int | None = None
    height: int | None = None
    duration: float | None = None


@dataclass
class Build:
    kind: str
    files: list[OutFile]
    meta: dict = field(default_factory=dict)


def mime_of(path: Path | str) -> str:
    ext = Path(path).suffix.lower()
    return {".m4a": "audio/mp4", ".usdz": "model/vnd.usdz+zip", ".md": "text/markdown",
            ".wav": "audio/wav", ".mov": "video/quicktime", ".mp4": "video/mp4", ".glb": "model/gltf-binary",
            ".heic": "image/heic", ".webp": "image/webp", ".psd": "image/vnd.adobe.photoshop",
            }.get(ext) or mimetypes.guess_type(str(path))[0] or "application/octet-stream"


def run(cmd: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess:
    """Run a tool, its output captured: stdout is the node's line to us, so nothing may print there."""
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                       stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        tail = (r.stderr or r.stdout).strip().splitlines()[-6:]
        raise RuntimeError(f"{Path(cmd[0]).name} failed: " + " | ".join(tail))
    return r


@cache
def encoders() -> str:
    return subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", stdin=subprocess.DEVNULL).stdout


def probe(path: Path) -> dict:
    r = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)])
    return json.loads(r.stdout)


def _fps(stream: dict) -> float | None:
    num, _, den = (stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "0/0").partition("/")
    try:
        return round(float(num) / float(den), 3) if float(den) else None
    except ValueError:
        return None


def _rotation(stream: dict) -> int:
    for sd in stream.get("side_data_list", []) or []:
        if "rotation" in sd:
            return abs(int(float(sd["rotation"]))) % 180
    return abs(int(stream.get("tags", {}).get("rotate", 0))) % 180


def _bitrate_bps(value: str) -> int:
    value = value.strip().lower()
    mult = {"k": 1_000, "m": 1_000_000}.get(value[-1], 1)
    return int(float(value.rstrip("km")) * mult)


def ffmpeg_with_progress(cmd: list[str], duration: float | None, progress: Progress, stage: str) -> None:
    """Run ffmpeg, reporting how far through the source it is."""
    full = ["ffmpeg", "-hide_banner", "-nostats", "-y", "-progress", "pipe:1", *cmd]
    proc = subprocess.Popen(full, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True,
                            encoding="utf-8", errors="replace")
    last = -1.0
    assert proc.stdout
    err: list[str] = []
    t = threading.Thread(target=lambda: err.extend(proc.stderr), daemon=True)   # a full stderr pipe stalls ffmpeg
    t.start()
    for line in proc.stdout:
        if line.startswith("out_time_us=") and duration:
            try:
                frac = min(1.0, int(line.split("=", 1)[1]) / 1e6 / duration)
            except ValueError:
                continue
            if frac - last >= 0.01:
                last = frac
                progress(stage, frac)
    code = proc.wait()
    t.join(5)
    if code != 0:
        raise RuntimeError("ffmpeg failed: " + " | ".join("".join(err).strip().splitlines()[-6:]))


# ---------------------------------------------------------------- pictures


@dataclass
class Picture:
    width: int            # as shown, after any EXIF rotation
    height: int
    alpha: bool


def picture(path: Path) -> Picture:
    """An image's size as shown (ffmpeg turns it by its EXIF orientation, as Pillow's exif_transpose did)."""
    r = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_frames", "-read_intervals", "%+#1",
             "-print_format", "json", str(path)])
    frames = json.loads(r.stdout).get("frames") or []
    if not frames:
        raise RuntimeError(f"can't read {path.name} as a picture")
    f = frames[0]
    w, h = int(f["width"]), int(f["height"])
    if _rotation(f) == 90:
        w, h = h, w
    fmt = str(f.get("pix_fmt") or "")
    alpha = fmt.startswith(("rgba", "bgra", "argb", "abgr", "ya", "yuva", "gbrap")) or fmt == "pal8"
    return Picture(w, h, alpha)


def fit(w: int, h: int, box: int) -> tuple[int, int]:
    """Within box x box, never larger than it was (Pillow's thumbnail)."""
    if max(w, h) <= box:
        return w, h
    if w >= h:
        return box, max(1, round(h * box / w))
    return max(1, round(w * box / h)), box


def render(src: Path, dest: Path, *, box: int, flatten: bool = True, quality: int = 4,
           crop_top: float | None = None) -> OutFile:
    """One frame of `src` as a JPEG (or PNG, by dest's suffix) within box x box. JPEGs have any
    transparency laid on the dark background; `crop_top` keeps only the top (height = width x it)."""
    pic = picture(src)
    w, h = pic.width, pic.height
    pre = ""
    if crop_top:
        h = min(h, int(w * crop_top))
        pre = f"crop={w}:{h}:0:0,"
    tw, th = fit(w, h, box)
    scale = f"{pre}scale={tw}:{th}:flags=lanczos"
    png = dest.suffix.lower() == ".png"
    if png or not (pic.alpha and flatten):
        args = ["-vf", scale + ("" if png else ",format=yuvj420p")]
    else:
        args = ["-filter_complex", f"color=c={DARK}:s={tw}x{th}[bg];[0:v]{scale},format=rgba[fg];"
                                   f"[bg][fg]overlay=shortest=1,format=yuvj420p"]
    quality_args = [] if png else ["-q:v", str(quality)]
    run(["ffmpeg", "-hide_banner", "-v", "error", "-y", "-i", str(src), "-frames:v", "1", *args, *quality_args,
         "-update", "1", str(dest)], timeout=300)
    return OutFile("thumb", dest, dest.name, mime_of(dest), width=tw, height=th)


def save_thumb(src: Path, dest: Path, size: int = THUMB, *, crop_top: float | None = None) -> OutFile:
    return render(src, dest, box=size, crop_top=crop_top)


def rasterize(src: Path, work: Path) -> Path:
    """Formats ffmpeg can't read well (PSD, HEIC, EXR, AI, SVG, PDF) go through macOS's own converters."""
    ext = src.suffix.lower()
    if MAC and ext in (".psd", ".heic", ".heif", ".tif", ".tiff", ".bmp", ".exr"):
        out = work / (src.stem + "-flat.png")
        run(["sips", "-s", "format", "png", str(src), "--out", str(out)], timeout=300)
        return out
    if ext in (".ai", ".svg", ".pdf"):
        if MAC:
            run(["qlmanage", "-t", "-s", str(IMAGE_MAX // 2), "-o", str(work), str(src)], timeout=120)
            out = work / (src.name + ".png")
        elif ext == ".pdf" and shutil.which("pdftoppm"):
            run(["pdftoppm", "-png", "-singlefile", "-scale-to", str(IMAGE_MAX // 2), str(src),
                 str(work / src.stem)], timeout=120)
            out = work / (src.stem + ".png")
        else:
            raise RuntimeError(f"can't render {ext} files on this machine")
        if not out.exists():
            raise RuntimeError(f"couldn't render {src.name}")
        return out
    return src


def build_image(src: Path, work: Path, progress: Progress) -> Build:
    progress("image", None)
    raster = rasterize(src, work)
    pic = picture(raster)
    w, h = pic.width, pic.height
    files = []
    passthrough = (raster == src and src.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".gif")
                   and src.stat().st_size <= IMAGE_PASSTHROUGH_BYTES and max(w, h) <= IMAGE_MAX)
    if passthrough:
        files.append(OutFile("image", src, src.name, mime_of(src), width=w, height=h))
    else:
        # a fixed name, so a source called thumb.* can't collide with the thumbnail
        dest = work / ("display.png" if pic.alpha else "display.jpg")
        big = render(raster, dest, box=IMAGE_MAX, flatten=False, quality=2)
        files.append(OutFile("image", dest, src.stem + dest.suffix, mime_of(dest), width=big.width, height=big.height))
    files.append(save_thumb(raster, work / "thumb.jpg"))
    meta = {"width": w, "height": h, "format": src.suffix.lower().lstrip("."), "bytes": src.stat().st_size}
    return Build("image", files, meta)


# ---------------------------------------------------------------- video


def frame_at(src: Path, t: float, dest: Path, width: int) -> OutFile:
    run(["ffmpeg", "-hide_banner", "-y", "-ss", f"{t:.3f}", "-i", str(src), "-frames:v", "1",
         "-vf", f"scale='min({width},iw)':-2", "-q:v", "3", "-update", "1", str(dest)], timeout=120)
    pic = picture(dest)
    return OutFile("poster", dest, dest.name, "image/jpeg", width=pic.width, height=pic.height)


def build_video(src: Path, work: Path, progress: Progress, *, height: int = 1080, bitrate: str = "4M",
                audio_bitrate: str = "160k", name: str | None = None) -> Build:
    info = probe(src)
    v = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    if v is None:
        raise RuntimeError("no video stream")
    duration = float(info["format"].get("duration") or v.get("duration") or 0)
    w, h = int(v["width"]), int(v["height"])
    if _rotation(v) == 90:
        w, h = h, w
    fps = _fps(v)
    total_bps = int(info["format"].get("bit_rate") or 0)
    meta = {"duration": duration, "width": w, "height": h, "fps": fps, "codec": v.get("codec_name"),
            "bitrate": total_bps, "bytes": src.stat().st_size, "audio": bool(a),
            "container": src.suffix.lower().lstrip(".")}

    # poster from a tenth of the way in, capped, so it's past any black intro
    t = min(duration * 0.1, 30.0) if duration > 1 else 0
    poster = frame_at(src, t, work / "poster.jpg", POSTER)
    thumb = save_thumb(poster.path, work / "thumb.jpg")

    out = work / f"{Path(name or src.name).stem}.mp4"
    target = _bitrate_bps(bitrate)
    short_side = min(w, h)
    light = (v.get("codec_name") in ("h264", "hevc") and v.get("pix_fmt") in ("yuv420p", "yuvj420p")
             and total_bps and total_bps <= target * 1.25 and short_side <= 2160
             and (a is None or a.get("codec_name") in ("aac", "mp3")))
    if light:
        # already phone-friendly: just move the index to the front so it streams
        tag = ["-tag:v", "hvc1"] if v.get("codec_name") == "hevc" else []
        ffmpeg_with_progress(["-i", str(src), "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", *tag,
                              "-movflags", "+faststart", str(out)], duration, progress, "remux")
        meta["proxy"] = "remux"
    else:
        if short_side > height:
            scale = f"scale=-2:{height}" if w >= h else f"scale={height}:-2"
        else:
            scale = "scale=trunc(iw/2)*2:trunc(ih/2)*2"
        vf = f"{scale},format=yuv420p"
        if fps and fps > 60:
            vf += ",fps=60"
        if "hevc_videotoolbox" in encoders():
            vcodec = ["-c:v", "hevc_videotoolbox", "-b:v", bitrate, "-tag:v", "hvc1", "-allow_sw", "1"]
        else:
            vcodec = ["-c:v", "libx264", "-preset", "veryfast", "-b:v", bitrate]
        acodec = ["-c:a", "aac_at" if "aac_at" in encoders() else "aac", "-b:a", audio_bitrate, "-ac", "2"]
        ffmpeg_with_progress(["-i", str(src), "-map", "0:v:0", "-map", "0:a:0?", "-vf", vf, *vcodec, *acodec,
                              "-movflags", "+faststart", str(out)], duration, progress, "proxy")
        meta["proxy"] = f"hevc {height}p {bitrate}"
    pinfo = probe(out)
    pv = next(s for s in pinfo["streams"] if s["codec_type"] == "video")
    proxy = OutFile("video", out, out.name, "video/mp4", width=int(pv["width"]), height=int(pv["height"]),
                    duration=float(pinfo["format"].get("duration") or duration))
    return Build("video", [proxy, poster, thumb], meta)


# ---------------------------------------------------------------- audio


def waveform(src: Path, dest: Path, width: int = 1600, height: int = 240) -> OutFile:
    run(["ffmpeg", "-hide_banner", "-y", "-i", str(src), "-filter_complex",
         f"aformat=channel_layouts=mono,showwavespic=s={width}x{height}:colors=#9f8bff:scale=sqrt",
         "-frames:v", "1", "-update", "1", str(dest)], timeout=600)
    return OutFile("waveform", dest, dest.name, "image/png", width=width, height=height)


def build_audio(src: Path, work: Path, progress: Progress, *, name: str | None = None) -> Build:
    info = probe(src)
    a = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    if a is None:
        raise RuntimeError("no audio stream")
    duration = float(info["format"].get("duration") or 0)
    meta = {"duration": duration, "codec": a.get("codec_name"), "sample_rate": int(a.get("sample_rate") or 0),
            "channels": a.get("channels"), "bytes": src.stat().st_size,
            "container": src.suffix.lower().lstrip(".")}
    progress("waveform", None)
    wave = waveform(src, work / "waveform.png")
    files = [wave, OutFile("thumb", wave.path, "waveform.png", "image/png", width=wave.width, height=wave.height)]
    if a.get("codec_name") in ("aac", "mp3") and src.stat().st_size < 60 * 1024 * 1024:
        files.insert(0, OutFile("audio", src, src.name, mime_of(src), duration=duration))
    else:
        out = work / f"{Path(name or src.name).stem}.m4a"
        codec = "aac_at" if "aac_at" in encoders() else "aac"
        ffmpeg_with_progress(["-i", str(src), "-map", "0:a:0", "-c:a", codec, "-b:a", "192k",
                              "-movflags", "+faststart", str(out)], duration, progress, "audio")
        files.insert(0, OutFile("audio", out, out.name, "audio/mp4", duration=duration))
    return Build("audio", files, meta)


# ---------------------------------------------------------------- documents


def build_document(src: Path, work: Path, progress: Progress) -> Build:
    progress("document", None)
    files = [OutFile("document", src, src.name, mime_of(src))]
    meta = {"bytes": src.stat().st_size, "format": src.suffix.lower().lstrip(".")}
    if src.suffix.lower() == ".pdf":
        try:
            files.append(save_thumb(rasterize(src, work), work / "thumb.jpg"))
        except RuntimeError:
            pass
    return Build("document", files, meta)


def build_file(src: Path, progress: Progress, work: Path | None = None) -> Build:
    """Any file exactly as it is (share_file as_is): the original bytes, e.g. camera footage or an
    archive for another machine. A video also gets what the phone plays (a proxy, poster and thumb,
    as build_video makes) beside the original: otherwise a ProRes render shared as it is shows as a
    file nobody can watch. A PDF is a document, the original with a page's thumb, so the apps preview it."""
    original = OutFile("file", src, src.name, mime_of(src))
    meta = {"bytes": src.stat().st_size, "format": src.suffix.lower().lstrip(".")}
    if work is not None and kind_of(src) == "video":
        try:
            video = build_video(src, work, progress)
            return Build("video", [*video.files, original], {**video.meta, "as_is": True})
        except Exception as e:  # noqa: BLE001 - no playable copy: the original alone
            print(f"as_is {src.name}: no playable copy ({e}); sharing the original alone", file=sys.stderr, flush=True)
    if work is not None and src.suffix.lower() == ".pdf":
        # a PDF is posted as it is anyway (a document's file is the original, plus a page's thumb), so
        # as_is costs it nothing, and as "other" the apps couldn't show it
        doc = build_document(src, work, progress)
        return Build("document", doc.files, {**doc.meta, "as_is": True})
    progress("file", None)
    return Build("other", [original], meta)
