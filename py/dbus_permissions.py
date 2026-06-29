"""
D-Bus permission and session negotiation for screen capture on GNOME Wayland.

Two capture paths:

1. **XDG Desktop Portal** (`org.freedesktop.portal.ScreenCast`)
   Shows the GNOME screen-share dialog. Flow:
   CreateSession → SelectSources → Start → OpenPipeWireRemote

2. **GNOME Shell Screencast** (`org.gnome.Shell.Screencast`)
   Direct shell API; bypasses the portal confirmation dialog.

Sources: maskai/py/sr1.py, maskai/py/avrec.py, maskai/py/gnome_sr.py
"""

from __future__ import annotations

import os
import random
from collections.abc import Callable
from typing import Any

import dbus
from dbus.mainloop.glib import DBusGMainLoop

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib

from py.settings import (
    CURSOR_MODE_METADATA,
    GNOME_SHELL_BUS,
    GNOME_SHELL_IFACE,
    GNOME_SHELL_PATH,
    PERSIST_MODE_DO_NOT,
    PORTAL_BUS,
    PORTAL_PATH,
    PORTAL_REQUEST_IFACE,
    PORTAL_REQUEST_TIMEOUT_SEC,
    PORTAL_RESPONSE_SUCCESS,
    PORTAL_SCREENCAST_IFACE,
    PORTAL_SESSION_IFACE,
    SOURCE_MONITOR,
    SOURCE_WINDOW,
    log,
)


def new_token(prefix: str) -> str:
    return f"{prefix}_{random.randint(0, 0xFFFFFF):06x}"


def setup_dbus_mainloop() -> None:
    """Register dbus-python with the GLib main loop (required before SessionBus)."""
    DBusGMainLoop(set_as_default=True)


def connect_session_bus() -> dbus.SessionBus:
    log(
        "dbus",
        f"Connecting to session bus "
        f"(DBUS={os.environ.get('DBUS_SESSION_BUS_ADDRESS', '<unset>')})",
    )
    return dbus.SessionBus()


