"""
Small GTK control panel for screendemo screen recording (v1, video only).

Recording runs in-process (shared GLib loop with Gtk) so Stop works reliably.

Usage:
  uv run py/ui_simple.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from py.screen_recording import GnomeShellScreenRecorder, PortalScreenRecorder
from py.settings import (
    DEFAULT_RECORDING_MODE,
    PROJECT_ROOT,
    RECORDING_MODE_GNOME,
    RECORDING_MODE_PORTAL,
    TMP_FOLDER,
    ScreenRecordingSettings,
    timestamped_filename,
)

APP_ID = "com.studythepyramid.screendemo.ui"


class SimpleRecorderWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.set_title("screendemo")
        self.set_default_size(440, 300)

        self._recorder: PortalScreenRecorder | GnomeShellScreenRecorder | None = None
        self._last_saved_path: str | None = None

        self.connect("close-request", self._on_close_request)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label="screendemo"))
        toolbar.add_top_bar(header)
        self.set_content(toolbar)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        root.set_margin_top(16)
        root.set_margin_bottom(16)
        root.set_margin_start(16)
        root.set_margin_end(16)
        toolbar.set_content(root)

        # --- output folder ---
        folder_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        folder_row.append(Gtk.Label(label="Folder:", width_chars=8, xalign=0))
        self.folder_entry = Gtk.Entry()
        self.folder_entry.set_text("tmp")
        self.folder_entry.set_hexpand(True)
        folder_row.append(self.folder_entry)
        btn_browse = Gtk.Button(label="Browse…")
        btn_browse.connect("clicked", self._on_browse_folder)
        folder_row.append(btn_browse)
        root.append(folder_row)

        # --- filename ---
        name_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        name_row.append(Gtk.Label(label="Filename:", width_chars=8, xalign=0))
        self.filename_entry = Gtk.Entry()
        default_ext = "mp4"
        self.filename_entry.set_text(timestamped_filename("recording", default_ext))
        self.filename_entry.set_hexpand(True)
        name_row.append(self.filename_entry)
        root.append(name_row)

        # --- mode ---
        mode_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        mode_row.append(Gtk.Label(label="Mode:", width_chars=8, xalign=0))
        self.mode_dropdown = Gtk.DropDown.new_from_strings(
            ["GNOME Shell (no dialog → MP4)", "Portal (picker → MP4)"]
        )
        self.mode_dropdown.set_selected(
            0 if DEFAULT_RECORDING_MODE == RECORDING_MODE_GNOME else 1
        )
        self.mode_dropdown.connect("notify::selected", self._on_mode_changed)
        self.mode_dropdown.set_hexpand(True)
        mode_row.append(self.mode_dropdown)
        root.append(mode_row)

        # --- record / stop ---
        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        btn_row.set_halign(Gtk.Align.CENTER)
        self.btn_record = Gtk.Button(label="Record")
        self.btn_record.add_css_class("suggested-action")
        self.btn_record.connect("clicked", self._on_record)
        btn_row.append(self.btn_record)
        self.btn_stop = Gtk.Button(label="Stop")
        self.btn_stop.add_css_class("destructive-action")
        self.btn_stop.set_sensitive(False)
        self.btn_stop.connect("clicked", self._on_stop)
        btn_row.append(self.btn_stop)
        root.append(btn_row)

        self.status_label = Gtk.Label(label="Status: idle", xalign=0)
        root.append(self.status_label)

        hint = Gtk.Label(
            label=(
                "Portal mode shows GNOME's screen-share icon in the top bar "
                "(required by the system; use GNOME Shell mode to avoid it). "
                "Close this window with the ✕ button when done — the terminal "
                "stays busy while the UI is open."
            ),
            wrap=True,
            xalign=0,
            css_classes=["dim-label"],
        )
        root.append(hint)

    def _on_close_request(self, _window) -> bool:
        if self._recorder is not None:
            self._set_status("stopping before close…")
            self._recorder.request_stop()
        self.destroy()
        return True

    def _recording_mode(self) -> str:
        return (
            RECORDING_MODE_GNOME
            if self.mode_dropdown.get_selected() == 0
            else RECORDING_MODE_PORTAL
        )

    def _default_ext_for_mode(self) -> str:
        return "mp4"

    def _on_mode_changed(self, _dropdown, _pspec) -> None:
        name = self.filename_entry.get_text().strip()
        if not name:
            return
        stem = Path(name).stem
        self.filename_entry.set_text(f"{stem}.{self._default_ext_for_mode()}")

    def _resolve_output_path(self) -> Path:
        folder_text = self.folder_entry.get_text().strip() or "tmp"
        folder = Path(folder_text)
        if not folder.is_absolute():
            folder = PROJECT_ROOT / folder
        folder.mkdir(parents=True, exist_ok=True)

        filename = self.filename_entry.get_text().strip()
        if not filename:
            filename = timestamped_filename("recording", self._default_ext_for_mode())
            self.filename_entry.set_text(filename)

        return folder / filename

    def _set_recording_ui(self, active: bool) -> None:
        self.btn_record.set_sensitive(not active)
        self.btn_stop.set_sensitive(active)
        self.folder_entry.set_sensitive(not active)
        self.filename_entry.set_sensitive(not active)
        self.mode_dropdown.set_sensitive(not active)

    def _set_status(self, text: str) -> None:
        self.status_label.set_label(f"Status: {text}")

    def _on_browse_folder(self, _button) -> None:
        dialog = Gtk.FileDialog(title="Select output folder")
        dialog.select_folder(
            self,
            None,
            self._on_folder_selected,
            None,
        )

    def _on_folder_selected(self, dialog, result) -> None:
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        path = folder.get_path()
        if path:
            try:
                rel = Path(path).relative_to(PROJECT_ROOT)
                self.folder_entry.set_text(str(rel))
            except ValueError:
                self.folder_entry.set_text(path)

    def _on_record(self, _button) -> None:
        if self._recorder is not None:
            return

        output_path = self._resolve_output_path()
        mode = self._recording_mode()
        settings = ScreenRecordingSettings(output_path=str(output_path), mode=mode)
        try:
            settings.validate()
        except ValueError as exc:
            self._set_status(f"error — {exc}")
            return

        if mode == RECORDING_MODE_GNOME:
            self._recorder = GnomeShellScreenRecorder(settings)
        else:
            self._recorder = PortalScreenRecorder(settings)

        self._set_recording_ui(True)
        self._recorder.begin(on_finished=self._on_recording_finished)
        self._sync_filename_from_recorder(output_path)

    def _sync_filename_from_recorder(self, requested: Path) -> None:
        """Keep the filename field aligned with the path the recorder is using."""
        if self._recorder is None:
            return
        actual = Path(getattr(self._recorder, "output_path", requested))
        self.filename_entry.set_text(actual.name)
        self._set_status(f"recording → {actual.name}")

    def _on_stop(self, _button) -> None:
        if self._recorder is None:
            return
        self._set_status("stopping…")
        self._recorder.request_stop()

    def _on_recording_finished(self, exit_code: int) -> None:
        GLib.idle_add(self._finish_recording_ui, exit_code)

    def _finish_recording_ui(self, exit_code: int) -> bool:
        if self._recorder is not None:
            self._last_saved_path = getattr(self._recorder, "output_path", None)
        self._recorder = None
        self._set_recording_ui(False)
        if exit_code == 0 and self._last_saved_path:
            saved = Path(self._last_saved_path)
            self._set_status(f"idle — saved {saved.name}")
            ext = saved.suffix.lstrip(".") or self._default_ext_for_mode()
        elif exit_code == 0:
            self._set_status("idle — saved")
            ext = self._default_ext_for_mode()
        else:
            self._set_status(f"idle — exited ({exit_code})")
            ext = self._default_ext_for_mode()

        self.filename_entry.set_text(timestamped_filename("recording", ext))
        return GLib.SOURCE_REMOVE


class SimpleRecorderApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID)

    def do_activate(self) -> None:
        win = self.props.active_window
        if not win:
            win = SimpleRecorderWindow(application=self)
        win.present()


def main() -> int:
    TMP_FOLDER.mkdir(parents=True, exist_ok=True)
    app = SimpleRecorderApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
