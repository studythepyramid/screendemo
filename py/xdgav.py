"""
Portal screen recording with mic + system audio (single live MP4 pipeline).

Video: org.freedesktop.portal.ScreenCast → pipewiresrc → x264
Audio: pipewiresrc mic + system → audiomixer → AAC → mp4mux

Sources: maskai/py/avrec.py, py/screen_recording.py, py/audio.py

Usage:
  uv run py/xdgav.py -o tmp/recording.mp4
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
from collections.abc import Callable
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import dbus
import gi

gi.require_version("Gst", "1.0")
gi.require_version("GLib", "2.0")
gi.require_version("GLibUnix", "2.0")
from gi.repository import GLib, GLibUnix, Gst

from py.audio import build_av_audio_mix_string, escape_gst_string, resolve_system_sink
from py.dbus_permissions import PortalScreenCastSession, setup_dbus_mainloop
from py.screen_recording import init_gstreamer
from py.settings import (
    SCREEN_EOS_FINALIZE_TIMEOUT_SEC,
    AvRecordingSettings,
    default_screen_mp4_path,
    log,
)


def build_portal_av_pipeline_string(
    output_path: str,
    system_sink: str,
    settings: AvRecordingSettings,
) -> str:
    """Portal PipeWire video + mixed audio → H.264/AAC MP4."""
    quoted_path = escape_gst_string(output_path)
    audio_branch = build_av_audio_mix_string(
        system_sink,
        mic_volume=settings.mic_volume,
        system_volume=settings.system_volume,
        audio_bitrate=settings.audio_bitrate,
        mic_capture_backend=settings.mic_capture_backend,
        system_capture_backend=settings.system_capture_backend,
    )
    return (
        f'mp4mux name=mux faststart=true ! filesink location="{quoted_path}" '
        f"pipewiresrc name=video_src do-timestamp=true ! queue ! videoconvert ! "
        f"x264enc tune=zerolatency speed-preset=ultrafast ! h264parse ! queue ! mux.video_0 "
        + audio_branch
    )


class PortalAvRecorder:
    """
    Record screen + audio via XDG Desktop Portal → single MP4.

    Shows the GNOME screen-share picker. Stop with request_stop(), Ctrl+C, or tray.
    """

    def __init__(self, settings: AvRecordingSettings) -> None:
        settings.validate()
        self.settings = settings
        self.system_sink = resolve_system_sink(settings.system_sink)
        self._portal_session: PortalScreenCastSession | None = None
        self.pipeline: Gst.Pipeline | None = None
        self._stop_requested = False
        self._finalize_timeout_id: int | None = None
        self._on_finished_cb: Callable[[int], None] | None = None
        self._exit_code = 0
        self._shutting_down = False
        self.output_path = os.path.abspath(settings.output_path)

        setup_dbus_mainloop()
        init_gstreamer()

    def begin(self, on_finished: Callable[[int], None] | None = None) -> None:
        """Start portal handshake (non-blocking; share Gtk/GLib main loop)."""
        self._on_finished_cb = on_finished
        self._exit_code = 0
        self._stop_requested = False
        self._shutting_down = False
        log("main", f"Output file: {self.output_path}")
        log("main", f"XDG_SESSION_TYPE={os.environ.get('XDG_SESSION_TYPE', '<unset>')}")
        log("main", f"System audio sink: {self.system_sink}")

        self._portal_session = PortalScreenCastSession(
            on_pipewire_ready=self._on_pipewire_ready,
            on_error=self._on_portal_error,
            on_session_closed=self._on_portal_session_closed,
            on_finished=self._on_portal_finished,
        )
        try:
            self._portal_session.begin()
        except dbus.DBusException as exc:
            print(f"D-Bus error: {exc}", file=sys.stderr)
            self._complete(1)

    def request_stop(self) -> None:
        self._graceful_stop("Stop requested")

    def start(self) -> int:
        loop = GLib.MainLoop()

        def done(_code: int) -> None:
            loop.quit()

        self.begin(on_finished=done)
        loop.run()
        return self._exit_code

    def _complete(self, code: int) -> None:
        self._exit_code = code
        cb = self._on_finished_cb
        self._on_finished_cb = None
        if cb:
            cb(code)

    def _on_portal_finished(self) -> None:
        self._complete(self._exit_code)

    def _on_portal_error(self, message: str) -> None:
        log("error", message)
        self._exit_code = 1
        self._shutdown_recording()

    def _on_portal_session_closed(self) -> None:
        if not self._stop_requested:
            self._graceful_stop("Portal session closed")

    def _on_pipewire_ready(self, fd: int, node_id: int) -> None:
        log("record", "Starting combined A/V GStreamer pipeline...")
        pipeline_string = build_portal_av_pipeline_string(
            self.output_path,
            self.system_sink,
            self.settings,
        )
        log("record", f"Pipeline: {pipeline_string}")

        try:
            pipeline = Gst.parse_launch(pipeline_string)
            if not isinstance(pipeline, Gst.Pipeline):
                raise RuntimeError("GStreamer did not return a pipeline object")

            video_src = pipeline.get_by_name("video_src")
            if video_src is None:
                raise RuntimeError("video_src element not found in pipeline")

            video_src.set_property("fd", fd)
            video_src.set_property("path", str(node_id))
            log("record", f"Configured video_src fd={fd}, path={node_id}")

            bus = pipeline.get_bus()
            bus.add_signal_watch()
            bus.connect("message", self._on_bus_message)

            self.pipeline = pipeline
            ret = pipeline.set_state(Gst.State.PLAYING)
            log(
                "record",
                f"Pipeline set_state(PLAYING) -> "
                f"{ret.value_nick if hasattr(ret, 'value_nick') else ret}",
            )
            if ret == Gst.StateChangeReturn.FAILURE:
                raise RuntimeError("Failed to set GStreamer pipeline to PLAYING")

            log("record", f"Recording A/V to: {self.output_path}")
            log("record", "Stop via app Stop button, system tray, or Ctrl+C")
            GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, self._request_stop)
        except Exception as exc:
            log("error", f"Failed to start pipeline: {exc}")
            print(f"Failed to start recording pipeline: {exc}", file=sys.stderr)
            self._exit_code = 1
            self._shutdown_recording()

    def _request_stop(self) -> bool:
        if self._stop_requested:
            log("record", "Stop already requested; waiting for file finalization...")
            return GLib.SOURCE_CONTINUE
        self._graceful_stop("Ctrl+C received")
        return GLib.SOURCE_REMOVE

    def _graceful_stop(self, reason: str) -> None:
        if self._stop_requested:
            return
        self._stop_requested = True
        log("record", f"{reason}. Finalizing MP4...")

        if self._portal_session is not None:
            self._portal_session.close_session()

        if self.pipeline is not None:
            self.pipeline.send_event(Gst.Event.new_eos())
            self._clear_finalize_timeout()
            self._finalize_timeout_id = GLib.timeout_add_seconds(
                SCREEN_EOS_FINALIZE_TIMEOUT_SEC,
                self._on_finalize_timeout,
            )
        else:
            self._shutdown_recording()

    def _clear_finalize_timeout(self) -> None:
        if self._finalize_timeout_id is not None:
            GLib.source_remove(self._finalize_timeout_id)
            self._finalize_timeout_id = None

    def _on_finalize_timeout(self) -> bool:
        self._finalize_timeout_id = None
        log("record", f"EOS finalization timeout ({SCREEN_EOS_FINALIZE_TIMEOUT_SEC}s)")
        self._shutdown_recording()
        return GLib.SOURCE_REMOVE

    def _on_bus_message(self, _bus: Gst.Bus, message: Gst.Message) -> None:
        if message.type == Gst.MessageType.EOS:
            log("record", "EOS received. Recording completed.")
            self._clear_finalize_timeout()
            self._shutdown_recording()
        elif message.type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            src_name = message.src.get_name() if message.src is not None else "unknown"
            log("record", f"GStreamer ERROR from {src_name}: {err}")
            if debug:
                log("record", f"Debug: {debug}")
            print(f"GStreamer error: {err}", file=sys.stderr)
            if not self._stop_requested:
                self._graceful_stop(f"Pipeline error from {src_name}")
            else:
                self._clear_finalize_timeout()
                self._shutdown_recording()
        elif message.type == Gst.MessageType.STATE_CHANGED and message.src == self.pipeline:
            _old, new, _pending = message.parse_state_changed()
            log("record", f"Pipeline state -> {new.value_nick}")

    def _shutdown_recording(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        self._clear_finalize_timeout()

        pipeline = self.pipeline
        self.pipeline = None
        if pipeline is None:
            self._finish_portal_session()
            return

        log("record", "Cleaning up GStreamer pipeline")
        bus = pipeline.get_bus()
        bus.remove_signal_watch()

        def worker() -> None:
            pipeline.set_state(Gst.State.NULL)
            GLib.idle_add(self._finish_portal_session)

        threading.Thread(target=worker, daemon=True).start()

    def _finish_portal_session(self) -> bool:
        self._shutting_down = False
        if self._portal_session is not None:
            self._portal_session.quit()
        else:
            self._complete(self._exit_code)
        return GLib.SOURCE_REMOVE


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Portal screen recording with mic + system audio → MP4"
    )
    parser.add_argument("-o", "--output", default=default_screen_mp4_path(), help="Output .mp4")
    parser.add_argument("--system-sink", default=None, help="PipeWire sink node.name")
    parser.add_argument("--mic-volume", type=float, default=1.0)
    parser.add_argument("--system-volume", type=float, default=1.0)
    parser.add_argument("--audio-bitrate", type=int, default=192_000)
    args = parser.parse_args()

    settings = AvRecordingSettings(
        output_path=args.output,
        system_sink=args.system_sink,
        mic_volume=args.mic_volume,
        system_volume=args.system_volume,
        audio_bitrate=args.audio_bitrate,
    )
    try:
        settings.validate()
    except ValueError as exc:
        print(f"Invalid settings: {exc}", file=sys.stderr)
        return 1

    try:
        return PortalAvRecorder(settings).start()
    except dbus.DBusException as exc:
        print(f"D-Bus error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
