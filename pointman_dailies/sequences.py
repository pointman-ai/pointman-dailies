"""Numbered image sequences (Blender render passes, simulation frames, ...).

`Set-0001.png ... Set-0050.png` is one thing to review, not fifty. Frames that belong to a sequence
are published together as a single video asset whose path uses `#` for the frame number:
`scene/work/seq-a/Set-####.png`. From the Python node's agent/sequences.py.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import media
from .media import Build, Progress

FRAME = re.compile(r"^(?P<prefix>.*?)(?P<num>\d{3,})\.(?P<ext>png|jpe?g|exr|tiff?|webp)$", re.IGNORECASE)
MIN_FRAMES = 8


def key_for(rel: str) -> tuple[str, re.Match] | None:
    name = rel.rsplit("/", 1)[-1]
    m = FRAME.match(name)
    if not m:
        return None
    folder = rel[: -len(name)]
    return f"{folder}{m['prefix']}{'#' * len(m['num'])}.{m['ext']}", m


def frames(root: Path, key: str, listing: list[Path] | None = None) -> list[Path]:
    """The frames on disk for a sequence key, in order. Pass `listing` (the folder's entries) to
    avoid listing the folder again."""
    folder, _, pattern = key.rpartition("/")
    prefix = pattern.split("#", 1)[0]
    digits = pattern.count("#")
    ext = pattern.rsplit(".", 1)[-1]
    rx = re.compile(re.escape(prefix) + r"(\d{%d})\.%s$" % (digits, re.escape(ext)), re.IGNORECASE)
    d = root / folder if folder else root
    try:
        entries = listing if listing is not None else list(d.iterdir())
    except OSError:
        return []
    found = [(int(m.group(1)), p) for p in entries if (m := rx.match(p.name))]
    return [p for _, p in sorted(found)]


def _vcodec(bitrate: str) -> list[str]:
    return (["-c:v", "hevc_videotoolbox", "-b:v", bitrate, "-tag:v", "hvc1", "-allow_sw", "1"]
            if "hevc_videotoolbox" in media.encoders() else ["-c:v", "libx264", "-crf", "20"])


def encode_frames(files: list[Path], out: Path, progress: Progress, *, fps: float = 24, height: int = 1080,
                  bitrate: str = "6M") -> Path:
    """Frames, in order, as one video (at most `height` on the short side)."""
    listing = out.with_suffix(".txt")

    def quoted(p: Path) -> str:
        return "'" + p.as_posix().replace("'", "'\\''") + "'"

    listing.write_text("".join(f"file {quoted(p)}\nduration {1 / fps:.6f}\n" for p in files)
                       + f"file {quoted(files[-1])}\n", encoding="utf-8")
    scale = f"scale='if(gt(iw,ih),-2,min({height},iw))':'if(gt(iw,ih),min({height},ih),-2)',format=yuv420p"
    # EXR frames are linear light (Blender's passes): shown through the sRGB curve, like any viewer does
    linear = ["-apply_trc", "iec61966_2_1"] if files[0].suffix.lower() == ".exr" else []
    media.ffmpeg_with_progress(["-f", "concat", "-safe", "0", *linear, "-i", str(listing), "-vf", scale,
                                "-r", str(fps), *_vcodec(bitrate), "-movflags", "+faststart", str(out)],
                               len(files) / fps, progress, "sequence")
    return out


def build_sequence(root: Path, key: str, work: Path, progress: Progress, *, fps: float = 24,
                   height: int = 1080, bitrate: str = "6M") -> Build:
    files = frames(root, key)
    if len(files) < 2:
        raise RuntimeError("sequence has fewer than two frames")
    out = work / (key.rsplit("/", 1)[-1].replace("#", "").rstrip("-_.") or "sequence").rsplit(".", 1)[0]
    out = encode_frames(files, out.with_suffix(".mp4"), progress, fps=fps, height=height, bitrate=bitrate)
    duration = len(files) / fps
    mid = media.rasterize(files[len(files) // 2], work)
    pic = media.picture(mid)
    poster = media.render(mid, work / "poster.jpg", box=media.POSTER, quality=3)
    poster.role = "poster"
    thumb = media.save_thumb(mid, work / "thumb.jpg")
    info = media.probe(out)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    proxy = media.OutFile("video", out, out.name, "video/mp4", width=int(v["width"]), height=int(v["height"]),
                          duration=duration)
    first, last = (FRAME.match(files[0].name)["num"], FRAME.match(files[-1].name)["num"])
    meta = {"duration": duration, "frames": len(files), "first_frame": int(first), "last_frame": int(last),
            "fps": fps, "width": pic.width, "height": pic.height, "sequence": True,
            "bytes": sum(p.stat().st_size for p in files)}
    return Build("video", [proxy, poster, thumb], meta)


def quiet_window(mtimes: list[float], minimum: float) -> float:
    """How long a sequence must go without new frames before it counts as finished: at least
    `minimum`, and three times the usual gap between frames, so a slow render (a minute or more per
    frame) isn't published after every frame."""
    ts = sorted(mtimes)[-21:]
    gaps = sorted(b - a for a, b in zip(ts, ts[1:]) if b > a)
    typical = gaps[len(gaps) // 2] if gaps else 0.0
    return max(minimum, 3 * typical)
