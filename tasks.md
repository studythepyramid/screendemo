
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