class PortalScreenCastSession:
    """
    Negotiate screen-capture permission via the XDG Desktop Portal.

    Runs the 5-step async handshake and calls ``on_pipewire_ready(fd, node_id)``
    when the user approves sharing and a PipeWire remote FD is available.
    """

    def __init__(
        self,
        on_pipewire_ready: Callable[[int, int], None],
        *,
        on_error: Callable[[str], None] | None = None,
        on_session_closed: Callable[[], None] | None = None,
        on_finished: Callable[[], None] | None = None,
        source_types: int = SOURCE_MONITOR | SOURCE_WINDOW,
        multiple_sources: bool = True,
        request_timeout_sec: int = PORTAL_REQUEST_TIMEOUT_SEC,
    ) -> None:
        setup_dbus_mainloop()
        self.bus = connect_session_bus()
        portal = self.bus.get_object(PORTAL_BUS, PORTAL_PATH)
        self.screencast = dbus.Interface(portal, PORTAL_SCREENCAST_IFACE)
        self.loop = GLib.MainLoop()

        self.on_pipewire_ready = on_pipewire_ready
        self.on_error = on_error or (lambda msg: log("error", msg))
        self.on_session_closed = on_session_closed
        self.on_finished = on_finished
        self.source_types = source_types
        self.multiple_sources = multiple_sources
        self.request_timeout_sec = request_timeout_sec

        self.session_handle: str | None = None
        self._pending_step: str | None = None
        self._pending_callback: Callable[[int, Any], None] | None = None
        self._timeout_id: int | None = None
        self._session_closed_match: int | None = None

        self.bus.add_signal_receiver(
            self._on_portal_response,
            signal_name="Response",
            dbus_interface=PORTAL_REQUEST_IFACE,
            bus_name=PORTAL_BUS,
        )
        log("portal", "Response listener registered.")

    def begin(self) -> None:
        """Start CreateSession handshake (non-blocking; requires a running GLib main loop)."""
        log("portal", "Creating session...")
        session_token = new_token("session")
        self._portal_request(
            "1/5",
            "CreateSession",
            self._on_session_created,
            options={"session_handle_token": session_token},
        )

    def start(self) -> None:
        """Begin handshake and block in a dedicated GLib main loop until done."""
        self.begin()
        self.loop.run()

    def quit(self) -> None:
        self._clear_request_timeout()
        if self.loop.is_running():
            self.loop.quit()
        elif self.on_finished:
            self.on_finished()

    def close_session(self) -> None:
        """End the portal screencast session (stops the top-bar share indicator)."""
        if not self.session_handle:
            return
        try:
            session_obj = self.bus.get_object(PORTAL_BUS, self.session_handle)
            session = dbus.Interface(session_obj, PORTAL_SESSION_IFACE)
            session.Close()
            log("portal", f"Session.Close() called on {self.session_handle}")
        except dbus.DBusException as exc:
            log("portal", f"Session.Close failed: {exc}")

    def _register_session_closed_listener(self) -> None:
        if not self.session_handle or self._session_closed_match is not None:
            return
        self._session_closed_match = self.bus.add_signal_receiver(
            self._on_session_closed_signal,
            signal_name="Closed",
            dbus_interface=PORTAL_SESSION_IFACE,
            path=self.session_handle,
            bus_name=PORTAL_BUS,
        )
        log("portal", f"Listening for Session.Closed on {self.session_handle}")

    def _on_session_closed_signal(self) -> None:
        log("portal", "Portal session closed (tray stop or compositor ended share)")
        if self.on_session_closed:
            self.on_session_closed()

    def _clear_request_timeout(self) -> None:
        if self._timeout_id is not None:
            GLib.source_remove(self._timeout_id)
            self._timeout_id = None

    def _on_request_timeout(self) -> bool:
        step = self._pending_step or "unknown"
        self.on_error(
            f"{step}: timeout after {self.request_timeout_sec}s waiting for portal Response"
        )
        self._pending_step = None
        self._pending_callback = None
        self._timeout_id = None
        self.quit()
        return GLib.SOURCE_REMOVE

    def _start_request_timeout(self, step: str) -> None:
        self._clear_request_timeout()
        self._timeout_id = GLib.timeout_add_seconds(
            self.request_timeout_sec,
            self._on_request_timeout,
        )
        log(step, f"Waiting up to {self.request_timeout_sec}s for portal Response...")

    def _on_portal_response(self, response, results) -> None:
        step = self._pending_step or "unknown"
        log(step, f"Portal Response received (code={int(response)})")
        log(step, f"Portal results: {dict(results)!r}")

        callback = self._pending_callback
        self._pending_step = None
        self._pending_callback = None
        self._clear_request_timeout()

        if callback is None:
            log(step, "WARNING: unexpected portal Response (no pending request)")
            return

        callback(int(response), results)

    def _portal_request(
        self,
        step: str,
        method_name: str,
        callback: Callable[[int, Any], None],
        *args,
        options: dict | None = None,
    ) -> None:
        opts = dbus.Dictionary(options or {}, signature="sv")
        opts["handle_token"] = new_token("request")

        self._pending_step = step
        self._pending_callback = callback

        method = getattr(self.screencast, method_name)
        log(step, f"Calling ScreenCast.{method_name}()")
        log(step, f"Options: {dict(opts)!r}")

        try:
            request_path = str(method(*args, opts))
        except dbus.DBusException:
            self._pending_step = None
            self._pending_callback = None
            raise

        log(step, f"Portal created request object: {request_path}")
        self._start_request_timeout(step)

    def _fail(self, step: str, code: int) -> None:
        self.on_error(f"{step}: portal response code {code}")
        self.quit()

    def _on_session_created(self, response: int, results) -> None:
        if response != PORTAL_RESPONSE_SUCCESS:
            self._fail("1/5", response)
            return

        self.session_handle = str(results["session_handle"])
        log("1/5", f"Session created: {self.session_handle}")
        self._register_session_closed_listener()

        log("2/5", "Selecting capture sources...")
        self._portal_request(
            "2/5",
            "SelectSources",
            self._on_sources_selected,
            self.session_handle,
            options={
                "types": dbus.UInt32(self.source_types),
                "multiple": dbus.Boolean(self.multiple_sources),
                "cursor_mode": dbus.UInt32(CURSOR_MODE_METADATA),
                "persist_mode": dbus.UInt32(PERSIST_MODE_DO_NOT),
            },
        )

    def _on_sources_selected(self, response: int, _results) -> None:
        if response != PORTAL_RESPONSE_SUCCESS:
            self._fail("2/5", response)
            return

        log("2/5", "Sources configured")
        log("3/5", "Starting portal session (GNOME screen-share dialog should appear)...")
        self._portal_request(
            "3/5",
            "Start",
            self._on_started,
            self.session_handle,
            "",
        )

    def _on_started(self, response: int, results) -> None:
        if response != PORTAL_RESPONSE_SUCCESS:
            self._fail("3/5", response)
            return

        streams = results.get("streams", [])
        if not streams:
            self.on_error("3/5: portal returned no streams")
            self.quit()
            return

        log("3/5", f"User approved. Streams: {streams!r}")
        node_id = int(streams[0][0])
        log("3/5", f"PipeWire node id: {node_id}")

        log("4/5", "Opening PipeWire remote...")
        options = dbus.Dictionary({}, signature="sv")
        fd_result = self.screencast.OpenPipeWireRemote(self.session_handle, options)

        if hasattr(fd_result, "take"):
            fd = fd_result.take()
        else:
            fd = int(fd_result)

        log("4/5", f"PipeWire FD obtained: {fd!r}")
        self.on_pipewire_ready(fd, node_id)


class GnomeShellScreencast:
    """
    Start/stop full-screen recording via org.gnome.Shell.Screencast.

    GNOME Shell writes WebM video directly; audio is captured separately
    (see audio.py) and muxed afterward (see sink2mp4.py).
    """

    def __init__(self, bus: dbus.SessionBus | None = None) -> None:
        setup_dbus_mainloop()
        self.bus = bus or connect_session_bus()
        try:
            shell_obj = self.bus.get_object(GNOME_SHELL_BUS, GNOME_SHELL_PATH)
            self.screencast = dbus.Interface(shell_obj, GNOME_SHELL_IFACE)
        except dbus.DBusException as exc:
            raise RuntimeError(
                f"Failed to connect to GNOME Screencast D-Bus API: {exc}"
            ) from exc

    def start(self, output_path: str, *, draw_cursor: bool = True) -> str:
        """
        Begin GNOME Shell screencast to ``output_path`` (must be absolute).

        Returns the actual filename GNOME Shell chose.
        """
        options = dbus.Dictionary(
            {"draw-cursor": dbus.Boolean(draw_cursor)},
            signature="sv",
        )
        success, actual_filename = self.screencast.Screencast(output_path, options)
        if not success:
            raise RuntimeError("GNOME Shell Screencast failed to start")
        log("gnome-screencast", f"Recording video to: {actual_filename}")
        return str(actual_filename)

    def stop(self) -> None:
        try:
            self.screencast.StopScreencast()
            log("gnome-screencast", "Stopped.")
        except dbus.DBusException as exc:
            log("gnome-screencast", f"Stop failed: {exc}")
