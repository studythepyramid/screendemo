"""
Screen recording on GNOME Wayland via PipeWire + GStreamer.

Capture paths:

1. **portal** — XDG Desktop Portal → MP4 (video-only in this module; A+V in xdgav.py)
2. **gnome** — org.gnome.Shell.Screencast → WebM/MKV (video-only or A+V via GnomeShellAvRecorder + postmux.py)
"""

from __future__ import annotations

import os
import signal
import sys
from collections.abc import Callable
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import argparse
import os
import random
import signal
import sys
import threading
from collections.abc import Callable

import dbus
import gi

gi.require_version("Gst", "1.0")
gi.require_version("GLib", "2.0")
gi.require_version("GLibUnix", "2.0")
from gi.repository import GLib, GLibUnix, Gst

from py.audio import escape_gst_string
from py.audio import AudioRecorder, AudioSettings
from py.dbus_permissions import GnomeShellScreencast, PortalScreenCastSession, setup_dbus_mainloop
from py.postmux import cleanup_temp_files, mux_av_files
from py.settings import (
    AUDIO_EOS_FINALIZE_TIMEOUT_SEC,
    DEFAULT_RECORDING_MODE,
    RECORDING_MODE_GNOME,
    RECORDING_MODE_PORTAL,
    SCREEN_EOS_FINALIZE_TIMEOUT_SEC,
    AvRecordingSettings,
    ScreenRecordingSettings,
    default_screen_mp4_path,
    default_screen_mkv_path,
    default_screen_webm_path,
    log,
)


def init_gstreamer() -> None:
    Gst.init(None)


def build_portal_video_pipeline_string(output_path: str) -> str:
    """Portal PipeWire video stream → H.264 MP4."""
    quoted_path = escape_gst_string(output_path)
    return (
        "pipewiresrc name=video_src do-timestamp=true ! "
        "queue ! videoconvert ! "
        "x264enc tune=zerolatency speed-preset=ultrafast ! "
        "h264parse ! mp4mux faststart=true ! "
        f'filesink location="{quoted_path}"'
    )


class PortalScreenRecorder:
    """
    Record screen via XDG Desktop Portal + PipeWire → MP4.

    Shows the GNOME screen-share picker. Stop with request_stop(), Ctrl+C, or tray.
    """

    def __init__(self, settings: ScreenRecordingSettings) -> None:
        settings.validate()
        self.settings = settings
        self._portal_session: PortalScreenCastSession | None = None
        self.pipeline: Gst.Pipeline | None = None
        self._stop_requested = False
        self._finalize_timeout_id: int | None = None
        self._on_finished_cb: Callable[[int], None] | None = None
        self._exit_code = 0
        self._shutting_down = False

        setup_dbus_mainloop()
        init_gstreamer()

    def begin(self, on_finished: Callable[[int], None] | None = None) -> None:
        """Start portal handshake (non-blocking; share Gtk/GLib main loop)."""
        self._on_finished_cb = on_finished
        self._exit_code = 0
        self._stop_requested = False
        self._shutting_down = False
        log("main", f"Output file: {self.settings.output_path}")
        log("main", f"XDG_SESSION_TYPE={os.environ.get('XDG_SESSION_TYPE', '<unset>')}")

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
        """Stop recording and finalize the output file."""
        self._graceful_stop("Stop requested")

    def start(self) -> int:
        """Block until recording finishes (CLI entry point)."""
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
        log("record", "Starting GStreamer video pipeline...")
        pipeline_string = build_portal_video_pipeline_string(self.settings.output_path)
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

            log("record", f"Recording to: {self.settings.output_path}")
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

        # Close portal share first so pipewiresrc can drain (avoids EOS hang).
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
        """Tear down pipeline without blocking the Gtk main loop."""
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

    def _cleanup_pipeline(self) -> None:
        """Synchronous cleanup (CLI-only paths); prefer _shutdown_recording in Gtk."""
        if self.pipeline is None:
            return
        log("record", "Cleaning up GStreamer pipeline")
        self._clear_finalize_timeout()
        bus = self.pipeline.get_bus()
        bus.remove_signal_watch()
        self.pipeline.set_state(Gst.State.NULL)
        self.pipeline = None


