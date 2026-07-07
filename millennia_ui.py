#!/usr/bin/env python3
"""Millennia eV laser control GUI.

A small myTk front-end for the Spectra-Physics Millennia eV CW pump laser,
driven through PyHardwareLibrary's ``MillenniaDevice``. It offers:

  * a diode On/Off button (the pump diodes, command ON/OFF / query ?D),
  * a shutter Open/Close button (the mechanical block, SHT:1/SHT:0 / ?SHT),
  * a power monitor showing output power (?P) and the live diode/shutter state.

All serial I/O runs on a single background worker thread owned by
PyHardwareLibrary's ``DeviceController`` (poll, connect, and user commands all
flow through it), so the slow confirm-and-retry actions (the eV does not ack
ON/OFF, so the driver writes, settles, and reads back -- up to several seconds)
never freeze the UI. The controller reports connect/disconnect/status/failure
through the ``NotificationCenter``; this app observes those and marshals each
onto the Tk main thread via ``App.schedule_on_main_thread``, where they are
assigned to Bindable properties that update the widgets automatically.

If the laser's serial port cannot be opened because another program holds it
(on this bench the eV's USB port is routinely captured by Parallels for the
Windows "Spectra-Physics" app), the status line says so explicitly instead of
failing silently. Use the Simulator checkbox to drive a DebugMillenniaDevice
when no hardware is reachable.

The connected laser is discovered by the driver; pass --port only to pin a
specific serial port.

Run:
    python millennia_ui.py                 # discover the connected laser
    python millennia_ui.py --port /dev/cu.usbmodemXXXX
    python millennia_ui.py --simulate      # no hardware needed

The same executable is also a command-line remote for a *running* app (it talks
to the RPC server RemoteControllable exposes):

    python millennia_ui.py ctl status      # on/off/open/close/status
    python millennia_ui.py install-cli     # add a `millenia-ctl` command to PATH
    millenia-ctl on                        # once installed
"""

import argparse
import os
import sys
from contextlib import suppress

from mytk import (
    App,
    BooleanIndicator,
    Box,
    Button,
    Dialog,
    Label,
    Level,
    NumericIndicator,
    RemoteControllable,
)

from hardwarelibrary.sources.millennia import (
    MillenniaDevice,
    DebugMillenniaDevice,
)
from hardwarelibrary.devicecontroller import (
    DeviceController,
    DeviceControllerNotification as N,
    connectionErrorReason,
)
from hardwarelibrary.notificationcenter import NotificationCenter

# Version is injected at build time by packaging/make_version.py (from the git
# tag); falls back to a dev marker when running from a checkout.
try:
    from _version import __version__
except Exception:
    __version__ = "0.0.0+dev"

# Device discovery is PyHardwareLibrary's job, not the app's: MillenniaDevice()
# finds the connected laser itself (by its own criteria). We only pass a
# portPath when the user pins one with --port. Keeping USB-id knowledge in the
# driver also avoids matching the eV's generic STM32 identity from here.


# Serial-error classification (busy / permission / missing) now lives in
# PyHardwareLibrary as devicecontroller.connectionErrorReason, which walks the
# exception chain the driver preserves. _connection_message() uses it.



# TODO(mytk-1.6.1): this decorator and the tag-scan in _register_remote_api are
# slated to move into mytk's RemoteControllable. Once this project requires
# mytk >= 1.6.1, DELETE this local copy, import remote_command from mytk, and
# replace the scan loop with self.register_remote_commands() (see below).
def remote_command(fct=None, *, name=None):
    """Mark a method for remote exposure by ``_register_remote_api``.

    This is only a *tag*: it records the RPC name on the function and returns it
    unchanged. The actual registration happens at runtime, once an instance (and
    its ``self.remote`` registry) exists — ``@app.remote`` cannot be used in the
    class body because there is no live app there yet. Use bare
    (``@remote_command``) or with an explicit name (``@remote_command(name="status")``).
    """
    if fct is None:
        return lambda f: remote_command(f, name=name)
    fct._remote_name = name or fct.__name__
    return fct


