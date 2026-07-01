"""
Post-record mux for GNOME Shell screencast + separate audio track.

Remuxes with ffmpeg (-c copy, no re-encode). Audio sidecar should be .m4a / .mp4
(AAC in MP4 container from audio.py).

Usage:
  uv run py/postmux.py video.mp4 audio.m4a -o out.mkv
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from py.settings import log

PRE_MUX_FLUSH_SEC = 0.5


def _verify_inputs(video: Path, audio: Path) -> None:
    if video.stat().st_size == 0:
        raise RuntimeError(f"video file is empty: {video.name}")
    if audio.stat().st_size == 0:
        raise RuntimeError(f"audio file is empty: {audio.name}")


def mux_av_files(video_path: str | Path, audio_path: str | Path, output_path: str | Path) -> str:
    """
    Mux video + audio into MKV or WebM using ffmpeg stream copy.

    Returns the absolute output path.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found on PATH")

    video = Path(video_path).resolve()
    audio = Path(audio_path).resolve()
    output = Path(output_path).resolve()
    if not video.is_file():
        raise FileNotFoundError(f"video not found: {video}")
    if not audio.is_file():
        raise FileNotFoundError(f"audio not found: {audio}")

    time.sleep(PRE_MUX_FLUSH_SEC)
    _verify_inputs(video, audio)

    if output.exists():
        output.unlink()

    output.parent.mkdir(parents=True, exist_ok=True)

    log(
        "postmux",
        f"Muxing {video.name} ({video.stat().st_size} B) + "
        f"{audio.name} ({audio.stat().st_size} B) → {output.name} (ffmpeg -c copy)",
    )

    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video),
        "-i",
        str(audio),
        "-c",
        "copy",
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        str(output),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        _remove_if_empty(output)
        err = (result.stderr or result.stdout or "unknown ffmpeg error").strip()
        raise RuntimeError(f"ffmpeg mux failed: {err}")

    if not output.is_file() or output.stat().st_size == 0:
        _remove_if_empty(output)
        raise RuntimeError("ffmpeg mux produced an empty output file")

    log("postmux", f"Saved: {output} ({output.stat().st_size} B)")
    return str(output)


def _remove_if_empty(path: Path) -> None:
    if path.is_file() and path.stat().st_size == 0:
        path.unlink(missing_ok=True)
        log("postmux", f"Removed empty output: {path.name}")


def cleanup_temp_files(*paths: str | Path) -> None:
    """Remove temporary recording files (ignore missing paths)."""
    for path in paths:
        p = Path(path)
        if p.is_file():
            try:
                p.unlink()
                log("postmux", f"Removed temp: {p.name}")
            except OSError as exc:
                log("postmux", f"Could not remove {p}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Remux GNOME video + audio to MKV/WebM")
    parser.add_argument("video", help="Video file from GNOME Shell Screencast")
    parser.add_argument("audio", help="Audio sidecar (.m4a / .mp4)")
    parser.add_argument("-o", "--output", required=True, help="Output .mkv or .webm path")
    args = parser.parse_args()

    try:
        out = mux_av_files(args.video, args.audio, args.output)
    except (RuntimeError, FileNotFoundError, OSError) as exc:
        print(f"postmux error: {exc}", file=sys.stderr)
        return 1

    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
