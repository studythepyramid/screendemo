"""
Capture a single desktop screenshot to a PNG file on GNOME Wayland.

Tries org.gnome.Shell.Screenshot first (no portal dialog). Falls back to the
XDG Desktop Portal when Shell returns AccessDenied.

Usage:
  uv run py/snapshot.py -o tmp/snapshot.png
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from urllib.parse import unquote, urlparse

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import dbus
import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib

from py.dbus_permissions import GnomeShellScreenshot, PortalScreenshot
from py.edimage import finalize_snapshot
from py.settings import PROJECT_ROOT, TMP_FOLDER, default_snapshot_png_path, log


def uri_to_path(uri: str) -> Path:
    return Path(unquote(urlparse(uri).path))


def copy_uri_to_path(uri: str, dest: Path) -> Path:
    src = uri_to_path(uri)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return dest.resolve()


def _try_gnome_shell(dest: Path, *, include_cursor: bool) -> str | None:
    try:
        shell = GnomeShellScreenshot()
        used = shell.capture(str(dest), include_cursor=include_cursor)
        return os.path.abspath(used)
    except dbus.DBusException as exc:
        if "AccessDenied" in str(exc) or "not allowed" in str(exc).lower():
            log("snapshot", "GNOME Shell denied; using portal")
            return None
        raise RuntimeError(f"GNOME Shell Screenshot failed: {exc}") from exc
    except RuntimeError as exc:
        if "AccessDenied" in str(exc) or "not allowed" in str(exc).lower():
            log("snapshot", "GNOME Shell denied; using portal")
            return None
        raise


def _finalize_snapshot(path: str) -> str:
    """Apply post-capture edits (crop, open in Drawing)."""
    try:
        return finalize_snapshot(path)
    except (RuntimeError, OSError) as exc:
        raise RuntimeError(f"snapshot finalize failed: {exc}") from exc


def capture_snapshot(
    output_path: str | Path,
    *,
    include_cursor: bool = True,
    interactive: bool = False,
    parent_window: str = "",
) -> str:
    """
    Capture the desktop to ``output_path`` (blocking).

    Returns the absolute path of the saved PNG.
    """
    dest = Path(output_path)
    if not dest.is_absolute():
        dest = PROJECT_ROOT / dest
    dest = dest.resolve()
    if dest.suffix.lower() != ".png":
        raise ValueError("snapshot output must end with .png")
    dest.parent.mkdir(parents=True, exist_ok=True)

    gnome_path = _try_gnome_shell(dest, include_cursor=include_cursor)
    if gnome_path is not None:
        return _finalize_snapshot(gnome_path)

    result: dict[str, str | None] = {"uri": None, "error": None}
    loop = GLib.MainLoop()

    def on_success(uri: str) -> None:
        result["uri"] = uri
        loop.quit()

    def on_error(message: str) -> None:
        result["error"] = message
        loop.quit()

    portal = PortalScreenshot(on_success, on_error=on_error)
    portal.capture(parent_window, interactive=interactive)
    loop.run()

    if result["error"]:
        raise RuntimeError(result["error"])
    if not result["uri"]:
        raise RuntimeError("portal screenshot returned no uri")

    saved = copy_uri_to_path(result["uri"], dest)
    log("snapshot", f"Saved: {saved}")
    return _finalize_snapshot(str(saved))


def capture_snapshot_async(
    output_path: str | Path,
    on_done: Callable[[str], None],
    *,
    on_error: Callable[[str], None] | None = None,
    include_cursor: bool = True,
    interactive: bool = False,
    parent_window: str = "",
) -> None:
    """
    Capture the desktop without blocking the GLib main loop.

    ``on_done`` receives the absolute path to the PNG.
    """
    dest = Path(output_path)
    if not dest.is_absolute():
        dest = PROJECT_ROOT / dest
    dest = dest.resolve()
    if dest.suffix.lower() != ".png":
        if on_error:
            on_error("snapshot output must end with .png")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)

    def finish_ok(path: str) -> None:
        try:
            path = _finalize_snapshot(path)
        except RuntimeError as exc:
            finish_err(str(exc))
            return
        GLib.idle_add(lambda: (on_done(path), False)[-1])

    def finish_err(message: str) -> None:
        if on_error:
            GLib.idle_add(lambda: (on_error(message), False)[-1])

    gnome_path = _try_gnome_shell(dest, include_cursor=include_cursor)
    if gnome_path is not None:
        finish_ok(gnome_path)
        return

    def on_portal_success(uri: str) -> None:
        try:
            saved = copy_uri_to_path(uri, dest)
            log("snapshot", f"Saved: {saved}")
            finish_ok(str(saved))
        except OSError as exc:
            finish_err(f"failed to copy screenshot: {exc}")

    portal = PortalScreenshot(on_portal_success, on_error=finish_err)
    portal.capture(parent_window, interactive=interactive)


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture desktop screenshot to PNG")
    parser.add_argument(
        "-o",
        "--output",
        default=str(TMP_FOLDER / default_snapshot_png_path()),
        help="Output .png path",
    )
    parser.add_argument(
        "--no-cursor",
        action="store_true",
        help="Exclude mouse cursor (GNOME Shell path only)",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Use portal region/window picker (portal fallback only)",
    )
    args = parser.parse_args()

    try:
        path = capture_snapshot(
            args.output,
            include_cursor=not args.no_cursor,
            interactive=args.interactive,
        )
    except (RuntimeError, ValueError) as exc:
        print(f"Snapshot error: {exc}", file=sys.stderr)
        return 1

    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