class MillenniaApp(App, RemoteControllable):
    """A myTk App that builds the window and wires the widgets to the controller.

    Mixes in `RemoteControllable`, so the laser commands (turn_on, turn_off,
    open_shutter, close_shutter) and a status query are reachable over RPC from
    another process via ``mytk.connect(...)`` (localhost only, see
    :meth:`_register_remote_api`).
    """

    HELP_URL = "https://github.com/DCC-Lab/MilleniaUI"

    MAX_POWER = 30.0  # full-scale of the power Level bar (W)

    STATUS_COLORS = {
        "ok": "#1a7f37",
        "info": "#57606a",
        "warn": "#9a6700",
        "error": "#cf222e",
    }

    def __init__(self, simulate=False, port=None, remote=True, remote_port=8777):
        self.simulate_arg = simulate
        self.port_arg = port
        self.remote_enabled = remote
        self.remote_port = remote_port

        # Last known truth, so a button click can send the *opposite* action.
        # These drive *derived* UI (button labels/enablement, status colour) and
        # are observed rather than value-bound (see _bind_state_to_ui).
        self.connected = False
        self.monitoring = False  # disconnected but auto-reconnecting
        self.busy = False
        # Whether each laser state is currently known (False before the first
        # poll and after a drop); drives the "—" text and disables the buttons.
        self.diode_known = False
        self.shutter_known = False

        # Displayed state value-bound 1:1 to widgets via the Bindable mixin:
        # assigning any of these pushes the value straight to its widget(s).
        # diode_on / shutter_open feed the on/off indicators, so they are plain
        # bools (never None — a BooleanVar cannot hold None).
        self.diode_on = False
        self.shutter_open = False
        self.power = 0.0
        self.identity = ""
        self.status = "Starting…"
        self.status_kind = "info"

        super().__init__(
            geometry="640x460",
            name="Millennia eV Control",
            help_url=self.HELP_URL,
        )
        # App titles the window from `name`; use a richer title with the version.
        self.root.title(
            "Millennia eV — Laser Control  (v{0})".format(__version__))

        # PyHardwareLibrary's DeviceController owns the worker thread and reports
        # through the NotificationCenter; it is created in _start_controller once
        # the UI exists to observe it.
        self.device = None
        self.controller = None

        self._build_ui()
        self._bind_state_to_ui()

        # Clicking the window's close box routes through quit() so the worker
        # thread and device are released cleanly.
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

        self._refresh_ui()

        # Build the device + DeviceController and auto-connect. Done before the
        # RPC server starts so a remote call can never see a None controller.
        self._start_controller()

        # Expose the laser commands over RPC (localhost) if enabled.
        self._register_remote_api()

    # -- UI construction --

    def _build_ui(self):
        window = self.window

        # --- Diodes (On/Off) -------------------------------------------
        diode_box = Box(label="Pump Diodes")
        diode_box.grid_into(window, column=0, row=0, padx=8, pady=6, sticky="nsew")

        self.diode_indicator = BooleanIndicator(diameter=18)
        self.diode_indicator.grid_into(diode_box, column=0, row=0, padx=8, pady=8)
        self.diode_state_label = Label(text="—")
        self.diode_state_label.grid_into(diode_box, column=1, row=0,
                                         padx=4, pady=8, sticky="w")
        self.diode_button = Button(
            "Turn On", width=12, user_event_callback=self._on_diode_clicked
        )
        self.diode_button.grid_into(diode_box, column=0, row=1, columnspan=2,
                                    padx=8, pady=(0, 8))

        # --- Shutter (Open/Close) --------------------------------------
        shutter_box = Box(label="Shutter")
        shutter_box.grid_into(window, column=1, row=0, padx=8, pady=6, sticky="nsew")

        self.shutter_indicator = BooleanIndicator(diameter=18)
        self.shutter_indicator.grid_into(shutter_box, column=0, row=0, padx=8, pady=8)
        self.shutter_state_label = Label(text="—")
        self.shutter_state_label.grid_into(shutter_box, column=1, row=0,
                                           padx=4, pady=8, sticky="w")
        self.shutter_button = Button(
            "Open Shutter", width=12, user_event_callback=self._on_shutter_clicked
        )
        self.shutter_button.grid_into(shutter_box, column=0, row=1, columnspan=2,
                                      padx=8, pady=(0, 8))

        # --- Power monitor ---------------------------------------------
        power_box = Box(label="Power Monitor")
        power_box.grid_into(window, column=0, row=1, columnspan=2,
                            padx=8, pady=6, sticky="nsew")

        # No explicit value_variable: NumericIndicator makes its own, and
        # _bind_state_to_ui binds our `power` property to it.
        self.power_indicator = NumericIndicator(format_string="{0:.2f} W")
        self.power_indicator.grid_into(power_box, column=0, row=0,
                                       padx=8, pady=(8, 2), sticky="w")

        # A wide fixed-size bar that spans the box. (We deliberately do NOT
        # stretch it via a weighted cell + <Configure> handler: mytk's
        # CanvasView.on_resize calls update_idletasks(), so resizing the canvas
        # from within a resize callback recurses until RecursionError.)
        self.power_level = Level(maximum=self.MAX_POWER, width=600, height=22)
        self.power_level.grid_into(power_box, column=0, row=1,
                                   padx=8, pady=(2, 10), sticky="w")

        # --- Connection (kept at the bottom: normally untouched) --------
        conn_box = Box(label="Connection")
        conn_box.grid_into(window, column=0, row=2, columnspan=2,
                           padx=8, pady=6, sticky="nsew")

        self.status_label = Label(text="Starting…")
        self.status_label.grid_into(conn_box, column=0, row=0, columnspan=2,
                                    padx=8, pady=(6, 2), sticky="w")

        self.identity_label = Label(text="")
        self.identity_label.grid_into(conn_box, column=0, row=1, columnspan=2,
                                      padx=8, pady=(0, 4), sticky="w")

        self.connect_button = Button(
            "Connect", user_event_callback=self._on_connect_clicked
        )
        self.connect_button.grid_into(conn_box, column=0, row=2,
                                      padx=8, pady=6, sticky="w")

        window.all_resize_weight(1)

    # -- state <-> UI binding (Bindable mixin) --

    # State that drives *derived* UI (ON/OFF text, button labels/enablement,
    # status colour) which cannot be value-bound 1:1, so we observe it and
    # recompute in observed_property_changed. diode_on/shutter_open appear here
    # too: besides driving their (value-bound) indicators, they change the button
    # labels and the ON/OFF text.
    DERIVED_TRIGGERS = frozenset(
        {"connected", "monitoring", "busy", "diode_on", "diode_known",
         "shutter_open", "shutter_known", "status_kind"}
    )

    def _bind_state_to_ui(self):
        """Connect model state to widgets with the Bindable mixin.

        Two-way value bindings keep a property and a widget's ``value_variable``
        synchronised automatically, so assigning the property updates the widget
        (and vice-versa). State that first needs a transformation before it can
        be shown is handled by observing it and recomputing the derived UI in
        :meth:`observed_property_changed`.

        Must run after :meth:`_build_ui`: a widget's ``value_variable`` only
        exists once the widget has been placed on screen.
        """
        # Direct value bindings — one property, possibly several widgets.
        self.bind_property_to_widget_value("diode_on", self.diode_indicator)
        self.bind_property_to_widget_value("shutter_open", self.shutter_indicator)
        self.bind_property_to_widget_value("power", self.power_indicator)
        self.bind_property_to_widget_value("power", self.power_level)
        self.bind_property_to_widget_value("identity", self.identity_label)
        self.bind_property_to_widget_value("status", self.status_label)

        # Derived UI — observe the raw state; recompute in the callback.
        for name in self.DERIVED_TRIGGERS:
            self.add_observer(self, name)

    def observed_property_changed(self, observed, name, value, context):
        # Let Bindable service the two-way value bindings first...
        super().observed_property_changed(observed, name, value, context)
        # ...then recompute anything that is a *function* of the raw state.
        if name in self.DERIVED_TRIGGERS:
            self._refresh_ui()

    def _refresh_ui(self):
        """Recompute the derived UI from the current state.

        The on/off indicators are value-bound to diode_on / shutter_open, so
        they need no work here — only the tri-state *text* ("—"/ON/OFF), the
        status colour, and the buttons are functions of state.
        """
        self.diode_state_label.value_variable.set(
            self._state_text(self.diode_known, self.diode_on, "ON", "OFF"))
        self.shutter_state_label.value_variable.set(
            self._state_text(self.shutter_known, self.shutter_open, "OPEN", "CLOSED"))

        color = self.STATUS_COLORS.get(self.status_kind, "#000000")
        with suppress(Exception):
            self.status_label.widget.configure(foreground=color)

        self._update_controls_enabled()

    # -- DeviceController lifecycle --

    def _make_device(self, simulate, port):
        """Build the PhysicalDevice the controller will drive.

        A port path is optional: with --port it is pinned, otherwise the device
        discovers the connected laser itself (MillenniaDevice knows how to find
        its instance). DebugMillenniaDevice is used in --simulate.
        """
        if simulate:
            return DebugMillenniaDevice()
        if port:
            return MillenniaDevice(portPath=port)
        return MillenniaDevice()

    def _start_controller(self):
        """Create the DeviceController for the current device and connect.

        The controller owns the single worker thread; we observe its
        notifications and marshal each onto the Tk main thread.
        """
        self.device = self._make_device(self.simulate_arg, self.port_arg)
        self.controller = DeviceController(self.device)
        self._observe_controller()
        self.controller.start()
        self.controller.connect()

    def _stop_controller(self):
        """Stop the worker and drop our observers (safe if never started)."""
        NotificationCenter().removeObserver(self)
        if self.controller is not None:
            self.controller.stop()
            self.controller = None

    def _observe_controller(self):
        nc = NotificationCenter()
        for name in (N.didConnect, N.didDisconnect, N.connectionLost,
                     N.connectionFailed, N.status, N.commandFailed):
            nc.addObserver(self, self._controller_did_post, name, self.controller)

    # -- controller notifications -> main thread -> bound state --

    def _controller_did_post(self, notification):
        # Runs on the controller's worker thread; bounce onto the Tk main thread
        # before touching state (bindings/observers then update the widgets).
        self.schedule_on_main_thread(
            self._apply_notification,
            args=(notification.name, notification.userInfo))

    def _apply_notification(self, name, user_info):
        """Translate one controller notification into bound state assignments.

        The Bindable bindings/observers do the rest; we only ensure the two
        never-None display values (power, identity) stay valid.
        """
        if name is N.didConnect:
            self.identity = self._identity(user_info)
            self.connected = True
            self.monitoring = False
            self.busy = False
            self.status = "Connected"
            self.status_kind = "ok"
        elif name is N.status:
            laser_on = user_info.get("isLaserOn")
            shutter_open = user_info.get("isShutterOpen")
            self.diode_on = bool(laser_on)
            self.diode_known = laser_on is not None
            self.shutter_open = bool(shutter_open)
            self.shutter_known = shutter_open is not None
            power = user_info.get("power")
            self.power = power if power is not None else 0.0
            self.busy = False
        elif name is N.connectionLost:
            self._enter_monitoring(
                "Connection lost ({0}). Monitoring port — will reconnect "
                "automatically.".format(user_info))
        elif name is N.connectionFailed:
            self._enter_monitoring(self._connection_message(user_info))
        elif name is N.didDisconnect:
            self.connected = False
            self.monitoring = False
            self.busy = False
            self.identity = ""
            self._clear_laser_state()
            self.status = "Disconnected"
            self.status_kind = "info"
        elif name is N.commandFailed:
            self.busy = False
            self.status = "Command failed: {0}".format(user_info)
            self.status_kind = "error"

    def _clear_laser_state(self):
        """Mark the laser state unknown (indicators off, "—" text, power 0)."""
        self.diode_on = False
        self.diode_known = False
        self.shutter_open = False
        self.shutter_known = False
        self.power = 0.0

    def _enter_monitoring(self, message):
        """Common state for a lost/failed connection that will be retried."""
        self.connected = False
        self.monitoring = self.controller.autoReconnect
        self.busy = False
        self._clear_laser_state()
        self.status = message
        self.status_kind = "warn"

    @staticmethod
    def _identity(device):
        parts = [getattr(device, "manufacturer", None),
                 getattr(device, "model", None)]
        label = " ".join(p for p in parts if p) or "Millennia"
        serial_number = getattr(device, "laserSerialNumber", None)
        firmware = getattr(device, "firmwareVersion", None)
        if serial_number:
            label += "   S/N {0}".format(serial_number)
        if firmware:
            label += "   fw {0}".format(firmware)
        return label

    @staticmethod
    def _connection_message(error):
        """Turn a connect failure into a user-facing line via the driver's
        preserved error chain (connectionErrorReason from PyHardwareLibrary)."""
        reason = connectionErrorReason(error)
        if reason == "busy":
            return ("The laser's serial port is busy — another program is using "
                    "it (e.g. the Spectra-Physics Windows app under Parallels). "
                    "Close it and it will reconnect automatically.")
        if reason == "permission":
            return "Permission denied opening the laser's serial port."
        if reason == "missing":
            return ("The laser is not available (no connected Millennia found). "
                    "Waiting for it to reappear…")
        return "Could not connect: {0}".format(error or type(error).__name__)

    @staticmethod
    def _state_text(known, value, true_text, false_text):
        """The tri-state label text: "—" when unknown, else true/false text."""
        return "—" if not known else (true_text if value else false_text)

    # -- control enable/disable + button labels --

    def _update_controls_enabled(self):
        connected = self.connected
        idle = connected and not self.busy

        # While monitoring (disconnected but auto-reconnecting), the button
        # reads "Disconnect" so a click cancels the auto-retry.
        self.connect_button.label = (
            "Disconnect" if (connected or self.monitoring) else "Connect"
        )
        self.diode_button.is_disabled = not idle or not self.diode_known
        self.shutter_button.is_disabled = not idle or not self.shutter_known

        self.diode_button.label = (
            "Turn Off" if self.diode_on else "Turn On"
        )
        self.shutter_button.label = (
            "Close Shutter" if self.shutter_open else "Open Shutter"
        )

    # -- callbacks (main thread) --

    def _on_connect_clicked(self, event, button):
        # "Disconnect" while connected OR while auto-reconnecting (cancels the
        # monitoring); otherwise start connecting.
        if self.connected or self.monitoring:
            self.controller.disconnect()
        else:
            self.controller.connect()

    def _on_diode_clicked(self, event, button):
        if not self.diode_known:
            return
        self.turn_off() if self.diode_on else self.turn_on()

    def _on_shutter_clicked(self, event, button):
        if not self.shutter_known:
            return
        self.close_shutter() if self.shutter_open else self.open_shutter()

    # -- laser commands (invoked by the buttons; the surface exposed over RPC
    #    via RemoteControllable). Each submits a device action onto the
    #    controller's worker thread; DeviceController rejects it (commandFailed)
    #    when not connected, so they are safe to call any time and take no
    #    arguments (RPC-friendly). --

    def _submit(self, action):
        # Mark busy immediately (the observer disables the controls); the next
        # status/commandFailed notification clears it.
        self.busy = True
        self.controller.submit(action)

    @remote_command
    def turn_on(self):
        """Turn the pump diodes on."""
        self._submit(lambda device: device.turnOn())

    @remote_command
    def turn_off(self):
        """Turn the pump diodes off."""
        self._submit(lambda device: device.turnOff())

    @remote_command
    def open_shutter(self):
        """Open the shutter."""
        self._submit(lambda device: device.openShutter())

    @remote_command
    def close_shutter(self):
        """Close the shutter."""
        self._submit(lambda device: device.closeShutter())

    # -- remote control (RemoteControllable) --

    def _register_remote_api(self):
        """Expose every ``@remote_command`` method over RPC and start the server.

        RemoteControllable marshals each remote call onto the Tk main thread, so
        the exposed functions are exactly the ones the buttons call. Clients
        connect with ``mytk.connect(port=..., app_name="Millennia eV Control")``
        and may call turn_on/turn_off/open_shutter/close_shutter or ``status()``.
        """
        if not self.remote_enabled:
            return
        # Read the @remote_command tag off the class (not the instance) so we
        # never trigger a property getter while scanning.
        # TODO(mytk-1.6.1): replace this loop with self.register_remote_commands()
        # once RemoteControllable provides it.
        for attr_name in dir(type(self)):
            tagged = getattr(type(self), attr_name, None)
            remote_name = getattr(tagged, "_remote_name", None)
            if remote_name is not None:
                self.remote(getattr(self, attr_name), name=remote_name)
        bound_port = self.start_remote(port=self.remote_port, app_name=self.name)
        print("Remote control listening on 127.0.0.1:{0} (app '{1}').".format(
            bound_port, self.name))

    @remote_command(name="status")
    def remote_status(self):
        """Return a snapshot of the laser state for remote clients.

        All values are XML-RPC serializable. ``diodes_on`` / ``shutter_open``
        are None while unknown (not yet polled, or disconnected).
        """
        return {
            "connected": self.connected,
            "monitoring": self.monitoring,
            "busy": self.busy,
            "diodes_on": self.diode_on if self.diode_known else None,
            "shutter_open": self.shutter_open if self.shutter_known else None,
            "power": self.power,
            "status": self.status,
            "identity": self.identity,
        }

    # -- App lifecycle / menu overrides --

    def quit(self):
        """Release the worker thread and device, then tear the window down."""
        try:
            self._stop_controller()
        finally:
            super().quit()

    def save(self):
        """No document model to save; the File ▸ Save… menu item is inert here."""
        Dialog.showinfo(
            title="Nothing to save",
            message="This application controls the laser live; there is no "
                    "document to save.",
        )

    def preferences(self):
        """No preferences UI yet."""
        Dialog.showinfo(
            title="Preferences",
            message="There are no preferences for this application.",
        )


