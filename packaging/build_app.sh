#!/bin/bash
# Build the standalone MilleniaUI.app with PyInstaller.
# Run from the repo root inside the project venv:
#
#     source .venv/bin/activate
#     ./packaging/build_app.sh
#
# Result: packaging/dist/MilleniaUI.app  (double-click to launch,
# drag into /Applications to install). Build artifacts stay under packaging/.
set -e
cd "$(dirname "$0")/.."

# Absolute path: PyInstaller resolves --icon relative to --specpath, so a
# "packaging/…" relative path would wrongly become "packaging/packaging/…".
ICON="$PWD/packaging/MilleniaUI.icns"

pyinstaller --windowed --name MilleniaUI --noconfirm \
  --osx-bundle-identifier ca.ulaval.cervo.milleniaui \
  --icon "$ICON" \
  --distpath packaging/dist --workpath packaging/build --specpath packaging \
  --collect-submodules PIL \
  --collect-all mytk \
  --collect-all hardwarelibrary \
  --collect-all matplotlib \
  --collect-all numpy \
  --collect-all PIL \
  --collect-all serial \
  --collect-all usb \
  --collect-all pyftdi \
  --collect-all dateutil \
  --collect-all fontTools \
  --collect-all kiwisolver \
  --collect-all cycler \
  --collect-all pyparsing \
  --exclude-module pandas --exclude-module scipy --exclude-module openpyxl \
  --exclude-module et_xmlfile \
  --exclude-module PyQt5 --exclude-module PyQt6 \
  --exclude-module PySide2 --exclude-module PySide6 --exclude-module wx \
  millennia_ui.py

echo
echo "Built: packaging/dist/MilleniaUI.app"
