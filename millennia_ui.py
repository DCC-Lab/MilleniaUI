#!/usr/bin/env python3
"""Millennia eV laser control GUI.

A small myTk front-end for the Spectra-Physics Millennia eV CW pump laser,
driven through PyHardwareLibrary's ``MillenniaDevice``. It offers:

  * a diode On/Off button (the pump diodes, command ON/OFF / query ?D),
  * a shutter Open/Close button (the mechanical block, SHT:1/SHT:0 / ?SHT),
  * a power monitor showing output power (?P) and the live diode/shutter state.

All serial I/O runs on a single background worker thread so the slow,
confirm-and-retry actions (the eV does not ack ON/OFF, so the driver writes,
settles, and reads back -- up to several seconds) never freeze the UI. State
is pushed back to the Tk main thread via ``App.schedule_on_main_thread``.

If the laser's serial port cannot be opened because another program holds it
(on this bench the eV's USB port is routinely captured by Parallels for the
Windows "Spectra-Physics" app), the status line says so explicitly instead of
failing silently. Use the Simulator checkbox to drive a DebugMillenniaDevice
when no hardware is reachable.

Run:
    python millennia_ui.py                 # auto-detect the Millennia serial port
    python millennia_ui.py --port /dev/cu.usbmodemXXXX
    python millennia_ui.py --simulate      # no hardware needed
"""

import argparse
import queue
import threading
import time

import serial

from mytk import (
    App,
    BooleanIndicator,
    Box,
    Button,
    Label,
    Level,
    NumericIndicator,
)
from tkinter import DoubleVar

from hardwarelibrary.sources.millennia import (
    MillenniaDevice,
    DebugMillenniaDevice,
)

# The eV's back-panel USB port enumerates as a generic STM32 Virtual COM Port.
MILLENNIA_VID = 0x0483
MILLENNIA_PID = 0x5740


def find_millennia_ports():
    """Return the list of serial device paths that look like a Millennia eV."""
    # Imported lazily so the GUI still launches if pyserial's tooling misbehaves.
    from hardwarelibrary.communication.serialport import SerialPort

    try:
        return SerialPort.matchPorts(
            idVendor=MILLENNIA_VID, idProduct=MILLENNIA_PID
        )
    except Exception:
        return []


def probe_port(port_path):
    """Open then immediately close ``port_path`` to classify its availability.

    Returns (ok, reason). reason is one of 'busy', 'permission', 'missing', or
    a raw error string. The driver swallows the underlying serial error and
    raises a bare UnableToInitialize, so we probe here to tell the user *why*
    a connection fails -- in particular whether the port is simply in use.
    """
    try:
        port = serial.Serial(port_path, 115200, timeout=0.3)
        port.close()
        return True, None
    except serial.SerialException as err:
        text = str(err).lower()
        if "resource busy" in text or "errno 16" in text or "busy" in text:
            return False, "busy"
        if "permission" in text or "errno 13" in text or "access is denied" in text:
            return False, "permission"
        if "no such file" in text or "errno 2" in text or "could not open" in text:
            return False, "missing"
        return False, str(err)
    except Exception as err:  # pragma: no cover - defensive
        return False, str(err)


