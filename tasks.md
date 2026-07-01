# July 01

## 0754am

test the gnome shell path
UI status show Mic. Sys Audio, freedesktop portal, etc.
UI buttons to turn on/off Mic and system audio


# june 30

## chop off system dock and tray

Yes — **ImageMagick can do this well**. It’s a good fit for one-off CLI crops and for scripting after `snapshot.py` saves a PNG.

## Left dock: remove 32 px

**ImageMagick 7** (Ubuntu default):

```bash
magick input.png -gravity West -chop 32x0 output.png
```

That chops 32 pixels off the **left** edge and keeps the rest.

Equivalent with explicit crop (if image is 1920×1080):

```bash
magick input.png -crop 1888x1080+32+0 +repage output.png
```

(`width = total_width - 32`, start at `x=32`, `y=0`.)

**Older `convert` syntax** (same idea):

```bash
convert input.png -gravity West -chop 32x0 output.png
```

## Top bar / system tray area

On GNOME the tray is usually in the **top** bar, not the bottom. To remove ~32 px from the top:

```bash
magick input.png -gravity North -chop 0x32 output.png
```

## Both left dock and top bar

Order matters: chop left first, then top:

```bash
magick input.png \
  -gravity West -chop 32x0 \
  -gravity North -chop 0x32 \
  output.png
```

Adjust `32` if your bar is taller (often 34–48 px at 100% scale).

## HiDPI note

If display scaling is **200%**, the dock may be **64 physical pixels** wide even if it looks like “32” in GNOME settings. Check one screenshot:

```bash
magick input.png -format "%wx%h" info:
```

Then inspect in an image viewer where the dock ends.

## Other tools (if you prefer)

| Tool | Left 32 px crop |
|------|------------------|
| **ffmpeg** | `ffmpeg -i in.png -vf "crop=iw-32:ih:32:0" out.png` |
| **Pillow (Python)** | `img.crop((32, 0, width, height))` — fits screendemo later |

## Practical workflow for you

```bash
uv run py/snapshot.py -o tmp/snap.png
magick tmp/snap.png -gravity West -chop 32x0 -gravity North -chop 0x32 tmp/snap-cropped.png
```

## Integration idea (later)

Add optional crop insets in `settings.py`, e.g. `SNAPSHOT_CROP_LEFT=32`, `SNAPSHOT_CROP_TOP=32`, and run ImageMagick or Pillow after capture. That keeps the UI delay + hide-window flow you already have.

If you want this wired into `snapshot.py` or the UI, switch to Agent mode and we can add it.


# June 27

## 0042am

Set the github remote, 
it's currently cloned with https://...
set remote according to ~/.ssh/config,  github-pyramid

## 0700

Hi cursor, please read the scripts in  ~/dev/maskai/py/
There're screen recordings and captures of camera and audio,
let's reuse it and arrange to lib style files and functions.
Please review the splitting of functionalities as follow:

dbus_permissions.py 
screen_recording.py
sink2mp4.py
audio.py
camera.py
ui.py : serve as window with buttons to start recording, enter file name, and so on.

For multiple programming languages, 
put .py scripts into py/
we might need GDScripts as well, they're going to have it own folder according to each specifications of their own settings.

---

Proposed split — review
Module	Responsibility	Source
dbus_permissions.py
Portal handshake + GNOME Shell Sreencast D-Bus

sr1.py, avrec.py, gnome_sr.py

audio.py
PipeWire mic + system sink mixing, resolve_system_sink(), volume/bitrate
audio_rec.py, gnome_sr.py, avrec.py

sink2mp4.py
Record a PipeWire sink monitor → MP4; also mux temp WebM + AAC → final container
audio_rec.py (sink-only variant), gnome_sr.py mux_files()

screen_recording.py
Orchestrate video capture (portal or GNOME path) + optional audio
sr1.py, avrec.py, gnome_sr.py

camera.py
OpenCV capture, preview, record to file
get_camera.py

ui.py
Window: filename, start/stop, volume controls
new

That split looks right. Shared D-Bus logic was duplicated across three scripts; pulling it into dbus_permissions.py is the right first step.

Note on sink2mp4.py: there is no file with that name in maskai — the name fits two jobs: (1) sink monitor → MP4, and (2) post-record mux after GNOME Shell writes WebM. Both can live in one module as separate functions.

GDScript: keep under something like godot/ later; Python stays in py/.

Step 1 done: py/dbus_permissions.py
Created:

screendemo/
  pyproject.toml
  py/
    __init__.py
    dbus_permissions.py   ← done
PortalScreenCastSession — async portal flow from sr1.py / avrec.py: CreateSession → SelectSources → Start → OpenPipeWireRemote, then calls on_pipewire_ready(fd, node_id).

GnomeShellScreencast — start/stop wrapper from gnome_sr.py (no portal dialog).

Helpers: setup_dbus_mainloop(), connect_session_bus(), new_token(), constants.

Planned order (one by one)
dbus_permissions.py ✓
audio.py — next
sink2mp4.py
screen_recording.py
camera.py
ui.py
Say when to proceed with audio.py.
