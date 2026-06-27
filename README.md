# Millennia eV Laser Control

A small [myTk](https://pypi.org/project/mytk/) GUI for the Spectra-Physics
**Millennia eV** CW pump laser, driven through
[PyHardwareLibrary](https://github.com/DCC-Lab/PyHardwareLibrary)'s
`MillenniaDevice`.

The window provides:

- **Pump Diodes On/Off** — a toggle button (driver `ON` / `OFF`, status `?D`).
- **Shutter Open/Close** — a toggle button (`SHT:1` / `SHT:0`, status `?SHT`).
- **Power Monitor** — live output power in watts (`?P`) plus On/Off and
  Open/Closed lamps for the diodes and the shutter.

All serial I/O runs on a background worker thread, so the eV's slow
write-then-confirm actions never freeze the UI. If the laser's serial port is
already in use by another program (on this bench the eV's USB port is routinely
captured by Parallels for the Windows *Spectra-Physics* app), the status line
says so explicitly.

**Auto-reconnect.** If the connection drops (cable pulled, port disappears) — or
the laser simply isn't present at launch — the app keeps watching for the port
and reconnects automatically as soon as it reappears. During this the status
line shows it is monitoring and the button reads **Disconnect** so you can cancel
the auto-retry. An explicit Disconnect is honoured: it will not reconnect on its
own until you press Connect again.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install git+https://github.com/DCC-Lab/PyHardwareLibrary.git
```

## Run

```bash
python millennia_ui.py                 # auto-detect the Millennia serial port
python millennia_ui.py --port /dev/cu.usbmodemXXXX
python millennia_ui.py --simulate      # no hardware needed (DebugMillenniaDevice)
```

## Standalone macOS app

A double-clickable bundle can be built with PyInstaller (no Python install
needed by the end user, and it launches in the user's own GUI session):

```bash
./packaging/build_app.sh        # -> packaging/dist/MilleniaUI.app
```

See [packaging/BUILD.md](packaging/BUILD.md) for details, Gatekeeper notes, and
code-signing.

## Requirements / known issues

- **Tk runtime.** myTk is a Tkinter front-end, so a working Tcl/Tk is required.
  The python.org **3.14** build ships **Tcl/Tk 9.0**, which crashes (SIGILL) on
  some Intel Macs / macOS 13. If `python -c "import tkinter; tkinter.Tk()"`
  crashes, use a Python whose Tk works — the python.org **3.13** installer
  ships the stable **Tk 8.6** and is the recommended runtime on this machine.
- **Port busy.** If the Spectra-Physics Windows app (or any program) holds the
  laser's USB/serial port, macOS will not expose a `/dev/cu.*` node for it and
  the GUI cannot connect. Quit that app first.

## License

[MIT](LICENSE) © 2026 Daniel Côté (DCC-Lab)