# -- millenia-ctl : command-line remote control ------------------------------

CTL_ACTIONS = {
    "on": "turn_on",
    "off": "turn_off",
    "open": "open_shutter",
    "close": "close_shutter",
}


def cli_main(argv):
    """``millenia-ctl`` — drive a *running* MilleniaUI app over its RPC server.

    This is the same executable as the GUI, invoked either through the
    ``millenia-ctl`` symlink (see :func:`install_cli`) or as ``… ctl <cmd>``.
    It only talks to an already-running app, so it stays lightweight (the
    transport is stdlib XML-RPC via ``mytk.connect``).
    """
    parser = argparse.ArgumentParser(
        prog="millenia-ctl",
        description="Control a running MilleniaUI laser application over its "
                    "remote-control (RPC) server.",
    )
    parser.add_argument(
        "command", choices=["status", "on", "off", "open", "close"],
        help="status; on/off (pump diodes); open/close (shutter).",
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="Server host (default 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8777,
                        help="Server port (default 8777).")
    args = parser.parse_args(argv)

    import mytk

    try:
        remote = mytk.connect(host=args.host, port=args.port,
                              app_name="Millennia eV Control")
    except mytk.RemoteAppMismatch as err:
        print("Reached a server, but it is not MilleniaUI: {0}".format(err),
              file=sys.stderr)
        return 2
    except Exception as err:  # connection refused, etc.
        print("Cannot reach MilleniaUI at {0}:{1} — is the app running with its "
              "remote server enabled (i.e. not launched with --no-remote)? [{2}]"
              .format(args.host, args.port, err), file=sys.stderr)
        return 2

    try:
        if args.command == "status":
            status = remote.status()
            width = max((len(k) for k in status), default=0)
            for key, value in status.items():
                print("{0:<{1}}  {2}".format(key, width, value))
        else:
            method = CTL_ACTIONS[args.command]
            getattr(remote, method)()
            print("Sent: {0}".format(method))
    except Exception as err:
        print("Remote call failed: {0}".format(err), file=sys.stderr)
        return 1
    return 0


