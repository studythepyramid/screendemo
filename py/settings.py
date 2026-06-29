"""
Centralized parameter settings for screendemo.

All modules should import tunables from here rather than hard-coding values.
"""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass
from pathlib import Path

# --- Paths ---

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TMP_FOLDER = PROJECT_ROOT / "tmp"
os.makedirs(TMP_FOLDER, exist_ok=True)

# --- Logging ---


def log(step: str, message: str) -> None:
    print(f"[{step}] {message}", flush=True)


# --- D-Bus: XDG Desktop Portal (ScreenCast) ---

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
PORTAL_SCREENCAST_IFACE = "org.freedesktop.portal.ScreenCast"
PORTAL_REQUEST_IFACE = "org.freedesktop.portal.Request"
PORTAL_SESSION_IFACE = "org.freedesktop.portal.Session"

SOURCE_MONITOR = 1
SOURCE_WINDOW = 2
CURSOR_MODE_METADATA = 4
PERSIST_MODE_DO_NOT = 0

PORTAL_RESPONSE_SUCCESS = 0
PORTAL_REQUEST_TIMEOUT_SEC = 60

# --- D-Bus: GNOME Shell Screencast ---

GNOME_SHELL_BUS = "org.gnome.Shell.Screencast"
GNOME_SHELL_PATH = "/org/gnome/Shell/Screencast"
GNOME_SHELL_IFACE = "org.gnome.Shell.Screencast"

# --- Audio / PipeWire / GStreamer ---

AUDIO_SAMPLE_RATE = 48_000
AUDIO_CHANNELS = 2
STEREO_CAPS = f"audio/x-raw,rate={AUDIO_SAMPLE_RATE},channels={AUDIO_CHANNELS}"

DEFAULT_MIC_VOLUME = 1.0
DEFAULT_SYSTEM_VOLUME = 1.0
DEFAULT_AUDIO_BITRATE = 192_000
MIN_AUDIO_BITRATE = 32_000
AUDIO_EOS_FINALIZE_TIMEOUT_SEC = 10

WPCTL_DEFAULT_SINK = "@DEFAULT_AUDIO_SINK@"
PULSE_DEFAULT_MONITOR = "@DEFAULT_SINK@.monitor"
PULSE_DEFAULT_SOURCE = "@DEFAULT_SOURCE@"
MIC_MEDIA_ROLE = "Communication"

# Native PipeWire capture — matches maskai/py/audio_rec.py (full level from t=0)
DEFAULT_MIC_CAPTURE_BACKEND = "pipewire"
DEFAULT_SYSTEM_CAPTURE_BACKEND = "pipewire"

# --- Output file naming ---

TIMESTAMP_FMT = "%Y%m%d-%H%M%S"


def timestamped_filename(prefix: str, ext: str) -> str:
    stamp = dt.datetime.now().strftime(TIMESTAMP_FMT)
    return f"{prefix}-{stamp}.{ext}"


def default_audio_mp4_path() -> str:
    return timestamped_filename("audio", "mp4")


def default_audio_aac_path() -> str:
    return timestamped_filename("audio", "aac")


def default_audio_wav_path() -> str:
    return timestamped_filename("audio", "wav")


def default_screen_mp4_path() -> str:
    return timestamped_filename("recording", "mp4")


def default_screen_mkv_path() -> str:
    return timestamped_filename("gnome_sr", "mkv")


def default_screen_webm_path() -> str:
    return timestamped_filename("screen", "webm")


# --- Screen recording ---

SCREEN_EOS_FINALIZE_TIMEOUT_SEC = 5
RECORDING_MODE_PORTAL = "portal"
RECORDING_MODE_GNOME = "gnome"
DEFAULT_RECORDING_MODE = RECORDING_MODE_GNOME


@dataclass
class ScreenRecordingSettings:
    """Desktop screen capture parameters (video only for now)."""

    output_path: str
    mode: str = DEFAULT_RECORDING_MODE
    draw_cursor: bool = True

    def validate(self) -> None:
        if self.mode == RECORDING_MODE_GNOME:
            if not (
                self.output_path.endswith(".webm")
                or self.output_path.endswith(".mkv")
                or self.output_path.endswith(".mp4")
            ):
                raise ValueError(
                    "GNOME Shell mode requires output ending with .webm, .mkv, or .mp4"
                )
        elif self.mode == RECORDING_MODE_PORTAL:
            if not self.output_path.endswith(".mp4"):
                raise ValueError("Portal mode requires output ending with .mp4")
        else:
            raise ValueError(f"Unknown recording mode: {self.mode}")


@dataclass
class AudioSettings:
    """Mic + system monitor capture parameters."""

    output_path: str
    system_sink: str | None = None
    mic_volume: float = DEFAULT_MIC_VOLUME
    system_volume: float = DEFAULT_SYSTEM_VOLUME
    audio_bitrate: int = DEFAULT_AUDIO_BITRATE
    sample_rate: int = AUDIO_SAMPLE_RATE
    channels: int = AUDIO_CHANNELS
    mic_capture_backend: str = DEFAULT_MIC_CAPTURE_BACKEND
    system_capture_backend: str = DEFAULT_SYSTEM_CAPTURE_BACKEND

    @property
    def stereo_caps(self) -> str:
        return f"audio/x-raw,rate={self.sample_rate},channels={self.channels}"

    def validate(self) -> None:
        if self.mic_volume <= 0:
            raise ValueError("mic_volume must be > 0")
        if self.system_volume <= 0:
            raise ValueError("system_volume must be > 0")
        if self.audio_bitrate < MIN_AUDIO_BITRATE:
            raise ValueError(f"audio_bitrate must be >= {MIN_AUDIO_BITRATE}")
