#!/bin/bash
# Launch the Millennia eV control GUI using this project's Python 3.13 venv.
# Run this from a Terminal that belongs to the GUI login session (the user
# logged in at the screen), otherwise macOS cannot open a window and Tk aborts.
#
#   ./run.sh              # connect to the real laser (auto-detect the port)
#   ./run.sh --simulate   # no hardware needed
#   ./run.sh --port /dev/cu.usbmodemXXXX
#
# For the real laser, first QUIT the Windows "Spectra-Physics" app in Parallels
# so macOS releases the USB serial port.
HERE="$(cd "$(dirname "$0")" && pwd)"
# Best-effort: stamp _version.py from the git tag so the window title shows it.
"$HERE/.venv/bin/python" "$HERE/packaging/make_version.py" >/dev/null 2>&1 || true
exec "$HERE/.venv/bin/python" "$HERE/millennia_ui.py" "$@"