class MillenniaController:
    """Owns the device and the serial worker thread.

    The GUI never touches the device directly: it enqueues commands and reads
    state that the worker pushes back (marshalled onto the Tk main thread).
    Keeping every device call on one thread serializes a long ON confirmation
    against the periodic poll without any locking on the device itself.
    """

    POLL_INTERVAL = 1.0      # seconds between status polls while connected
    RECONNECT_INTERVAL = 2.0  # seconds between port checks while reconnecting

    def __init__(self, on_state):
        # on_state(**fields) is called from the worker thread; the App wraps it
        # so the actual widget updates happen on the main thread.
        self._on_state = on_state
        self._queue = queue.Queue()
        self._stop = threading.Event()
        self.device = None
        self._connected = False
        # Auto-reconnect intent: True while the user wants to be connected, so
        # an unexpected drop (or a laser absent at launch) is retried until the
        # port reappears. An explicit Disconnect clears it.
        self._want_connected = False
        self._last_options = None
        self._last_reconnect_attempt = 0.0
        self._thread = threading.Thread(
            target=self._run, name="millennia-worker", daemon=True
        )

    # -- public API (called from the GUI / main thread) --

    def start(self):
        self._thread.start()

    def connect(self, simulate, port):
        self._queue.put(("connect", {"simulate": simulate, "port": port}))

    def disconnect(self):
        self._queue.put(("disconnect", None))

    def set_diodes(self, turn_on):
        self._queue.put(("diodes", bool(turn_on)))

    def set_shutter(self, open_it):
        self._queue.put(("shutter", bool(open_it)))

    def shutdown(self):
        """Stop the worker and release the device (called on window close)."""
        self._stop.set()
        self._queue.put(("disconnect", None))
        self._thread.join(timeout=5.0)

    # -- worker thread --

    def _push(self, **fields):
        self._on_state(**fields)

    def _run(self):
        while not self._stop.is_set():
            try:
                command = self._queue.get(timeout=self.POLL_INTERVAL)
            except queue.Empty:
                command = None

            if command is not None:
                self._handle_command(command)

            if self._connected and self.device is not None:
                self._poll_once()
            elif self._want_connected and not self._stop.is_set():
                now = time.monotonic()
                if now - self._last_reconnect_attempt >= self.RECONNECT_INTERVAL:
                    self._last_reconnect_attempt = now
                    self._try_reconnect()

        self._safe_shutdown()

    def _handle_command(self, command):
        kind, payload = command
        if kind == "connect":
            self._do_connect(payload)
        elif kind == "disconnect":
            self._do_disconnect()
        elif kind == "diodes":
            self._do_set_diodes(payload)
        elif kind == "shutter":
            self._do_set_shutter(payload)

    def _do_connect(self, options):
        if self._connected:
            return
        # Record the intent and target so the worker can keep retrying this same
        # connection if it drops or is not yet available.
        self._want_connected = True
        self._last_options = options
        self._push(busy=True, status="Connecting…", status_kind="info")
        try:
            device = self._make_device(options)
            device.initializeDevice()
        except Exception as err:
            self.device = None
            self._connected = False
            self._last_reconnect_attempt = time.monotonic()
            # Still wanted: the worker will watch the port and retry.
            self._push(
                busy=False,
                connected=False,
                monitoring=True,
                identity="",
                diodes_on=None,
                shutter_open=None,
                power=None,
                status=self._friendly_error(err),
                status_kind="warn",
            )
            return

        self.device = device
        self._connected = True
        self._push(
            busy=False,
            connected=True,
            monitoring=False,
            identity=self._identity(device),
            status="Connected",
            status_kind="ok",
        )
        self._poll_once()

    def _make_device(self, options):
        if options["simulate"]:
            return DebugMillenniaDevice()

        port = options.get("port")
        if not port:
            ports = find_millennia_ports()
            if not ports:
                raise ConnectionError(
                    "No Millennia serial port was found. The laser may be in "
                    "use by another application (for example the Windows "
                    "'Spectra-Physics' app running under Parallels, which "
                    "captures the USB port), or the USB driver did not attach. "
                    "Close that application, check the cable, then Rescan — or "
                    "tick Simulator to try the GUI without hardware."
                )
            port = ports[0]

        ok, reason = probe_port(port)
        if not ok:
            if reason == "busy":
                raise ConnectionError(
                    "Serial port {0} is busy — another program is already "
                    "using it. Close that program (e.g. the Spectra-Physics "
                    "Windows app under Parallels) and retry.".format(port)
                )
            if reason == "permission":
                raise ConnectionError(
                    "Permission denied opening {0}.".format(port)
                )
            if reason == "missing":
                raise ConnectionError(
                    "Serial port {0} no longer exists. Rescan to refresh.".format(port)
                )
            raise ConnectionError(
                "Cannot open {0}: {1}".format(port, reason)
            )

        return MillenniaDevice(portPath=port)

    def _do_disconnect(self):
        # Explicit user disconnect: clear the intent so we do NOT auto-reconnect.
        self._want_connected = False
        self._connected = False
        self._safe_shutdown()
        self._push(
            connected=False,
            monitoring=False,
            busy=False,
            identity="",
            diodes_on=None,
            shutter_open=None,
            power=None,
            status="Disconnected",
            status_kind="info",
        )

    def _try_reconnect(self):
        """Attempt a reconnect, but only when the port actually looks available.

        Probing first avoids spamming 'Connecting…/failed' every couple of
        seconds while the laser is simply gone.
        """
        options = self._last_options
        if options is None or self._connected:
            return
        if not options.get("simulate"):
            port = options.get("port")
            if port:
                ok, _ = probe_port(port)
                if not ok:
                    return  # still missing or busy
            elif not find_millennia_ports():
                return      # not back yet
        self._do_connect(options)

    def _safe_shutdown(self):
        if self.device is not None:
            try:
                self.device.shutdownDevice()
            except Exception:
                pass
            self.device = None

    def _do_set_diodes(self, turn_on):
        if not self._connected or self.device is None:
            return
        action = "on" if turn_on else "off"
        self._push(busy=True, status="Turning diodes {0}…".format(action),
                   status_kind="info")
        try:
            if turn_on:
                self.device.turnOn()
            else:
                self.device.turnOff()
        except Exception as err:
            self._push(busy=False,
                       status="Could not turn diodes {0}: {1}".format(action, err),
                       status_kind="error")
            return
        self._push(busy=False, status="Connected", status_kind="ok")
        self._poll_once()

    def _do_set_shutter(self, open_it):
        if not self._connected or self.device is None:
            return
        action = "Opening" if open_it else "Closing"
        self._push(busy=True, status="{0} shutter…".format(action),
                   status_kind="info")
        try:
            if open_it:
                self.device.openShutter()
            else:
                self.device.closeShutter()
        except Exception as err:
            self._push(busy=False,
                       status="Could not move shutter: {0}".format(err),
                       status_kind="error")
            return
        self._push(busy=False, status="Connected", status_kind="ok")
        self._poll_once()

    def _poll_once(self):
        try:
            diodes_on = self.device.isLaserOn()
            shutter_open = self.device.isShutterOpen()
            power = self.device.power()
        except Exception as err:
            self._connected = False
            self._safe_shutdown()
            self._last_reconnect_attempt = time.monotonic()
            # Intent (_want_connected) is left set, so the worker now watches
            # the port and reconnects automatically when it reappears.
            self._push(
                connected=False,
                monitoring=True,
                diodes_on=None,
                shutter_open=None,
                power=None,
                status="Connection lost ({0}). Monitoring port — will "
                       "reconnect automatically.".format(err),
                status_kind="warn",
            )
            return
        self._push(diodes_on=diodes_on, shutter_open=shutter_open, power=power)

    @staticmethod
    def _identity(device):
        parts = [
            getattr(device, "manufacturer", None),
            getattr(device, "model", None),
        ]
        label = " ".join(p for p in parts if p) or "Millennia"
        serial_number = getattr(device, "laserSerialNumber", None)
        firmware = getattr(device, "firmwareVersion", None)
        if serial_number:
            label += "   S/N {0}".format(serial_number)
        if firmware:
            label += "   fw {0}".format(firmware)
        return label

    @staticmethod
    def _friendly_error(err):
        # ConnectionError messages we build are already user-facing.
        if isinstance(err, ConnectionError):
            return str(err)
        return "Could not connect: {0}".format(err)


