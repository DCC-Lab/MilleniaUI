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

# Derive the version from the git tag and write _version.py (+ the Windows
# resource, unused here). Prints the resolved version.
VERSION=$(python packaging/make_version.py)
echo "Building MilleniaUI $VERSION"

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

# Stamp the bundle's Info.plist with the version.
PLIST="packaging/dist/MilleniaUI.app/Contents/Info.plist"
NUM=$(echo "$VERSION" | grep -oE '^[0-9]+(\.[0-9]+){0,2}'); NUM=${NUM:-0.0.0}
plutil -replace CFBundleShortVersionString -string "$NUM" "$PLIST"
plutil -replace CFBundleVersion -string "$VERSION" "$PLIST"

echo
echo "Built: packaging/dist/MilleniaUI.app  (version $VERSION)"