class GnomeShellScreenRecorder:
    """
    Record full screen via org.gnome.Shell.Screencast → WebM.

    Bypasses the portal confirmation dialog. Video only (no audio track).
    """

    def __init__(self, settings: ScreenRecordingSettings) -> None:
        settings.validate()
        self.settings = settings
        self.output_path = os.path.abspath(settings.output_path)
        self.shell = GnomeShellScreencast()
        self._stop_requested = False
        self._on_finished_cb: Callable[[int], None] | None = None
        self._exit_code = 0

    def begin(self, on_finished: Callable[[int], None] | None = None) -> None:
        self._on_finished_cb = on_finished
        self._exit_code = 0
        self._stop_requested = False
        log("main", f"Requested output: {self.output_path}")
        try:
            actual = self.shell.start(self.output_path, draw_cursor=self.settings.draw_cursor)
            # GNOME Shell may change the extension (e.g. .webm → .mp4).
            self.output_path = os.path.abspath(actual)
        except RuntimeError as exc:
            print(f"GNOME Screencast error: {exc}", file=sys.stderr)
            self._complete(1)
            return
        log("record", "Recording started.")
        GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, self._request_stop)

    def request_stop(self) -> None:
        if self._stop_requested:
            return
        self._stop_requested = True
        log("record", "Stopping GNOME Shell screencast...")
        self.shell.stop()
        log("record", f"Saved: {self.output_path}")
        self._complete(0)

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

    def _request_stop(self) -> bool:
        self.request_stop()
        return GLib.SOURCE_REMOVE


class GnomeShellAvRecorder:
    """
    GNOME Shell screencast (video file) + parallel AAC audio → post-mux to MKV/WebM.
    """

    def __init__(self, settings: AvRecordingSettings) -> None:
        settings.validate()
        self.settings = settings
        self.output_path = os.path.abspath(settings.output_path)
        self.shell = GnomeShellScreencast()
        self._audio_recorder: AudioRecorder | None = None
        self._stop_requested = False
        self._on_finished_cb: Callable[[int], None] | None = None
        self._exit_code = 0
        self._mux_started = False
        self._finalize_timeout_id: int | None = None

        output_dir = os.path.dirname(self.output_path)
        rand_id = f"{random.randint(0, 0xFFFFFF):06x}"
        self.temp_video_path = os.path.join(output_dir, f".temp_video_{rand_id}.webm")
        self.temp_audio_path = os.path.join(output_dir, f".temp_audio_{rand_id}.m4a")
        self.video_path = self.temp_video_path

    def begin(self, on_finished: Callable[[int], None] | None = None) -> None:
        self._on_finished_cb = on_finished
        self._exit_code = 0
        self._stop_requested = False
        self._mux_started = False
        log("main", f"Output file: {self.output_path}")

        audio_settings = AudioSettings(
            output_path=self.temp_audio_path,
            system_sink=self.settings.system_sink,
            mic_volume=self.settings.mic_volume,
            system_volume=self.settings.system_volume,
            audio_bitrate=self.settings.audio_bitrate,
            mic_capture_backend=self.settings.mic_capture_backend,
            system_capture_backend=self.settings.system_capture_backend,
        )
        try:
            self._audio_recorder = AudioRecorder(audio_settings, container="mp4")
            self._audio_recorder.begin(on_finished=self._on_audio_finished)
        except RuntimeError as exc:
            print(f"Audio error: {exc}", file=sys.stderr)
            self._complete(1)
            return

        try:
            actual = self.shell.start(self.temp_video_path, draw_cursor=self.settings.draw_cursor)
            self.video_path = os.path.abspath(actual)
        except RuntimeError as exc:
            print(f"GNOME Screencast error: {exc}", file=sys.stderr)
            if self._audio_recorder is not None:
                self._audio_recorder.request_stop()
            self._complete(1)
            return

        log("record", f"GNOME video → {self.video_path}")
        log("record", f"Audio sidecar → {self.temp_audio_path}")
        log("record", "Recording A/V started.")
        GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, self._request_stop)

    def request_stop(self) -> None:
        if self._stop_requested:
            return
        self._stop_requested = True
        log("record", "Stopping GNOME Shell screencast and audio...")
        try:
            self.shell.stop()
            log("gnome-screencast", "Stopped.")
        except RuntimeError as exc:
            log("error", str(exc))
        if self._audio_recorder is not None:
            self._audio_recorder.request_stop()
        self._arm_finalize_timeout()

    def _arm_finalize_timeout(self) -> None:
        if self._finalize_timeout_id is not None:
            GLib.source_remove(self._finalize_timeout_id)
        self._finalize_timeout_id = GLib.timeout_add_seconds(
            AUDIO_EOS_FINALIZE_TIMEOUT_SEC + 10,
            self._on_finalize_timeout,
        )

    def _clear_finalize_timeout(self) -> None:
        if self._finalize_timeout_id is not None:
            GLib.source_remove(self._finalize_timeout_id)
            self._finalize_timeout_id = None

    def _on_finalize_timeout(self) -> bool:
        self._finalize_timeout_id = None
        if self._mux_started:
            return GLib.SOURCE_REMOVE
        log("error", "Recording finalize timeout")
        try:
            self.shell.stop()
        except RuntimeError:
            pass
        cleanup_temp_files(self.temp_audio_path, self.video_path)
        self._complete(1)
        return GLib.SOURCE_REMOVE

    def start(self) -> int:
        loop = GLib.MainLoop()

        def done(_code: int) -> None:
            loop.quit()

        self.begin(on_finished=done)
        loop.run()
        return self._exit_code

    def _on_audio_finished(self, exit_code: int) -> None:
        if not self._stop_requested:
            if exit_code != 0:
                log("error", "Audio capture failed during recording")
                try:
                    self.shell.stop()
                except RuntimeError:
                    pass
                cleanup_temp_files(self.temp_audio_path, self.video_path)
                self._complete(1)
            return
        if self._mux_started:
            return
        self._mux_started = True
        self._clear_finalize_timeout()

        if exit_code != 0:
            cleanup_temp_files(self.temp_audio_path, self.video_path)
            self._complete(1)
            return

        video = self.video_path
        audio = self.temp_audio_path
        output = self.output_path

        def worker() -> None:
            code = 0
            try:
                if os.path.exists(video) and os.path.exists(audio):
                    mux_av_files(video, audio, output)
                    log("record", f"Saved: {output}")
                else:
                    log("error", "Temporary video or audio file missing")
                    code = 1
            except (RuntimeError, OSError) as exc:
                log("error", f"Post-mux failed: {exc}")
                code = 1
            finally:
                if code != 0 and os.path.exists(output) and os.path.getsize(output) == 0:
                    try:
                        os.unlink(output)
                    except OSError:
                        pass
                cleanup_temp_files(audio, video)
            GLib.idle_add(lambda: (self._complete(code), False)[-1])

        threading.Thread(target=worker, daemon=True).start()

    def _request_stop(self) -> bool:
        self.request_stop()
        return GLib.SOURCE_REMOVE

    def _complete(self, code: int) -> None:
        self._exit_code = code
        cb = self._on_finished_cb
        self._on_finished_cb = None
        if cb:
            cb(code)