def install_cli():
    """Install a ``millenia-ctl`` command on the user's PATH.

    Symlinks ``millenia-ctl`` to this program (the app bundle's binary when
    frozen, else the source script). The link dispatches to CLI mode because
    :func:`main` inspects its own invoked name. Tries ``/usr/local/bin`` first,
    then ``~/.local/bin``, then ``~/bin`` — the first directory it can write to
    wins — so it works with or without admin rights.
    """
    from pathlib import Path

    link_name = "millenia-ctl"
    target = Path(sys.executable if getattr(sys, "frozen", False)
                  else __file__).resolve()
    candidates = [
        Path("/usr/local/bin"),
        Path.home() / ".local" / "bin",
        Path.home() / "bin",
    ]

    problems = []
    for directory in candidates:
        link = directory / link_name
        try:
            directory.mkdir(parents=True, exist_ok=True)
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(target)
        except OSError as err:
            problems.append("{0} ({1})".format(directory, err))
            continue

        print("Installed {0} -> {1}".format(link, target))
        if str(directory) not in os.environ.get("PATH", "").split(os.pathsep):
            print("\nNote: {0} is not on your PATH. Add it to your shell "
                  "profile:\n    export PATH=\"{0}:$PATH\"".format(directory))
        print("\nThen, with the app running:\n    {0} status".format(link_name))
        return 0

    print("Could not install {0}. Tried:".format(link_name), file=sys.stderr)
    for problem in problems:
        print("  " + problem, file=sys.stderr)
    return 1


