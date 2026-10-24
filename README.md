# dailies

A Pointman extension that publishes a folder's work as it settles. It watches a folder on a node, waits
for each new or changed file to stop changing, and posts it in the thread of the sync that covers it:
video as a playable proxy, plus images, audio, PDFs, HTML pages with the files they load, and frame
sequences as one video. Comments on what it posts are written back into each project's folder as a
notes file, so a session working there can read them.

Python (standard library only), run by the node on Python 3.12.

```
pointman add dailies
```

## What it needs

- **ffmpeg** and **ffprobe** on the PATH, for video proxies, posters, audio and frame sequences.
- **On macOS,** `sips` and Quick Look (built in) for images and documents. Elsewhere, `pdftoppm` (poppler)
  for a PDF's picture, if it's there.
- macOS (Apple silicon) or Linux (x86-64 or arm64).

## What it does

- **Watches** `root`: FSEvents on macOS, elsewhere a poll of the folders' modified times, plus a full
  scan every `scan_interval`.
- **Settles:** a file is published once its size and modified time have held still for
  `stable_seconds`, so recordings and renders in progress are left alone. Eight or more numbered frames
  (`seq/shot-####.png`) wait for the render to pause and publish as one video. An HTML page is published
  with the files it loads.
- **Publishes only what a sync covers** (`create_sync` on the board), in that sync's thread. Nothing else
  publishes by itself. Ignored names and `rules` with `skip = true` never publish.
- **What it builds:** for a video, a proxy (HEVC on a Mac with VideoToolbox, H.264 elsewhere, at
  `video`'s or a rule's size and bitrate; a video that's already light is only remuxed) with a poster and
  thumbnail. Images in a form every app shows, audio as AAC with a waveform, a PDF with a picture of it,
  and pages with what they use. 3D files are left to the Blender extension.
- **Review notes:** each project's comments go to `<project>/review-notes.md` (`notes_file`). Notes at a
  timecode on a DaVinci Resolve timeline are left for the Resolve extension's markers.
- **The Files tab:** the folder's file list goes to the board after each scan that changed it.
- **Keeps the Mac awake** while there's publishing to do (`keep_awake`).
- **Keeps disk free:** it publishes nothing while less than `min_free_gb` is free.

It holds no token: files go to the board through the node, and so does every other call.

## Commands

Agents call them on a machine with `create_job`. `share_file` and `pointman push` of a file in the
folder come here too.

| Kind | Params | What |
|---|---|---|
| `dailies.publish` | `path`, `caption?`, `as_is?`, `channel_id?`, `thread_id?`, `title?`, `text?`, `by?`, `request_id?` | Publishes one file now, or a folder's files through settling. `path` is relative to `root`, or absolute inside it. `as_is`: the file exactly as it is (and a playable copy of a video). With `request_id`, it's posted where the board's request asked, as whoever asked |
| `dailies.rescan` | | A full scan, and the board's syncs again, now |

The node can also call the `dailies.syncs_changed` method when the board's syncs change, so they're read
at once rather than within 15 s.

## Status

`dailies.status`: the folder, what's waiting to settle, queued, being published, published and failed,
recent failures, the syncs, the last scan, the last error, whether it's watching, whether the node lets it
publish from `root`, and what it took over from an older node.

## Settings

Per machine, in `~/.config/pointman/extensions/dailies/config.toml`, or
`pointman config set node.extensions.dailies.<key> <value>`:

| Setting | Default | |
|---|---|---|
| `root` | `~/Documents/youtube` | The folder it watches and publishes from |
| `ignore` | dotfiles, `__pycache__`, `node_modules`, app bundles and project packages, Blender backups, `review-notes.md`… | File or folder names left alone anywhere in the folder (shell patterns) |
| `extra_ignore` | `[]` | More names to leave alone, on top of `ignore` |
| `stable_seconds` | 20 | Seconds a file's size and time must hold still before it's published |
| `scan_interval` | 300 | Seconds between full scans (30 at least); changes are seen sooner as they happen |
| `auto_documents` | `["pdf"]` | Of documents, only these extensions publish by themselves |
| `video` | `{height = 1080, bitrate = "4M", audio_bitrate = "160k"}` | The video proxies. `height` is the short side, so vertical videos get the same detail |
| `rules` | `[{match = "*/raw/*", video = {height = 720, bitrate = "2500k"}}]` | By path pattern: `skip = true` never publishes; `video` for smaller proxies |
| `notes_file` | `review-notes.md` | Where each project's comments are written. Empty: off |
| `min_free_gb` | 10 | Disk space to keep free |
| `light_workers` | 6 | Images, audio and pages published side by side (1–16; videos go one at a time) |
| `keep_awake` | `while_busy` | Keep the Mac awake: `while_busy`, `always` or `never` |
| `takeover` | | An older Python node's `state.db`, so what it already published isn't published again. Empty: where that node keeps it |

**The folder it may use:** the node lets it publish only from inside its `permissions.folders`,
`~/Documents/youtube`. So `root` must be that folder or one inside it.
