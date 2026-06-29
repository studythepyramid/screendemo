"""
PipeWire microphone + system audio capture via GStreamer.

Captures mic and desktop monitor, mixes them, and writes WAV, MP4, or AAC.

Sources: maskai/py/audio_rec.py, maskai/py/gnome_sr.py, maskai/py/avrec.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow `uv run py/audio.py` (script mode) as well as `uv run python -m py.audio`.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import argparse
import re
import signal
import subprocess
from typing import Literal

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GLib", "2.0")
gi.require_version("GLibUnix", "2.0")
from gi.repository import GLib, GLibUnix, Gst

from py.settings import (
    DEFAULT_AUDIO_BITRATE,
    DEFAULT_MIC_CAPTURE_BACKEND,
    DEFAULT_MIC_VOLUME,
    DEFAULT_SYSTEM_CAPTURE_BACKEND,
    DEFAULT_SYSTEM_VOLUME,
    MIC_MEDIA_ROLE,
    PULSE_DEFAULT_MONITOR,
    PULSE_DEFAULT_SOURCE,
    WPCTL_DEFAULT_SINK,
    AudioSettings,
    default_audio_aac_path,
    default_audio_mp4_path,
    default_audio_wav_path,
    log,
)

ContainerFormat = Literal["wav", "mp4", "aac"]
CaptureBackend = Literal["pulse", "pipewire"]


def init_gstreamer() -> None:
    Gst.init(None)


def escape_gst_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def resolve_system_sink(explicit: str | None = None) -> str:
    """Return PipeWire sink ``node.name`` used for native monitor capture."""
    if explicit:
        return explicit

    try:
        result = subprocess.run(
            ["wpctl", "inspect", WPCTL_DEFAULT_SINK],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "wpctl not found; install wireplumber or pass --system-sink explicitly"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"wpctl inspect {WPCTL_DEFAULT_SINK} failed: {exc.stderr.strip()}"
        ) from exc

    match = re.search(r'node\.name\s*=\s*"([^"]+)"', result.stdout)
    if match:
        return match.group(1)

    match = re.search(r"node\.name\s*=\s*(\S+)", result.stdout)
    if match:
        return match.group(1)

    raise RuntimeError(
        "Could not read node.name from wpctl; pass system_sink explicitly"
    )


def _optional_volume_element(volume: float) -> str:
    """Only insert a volume element when gain differs from unity (maskai uses none)."""
    if volume == 1.0:
        return ""
    return f"volume volume={volume} ! "


def _mixer_head(settings: AudioSettings) -> str:
    # Plain audiomixer — same as maskai/py/audio_rec.py (no latency tuning).
    return f"audiomixer name=mixer ! {settings.stereo_caps} ! audioconvert ! "


def _encode_tail(settings: AudioSettings, *, container: ContainerFormat) -> str:
    if container == "wav":
        return (
            f"audio/x-raw,format=S16LE,rate={settings.sample_rate},"
            f"channels={settings.channels} ! wavenc ! queue ! "
        )
    return (
        f"avenc_aac bitrate={settings.audio_bitrate} ! aacparse ! queue ! "
    )


def _mic_branch_pulse(settings: AudioSettings) -> str:
    return (
        f'pulsesrc name=mic_stream device="{PULSE_DEFAULT_SOURCE}" ! '
        f"queue ! "
        f"{_optional_volume_element(settings.mic_volume)}"
        f"audioconvert ! audioresample ! {settings.stereo_caps} ! mixer. "
    )


def _mic_branch_pipewire(settings: AudioSettings) -> str:
    return (
        f'pipewiresrc name=mic_stream stream-properties="props,media.role={MIC_MEDIA_ROLE}" ! '
        f"queue ! "
        f"{_optional_volume_element(settings.mic_volume)}"
        f"audioconvert ! audioresample ! {settings.stereo_caps} ! mixer. "
    )


def _system_branch_pulse(settings: AudioSettings) -> str:
    return (
        f'pulsesrc name=system_stream device="{PULSE_DEFAULT_MONITOR}" ! '
        f"queue ! "
        f"{_optional_volume_element(settings.system_volume)}"
        f"audioconvert ! audioresample ! {settings.stereo_caps} ! mixer. "
    )


def _system_branch_pipewire(settings: AudioSettings, system_sink: str) -> str:
    quoted_sink = escape_gst_string(system_sink)
    return (
        f'pipewiresrc name=system_stream target-object="{quoted_sink}" '
        f'stream-properties="props,stream.capture.sink=true" ! '
        f"queue ! "
        f"{_optional_volume_element(settings.system_volume)}"
        f"audioconvert ! audioresample ! {settings.stereo_caps} ! mixer. "
    )


def _mic_branch(settings: AudioSettings) -> str:
    if settings.mic_capture_backend == "pipewire":
        return _mic_branch_pipewire(settings)
    return _mic_branch_pulse(settings)


def _system_branch(settings: AudioSettings, system_sink: str) -> str:
    if settings.system_capture_backend == "pipewire":
        return _system_branch_pipewire(settings, system_sink)
    return _system_branch_pulse(settings)


def build_wav_pipeline_string(settings: AudioSettings, system_sink: str) -> str:
    """Mix mic + system monitor into uncompressed WAV."""
    quoted_path = escape_gst_string(settings.output_path)
    return (
        _mixer_head(settings)
        + _encode_tail(settings, container="wav")
        + f'filesink location="{quoted_path}" '
        + _mic_branch(settings)
        + _system_branch(settings, system_sink)
    )


def build_aac_pipeline_string(settings: AudioSettings, system_sink: str) -> str:
    """Mix mic + system monitor and write raw AAC (companion track for GNOME Shell video)."""
    quoted_path = escape_gst_string(settings.output_path)
    return (
        _mixer_head(settings)
        + _encode_tail(settings, container="aac")
        + f'filesink location="{quoted_path}" '
        + _mic_branch(settings)
        + _system_branch(settings, system_sink)
    )


def build_mp4_audio_pipeline_string(settings: AudioSettings, system_sink: str) -> str:
    """Mix mic + system monitor into an MP4 audio-only file."""
    quoted_path = escape_gst_string(settings.output_path)
    return (
        f'mp4mux name=mux faststart=true ! filesink location="{quoted_path}" '
        + _mixer_head(settings)
        + _encode_tail(settings, container="mp4")
        + "mux.audio_0 "
        + _mic_branch(settings)
        + _system_branch(settings, system_sink)
    )


def build_av_audio_mix_string(
    system_sink: str,
    *,
    mic_volume: float,
    system_volume: float,
    audio_bitrate: int,
    stereo_caps: str | None = None,
    mic_capture_backend: str = DEFAULT_MIC_CAPTURE_BACKEND,
    system_capture_backend: str = DEFAULT_SYSTEM_CAPTURE_BACKEND,
) -> str:
    """
    Return the audio branch fragment for a combined A/V pipeline (``mux.audio_0`` sink).

    Used by screen_recording.py when muxing portal video + audio in one MP4.
    """
    settings = AudioSettings(
        output_path="",
        system_sink=system_sink,
        mic_volume=mic_volume,
        system_volume=system_volume,
        audio_bitrate=audio_bitrate,
        mic_capture_backend=mic_capture_backend,
        system_capture_backend=system_capture_backend,
    )
    if stereo_caps:
        parts = stereo_caps.replace("audio/x-raw,", "").split(",")
        for part in parts:
            key, _, val = part.partition("=")
            if key == "rate":
                settings.sample_rate = int(val)
            elif key == "channels":
                settings.channels = int(val)
    return (
        _mixer_head(settings)
        + _encode_tail(settings, container="mp4")
        + "mux.audio_0 "
        + _mic_branch(settings)
        + _system_branch(settings, system_sink)
    )


def build_audio_pipeline_string(
    settings: AudioSettings,
    system_sink: str,
    *,
    container: ContainerFormat = "mp4",
) -> str:
    if container == "aac":
        return build_aac_pipeline_string(settings, system_sink)
    if container == "mp4":
        return build_mp4_audio_pipeline_string(settings, system_sink)
    return build_wav_pipeline_string(settings, system_sink)


class AudioRecorder:
    """Run a mic + system monitor GStreamer pipeline until EOS or stop."""

    def __init__(
        self,
        settings: AudioSettings,
        *,
        container: ContainerFormat = "mp4",
    ) -> None:
        settings.validate()
        self.settings = settings
        self.container = container
        self.system_sink = resolve_system_sink(settings.system_sink)

        init_gstreamer()
        self.pipeline: Gst.Pipeline | None = None
        self.loop = GLib.MainLoop()
        self._stop_requested = False

    def _build_pipeline(self) -> Gst.Pipeline:
        pipeline_string = build_audio_pipeline_string(
            self.settings,
            self.system_sink,
            container=self.container,
        )
        log("audio", f"Pipeline: {pipeline_string}")
        log("audio", f"System sink: {self.system_sink}")
        log(
            "audio",
            f"Capture backends: mic={self.settings.mic_capture_backend}, "
            f"system={self.settings.system_capture_backend}",
        )

        pipeline = Gst.parse_launch(pipeline_string)
        if not isinstance(pipeline, Gst.Pipeline):
            raise RuntimeError("GStreamer did not return a pipeline object")
        return pipeline

    def start(self) -> int:
        try:
            self.pipeline = self._build_pipeline()
        except Exception as exc:
            log("audio", f"Pipeline build failed: {exc}")
            return 1

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)

        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            log("audio", "Failed to set pipeline to PLAYING")
            return 1

        log("audio", f"Recording mic + system audio to: {self.settings.output_path}")
        log("audio", "Press Ctrl+C to stop and finalize.")
        GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, self._request_stop)
        self.loop.run()
        return 0

    def request_stop(self) -> None:
        """Send EOS to flush and finalize the output container."""
        if self._stop_requested:
            return
        self._stop_requested = True
        log("audio", "Stopping; sending EOS...")
        if self.pipeline is not None:
            self.pipeline.send_event(Gst.Event.new_eos())
        else:
            self.loop.quit()

    def _request_stop(self) -> bool:
        self.request_stop()
        return GLib.SOURCE_REMOVE

    def _on_bus_message(self, _bus: Gst.Bus, message: Gst.Message) -> None:
        if message.type == Gst.MessageType.EOS:
            log("audio", "EOS received. File finalized.")
            self._cleanup()
            self.loop.quit()
        elif message.type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            log("audio", f"GStreamer error: {err}")
            if debug:
                log("audio", f"Debug: {debug}")
            self._cleanup()
            self.loop.quit()
        elif message.type == Gst.MessageType.STATE_CHANGED and message.src == self.pipeline:
            _old, new, _pending = message.parse_state_changed()
            log("audio", f"Pipeline state -> {new.value_nick}")

    def _cleanup(self) -> None:
        if self.pipeline is None:
            return
        bus = self.pipeline.get_bus()
        bus.remove_signal_watch()
        self.pipeline.set_state(Gst.State.NULL)
        self.pipeline = None


def _default_output_path(container: ContainerFormat) -> str:
    if container == "aac":
        return default_audio_aac_path()
    if container == "mp4":
        return default_audio_mp4_path()
    return default_audio_wav_path()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Record microphone + system audio (PipeWire + GStreamer)"
    )
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="Output path (.wav, .mp4, or .aac depending on --container)",
    )
    parser.add_argument(
        "--container",
        choices=("wav", "mp4", "aac"),
        default="mp4",
        help="Output format: mp4 (default, matches maskai), wav=lossless, aac=raw AAC",
    )
    parser.add_argument(
        "--system-sink",
        default=None,
        help=f"PipeWire sink node.name for --system-backend pipewire "
        f"(default: wpctl inspect {WPCTL_DEFAULT_SINK})",
    )
    parser.add_argument(
        "--mic-backend",
        choices=("pulse", "pipewire"),
        default=DEFAULT_MIC_CAPTURE_BACKEND,
        help="Mic capture backend (default: pipewire)",
    )
    parser.add_argument(
        "--system-backend",
        choices=("pulse", "pipewire"),
        default=DEFAULT_SYSTEM_CAPTURE_BACKEND,
        help="Desktop audio capture backend (default: pipewire)",
    )
    parser.add_argument(
        "--mic-volume",
        type=float,
        default=DEFAULT_MIC_VOLUME,
        help="Microphone linear gain (default: 1.0)",
    )
    parser.add_argument(
        "--system-volume",
        type=float,
        default=DEFAULT_SYSTEM_VOLUME,
        help="Desktop audio linear gain, 1.0=unity (default: 1.0)",
    )
    parser.add_argument(
        "--audio-bitrate",
        type=int,
        default=DEFAULT_AUDIO_BITRATE,
        help="AAC bitrate in bps for mp4/aac containers (default: 192000)",
    )
    args = parser.parse_args()

    if args.output is None:
        args.output = _default_output_path(args.container)

    settings = AudioSettings(
        output_path=args.output,
        system_sink=args.system_sink,
        mic_volume=args.mic_volume,
        system_volume=args.system_volume,
        audio_bitrate=args.audio_bitrate,
        mic_capture_backend=args.mic_backend,
        system_capture_backend=args.system_backend,
    )

    try:
        settings.validate()
    except ValueError as exc:
        print(f"Invalid settings: {exc}", file=sys.stderr)
        return 1

    try:
        return AudioRecorder(settings, container=args.container).start()
    except RuntimeError as exc:
        print(f"Audio error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
