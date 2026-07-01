"""
Image editing helpers for screendemo (crop snapshots, open in Drawing, etc.).

Uses ffmpeg for cropping.

Usage:
  uv run py/edimage.py -i tmp/snapshot.png
  uv run py/edimage.py -i tmp/snapshot.png -o tmp/snapshot-cropped.png
  uv run py/edimage.py -i tmp/snapshot.png --open
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from py.settings import (
    CROP_SNAPSHOT_BOTTOM,
    CROP_SNAPSHOT_LEFT,
    CROP_SNAPSHOT_RIGHT,
    CROP_SNAPSHOT_TOP,
    DRAWING_APP_ID,
    DRAWING_FULLSCREEN_TIMEOUT_SEC,
    DRAWING_START_FULLSCREEN,
    DRAWING_WINDOW_ACTION_PATH,
    OPEN_SNAPSHOT_IN_DRAWING,
    PROJECT_ROOT,
    SNAPSHOT_DRAW_COMMAND,
    log,
)


def _resolve_path(path: str | Path) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p.resolve()


def crop_image_ffmpeg(
    input_path: str | Path,
    output_path: str | Path | None = None,
    *,
    left: int = CROP_SNAPSHOT_LEFT,
    top: int = CROP_SNAPSHOT_TOP,
    right: int = CROP_SNAPSHOT_RIGHT,
    bottom: int = CROP_SNAPSHOT_BOTTOM,
    inplace: bool = False,
) -> str:
    """
    Crop pixels from the edges of an image using ffmpeg.

    Removes ``left`` / ``top`` / ``right`` / ``bottom`` pixels from each edge.
    When all are zero, returns the input path unchanged.

    Returns the absolute path of the cropped image.
    """
    src = _resolve_path(input_path)
    if not src.is_file():
        raise FileNotFoundError(f"image not found: {src}")

    if left == 0 and top == 0 and right == 0 and bottom == 0:
        return str(src)

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found on PATH")

    if inplace or output_path is None:
        dest = src
        tmp = src.with_name(f"{src.stem}.crop{src.suffix}")
    else:
        dest = _resolve_path(output_path)
        tmp = dest
    dest.parent.mkdir(parents=True, exist_ok=True)

    # crop=w:h:x:y  — w/h relative to input (iw/ih), origin at (left, top)
    vf = f"crop=iw-{left + right}:ih-{top + bottom}:{left}:{top}"
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(src),
        "-vf",
        vf,
        str(tmp),
    ]
    log("edimage", f"Cropping {src.name}: left={left} top={top} right={right} bottom={bottom}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "unknown ffmpeg error").strip()
        if tmp != src and tmp.exists():
            tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg crop failed: {err}")

    if inplace or output_path is None:
        tmp.replace(src)
        saved = src
    else:
        saved = dest

    log("edimage", f"Saved: {saved}")
    return str(saved)


def crop_snapshot(path: str | Path) -> str:
    """Crop a snapshot using defaults from settings.py."""
    return crop_image_ffmpeg(path, inplace=True)


def _try_drawing_fullscreen() -> bool:
    """Toggle Drawing's ``fullscreen`` action via org.gtk.Actions (same as F11)."""
    import dbus

    bus = dbus.SessionBus()
    paths = [
        DRAWING_WINDOW_ACTION_PATH,
        "/com/github/maoschanz/drawing/window/2",
        "/com/github/maoschanz/drawing/window/3",
    ]
    for path in paths:
        try:
            obj = bus.get_object(DRAWING_APP_ID, path)
            actions = dbus.Interface(obj, "org.gtk.Actions")
            actions.SetState(
                "fullscreen",
                dbus.Boolean(True),
                dbus.Dictionary({}, signature="sv"),
            )
            return True
        except dbus.DBusException:
            continue
    return False


def _wait_and_fullscreen_drawing() -> None:
    deadline = time.monotonic() + DRAWING_FULLSCREEN_TIMEOUT_SEC
    while time.monotonic() < deadline:
        if _try_drawing_fullscreen():
            log("edimage", "Drawing set to fullscreen")
            return
        time.sleep(0.2)
    log("edimage", "Could not set Drawing fullscreen (window not ready)")


def open_snapshot_in_drawing(path: str | Path) -> None:
    """Launch GNOME Drawing with the snapshot file (non-blocking)."""
    image = _resolve_path(path)
    if not image.is_file():
        raise FileNotFoundError(f"image not found: {image}")

    draw_cmd = shutil.which(SNAPSHOT_DRAW_COMMAND) or SNAPSHOT_DRAW_COMMAND
    log("edimage", f"Opening in {draw_cmd}: {image.name}")
    subprocess.Popen(
        [draw_cmd, str(image)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    if DRAWING_START_FULLSCREEN:
        threading.Thread(target=_wait_and_fullscreen_drawing, daemon=True).start()


def finalize_snapshot(path: str | Path) -> str:
    """
    Crop dock / top bar, then open in Drawing when enabled in settings.

    Used after the Snapshot button or ``snapshot.py`` capture.
    """
    cropped = crop_snapshot(path)
    if OPEN_SNAPSHOT_IN_DRAWING:
        try:
            open_snapshot_in_drawing(cropped)
        except (FileNotFoundError, OSError) as exc:
            log("edimage", f"Could not open Drawing: {exc}")
    return cropped


def main() -> int:
    parser = argparse.ArgumentParser(description="Crop snapshot images with ffmpeg")
    parser.add_argument("-i", "--input", required=True, help="Input image path")
    parser.add_argument("-o", "--output", help="Output path (default: overwrite input)")
    parser.add_argument("--left", type=int, default=CROP_SNAPSHOT_LEFT)
    parser.add_argument("--top", type=int, default=CROP_SNAPSHOT_TOP)
    parser.add_argument("--right", type=int, default=CROP_SNAPSHOT_RIGHT)
    parser.add_argument("--bottom", type=int, default=CROP_SNAPSHOT_BOTTOM)
    parser.add_argument(
        "--open",
        action="store_true",
        help="Open cropped image in Drawing after crop",
    )
    args = parser.parse_args()

    try:
        out = crop_image_ffmpeg(
            args.input,
            args.output,
            left=args.left,
            top=args.top,
            right=args.right,
            bottom=args.bottom,
            inplace=args.output is None,
        )
        if args.open:
            open_snapshot_in_drawing(out)
    except (RuntimeError, FileNotFoundError, OSError) as exc:
        print(f"edimage error: {exc}", file=sys.stderr)
        return 1

    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