def run_screen_recording(settings: ScreenRecordingSettings) -> int:
    if settings.mode == RECORDING_MODE_GNOME:
        return GnomeShellScreenRecorder(settings).start()
    return PortalScreenRecorder(settings).start()


def run_av_recording(settings: AvRecordingSettings) -> int:
    if settings.mode == RECORDING_MODE_GNOME:
        return GnomeShellAvRecorder(settings).start()
    from py.xdgav import PortalAvRecorder

    return PortalAvRecorder(settings).start()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Screen recording on GNOME Wayland (PipeWire + GStreamer)"
    )
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="Output path (.mp4 for portal, .webm/.mkv for gnome)",
    )
    parser.add_argument(
        "--mode",
        choices=(RECORDING_MODE_PORTAL, RECORDING_MODE_GNOME),
        default=DEFAULT_RECORDING_MODE,
        help="portal=picker dialog → MP4; gnome=shell API → WebM (default: portal)",
    )
    parser.add_argument(
        "--no-cursor",
        action="store_true",
        help="Hide mouse cursor (gnome mode only)",
    )
    args = parser.parse_args()

    if args.output is None:
        args.output = (
            default_screen_webm_path()
            if args.mode == RECORDING_MODE_GNOME
            else default_screen_mp4_path()
        )

    settings = ScreenRecordingSettings(
        output_path=args.output,
        mode=args.mode,
        draw_cursor=not args.no_cursor,
    )

    try:
        settings.validate()
    except ValueError as exc:
        print(f"Invalid settings: {exc}", file=sys.stderr)
        return 1

    return run_screen_recording(settings)


if __name__ == "__main__":
    raise SystemExit(main())