class MillenniaApp:
    """Builds the window and wires the widgets to the controller."""

    MAX_POWER = 30.0  # full-scale of the power Level bar (W)

    STATUS_COLORS = {
        "ok": "#1a7f37",
        "info": "#57606a",
        "warn": "#9a6700",
        "error": "#cf222e",
    }

    def __init__(self, simulate=False, port=None):
        self.simulate_arg = simulate
        self.port_arg = port

        # Last known truth, so a button click can send the *opposite* action.
        self.connected = False
        self.monitoring = False  # disconnected but auto-reconnecting
        self.busy = False
        self.diodes_on = None
        self.shutter_open = None

        self.app = App(geometry="640x460", name="Millennia eV Control")
        self.app.window.widget.title("Millennia eV — Laser Control")

        self.controller = MillenniaController(on_state=self._on_state_from_worker)

        self._build_ui()

        self.app.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.controller.start()

        # Auto-connect on launch using the CLI arguments.
        self._update_controls_enabled()
        self.controller.connect(self.simulate_arg, self.port_arg)

    # -- UI construction --

    def _build_ui(self):
        window = self.app.window

        # --- Diodes (On/Off) -------------------------------------------
        diode_box = Box(label="Pump Diodes")
        diode_box.grid_into(window, column=0, row=0, padx=8, pady=6, sticky="nsew")

        self.diode_lamp = BooleanIndicator(diameter=18)
        self.diode_lamp.grid_into(diode_box, column=0, row=0, padx=8, pady=8)
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

        self.shutter_lamp = BooleanIndicator(diameter=18)
        self.shutter_lamp.grid_into(shutter_box, column=0, row=0, padx=8, pady=8)
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

        self.power_var = DoubleVar(value=0.0)
        self.power_indicator = NumericIndicator(
            value_variable=self.power_var, format_string="{0:.2f} W"
        )
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

        self.rescan_button = Button(
            "Rescan ports", user_event_callback=self._on_rescan_clicked
        )
        self.rescan_button.grid_into(conn_box, column=1, row=2,
                                     padx=4, pady=6, sticky="w")

        window.all_resize_weight(1)

    # -- worker -> main thread bridge --

    def _on_state_from_worker(self, **fields):
        # Called on the worker thread; bounce onto the Tk main thread.
        self.app.schedule_on_main_thread(self._apply_state, kwargs=fields)

    def _apply_state(self, **fields):
        if "status" in fields:
            self.status_label.value_variable.set(fields["status"])
            kind = fields.get("status_kind", "info")
            color = self.STATUS_COLORS.get(kind, "#000000")
            try:
                self.status_label.widget.configure(foreground=color)
            except Exception:
                pass

        if "identity" in fields:
            self.identity_label.value_variable.set(fields["identity"] or "")

        if "connected" in fields:
            self.connected = bool(fields["connected"])

        if "monitoring" in fields:
            self.monitoring = bool(fields["monitoring"])

        if "busy" in fields:
            self.busy = bool(fields["busy"])

        if "diodes_on" in fields:
            self.diodes_on = fields["diodes_on"]
            self._apply_lamp(self.diode_lamp, self.diode_state_label,
                             self.diodes_on, "ON", "OFF")

        if "shutter_open" in fields:
            self.shutter_open = fields["shutter_open"]
            self._apply_lamp(self.shutter_lamp, self.shutter_state_label,
                             self.shutter_open, "OPEN", "CLOSED")

        if "power" in fields:
            power = fields["power"]
            value = power if power is not None else 0.0
            self.power_var.set(value)          # numeric readout
            self.power_level.value_variable.set(value)  # bar indicator

        self._update_controls_enabled()

    @staticmethod
    def _apply_lamp(lamp, label, value, true_text, false_text):
        lamp.value_variable.set(bool(value) if value is not None else False)
        label.value_variable.set(
            "—" if value is None else (true_text if value else false_text)
        )

    # -- control enable/disable + button labels --

    def _update_controls_enabled(self):
        connected = self.connected
        idle = connected and not self.busy

        # While monitoring (disconnected but auto-reconnecting), the button
        # reads "Disconnect" so a click cancels the auto-retry.
        self.connect_button.label = (
            "Disconnect" if (connected or self.monitoring) else "Connect"
        )
        # Rescan only makes sense when idle and not actively connected.
        self.rescan_button.is_disabled = connected or self.busy

        self.diode_button.is_disabled = not idle or self.diodes_on is None
        self.shutter_button.is_disabled = not idle or self.shutter_open is None

        self.diode_button.label = (
            "Turn Off" if self.diodes_on else "Turn On"
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
            self.controller.connect(self.simulate_arg, self.port_arg)

    def _on_rescan_clicked(self, event, button):
        ports = find_millennia_ports()
        if ports:
            self.port_arg = ports[0]
            self.status_label.value_variable.set(
                "Found Millennia at {0}. Press Connect.".format(ports[0])
            )
            self.status_label.widget.configure(
                foreground=self.STATUS_COLORS["ok"]
            )
        else:
            self.port_arg = None
            self.status_label.value_variable.set(
                "No Millennia serial port found (it may be captured by another "
                "app, e.g. Spectra-Physics under Parallels)."
            )
            self.status_label.widget.configure(
                foreground=self.STATUS_COLORS["warn"]
            )

    def _on_diode_clicked(self, event, button):
        if self.diodes_on is None:
            return
        self.controller.set_diodes(not self.diodes_on)

    def _on_shutter_clicked(self, event, button):
        if self.shutter_open is None:
            return
        self.controller.set_shutter(not self.shutter_open)

    def _on_close(self):
        try:
            self.controller.shutdown()
        finally:
            self.app.quit()

    def run(self):
        self.app.mainloop()


def main():
    parser = argparse.ArgumentParser(description="Millennia eV laser control GUI")
    parser.add_argument(
        "--simulate", action="store_true",
        help="Use the built-in DebugMillenniaDevice (no hardware needed).",
    )
    parser.add_argument(
        "--port", default=None,
        help="Serial port path (e.g. /dev/cu.usbmodemXXXX). Auto-detected if omitted.",
    )
    # parse_known_args so a stray argument from a Finder/.app launch (e.g. an
    # old-style -psn_ process serial number) never aborts startup.
    args, _ = parser.parse_known_args()

    try:
        MillenniaApp(simulate=args.simulate, port=args.port).run()
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
    main()