def main():
    """Entry point: dispatch to the CLI, the installer, or the GUI."""
    invoked = os.path.basename(sys.argv[0]).lower()
    if invoked.startswith("millenia-ctl"):
        return cli_main(sys.argv[1:])
    if len(sys.argv) > 1:
        if sys.argv[1] == "ctl":
            return cli_main(sys.argv[2:])
        if sys.argv[1] == "install-cli":
            return install_cli()
    return gui_main()


def gui_main():
    parser = argparse.ArgumentParser(description="Millennia eV laser control GUI")
    parser.add_argument(
        "--version", action="version", version="MilleniaUI {0}".format(__version__),
    )
    parser.add_argument(
        "--simulate", action="store_true",
        help="Use the built-in DebugMillenniaDevice (no hardware needed).",
    )
    parser.add_argument(
        "--port", default=None,
        help="Serial port path to pin (e.g. /dev/cu.usbmodemXXXX); optional — "
             "the connected laser is auto-discovered if omitted.",
    )
    parser.add_argument(
        "--no-remote", action="store_true",
        help="Do not start the localhost remote-control (RPC) server.",
    )
    parser.add_argument(
        "--remote-port", type=int, default=8777,
        help="Port for the remote-control server (default 8777).",
    )
    # parse_known_args so a stray argument from a Finder/.app launch (e.g. an
    # old-style -psn_ process serial number) never aborts startup.
    args, _ = parser.parse_known_args()

    try:
        MillenniaApp(
            simulate=args.simulate,
            port=args.port,
            remote=not args.no_remote,
            remote_port=args.remote_port,
        ).mainloop()
    except Exception:
        # A bundled .app has no terminal, so a startup crash would vanish.
        # Record it where the user (or a developer) can find it.
        import traceback
        from pathlib import Path

        log_dir = Path.home() / "Library" / "Logs"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            with open(log_dir / "MilleniaUI.log", "a") as handle:
                handle.write("=== MilleniaUI crash ===\n")
                traceback.print_exc(file=handle)
        except Exception:
            pass
        raise


if __name__ == "__main__":
    sys.exit(main())
