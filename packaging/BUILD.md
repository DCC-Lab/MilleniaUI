# Building a standalone MilleniaUI app

The `millennia_ui.py` GUI can be bundled into a double-clickable macOS
application so it runs without a separate Python install — and, because a
`.app` launched from Finder runs in the user's own GUI session, it also avoids
the "wrong login session owns the screen" problem you hit running the script
directly. Bundling is done with [PyInstaller](https://pyinstaller.org/).

## One-time setup

From the repo root, in the project's virtual environment (Python 3.13 — see the
top-level README for why 3.13 and not 3.14):

```bash
source .venv/bin/activate
pip install -r requirements.txt
pip install git+https://github.com/DCC-Lab/PyHardwareLibrary.git
pip install pyinstaller
```

PyInstaller freezes whatever is importable in this environment, so the runtime
deps (`mytk`, `pyserial`, `hardwarelibrary`, and its `pyusb`/`pyftdi`/
`matplotlib`/`numpy`/`Pillow` dependencies) must already be installed here.

## Build

```bash
./packaging/build_app.sh
```

Result: **`packaging/dist/MilleniaUI.app`** — a standard `.app` bundle.
Double-click to launch; drag into `/Applications` to install.

Typical build time: ~1 minute. Typical bundle size: ~170 MB (numpy and
matplotlib dominate; pandas/scipy are excluded since this app never imports
them).

## Flags explained

The full command lives in `packaging/build_app.sh`. The non-obvious parts:

- `--windowed` — produces a GUI `.app` with no terminal console window.
- `--distpath`/`--workpath`/`--specpath packaging` — keeps `dist/`, `build/`,
  and the generated `MilleniaUI.spec` inside `packaging/` instead of scattering
  them at the repo root.
- `--collect-submodules PIL` — `mytk` loads `PIL.ImageDraw` and `PIL.ImageTk`
  via `importlib.import_module()`, which PyInstaller's static analyzer cannot
  follow. Without it the bundled app prompts "install the missing module
  'ImageDraw'" on first launch.
- `--collect-all hardwarelibrary` (and `serial`, `usb`, `pyftdi`) — the device
  drivers and their serial/USB backends are imported indirectly; collecting the
  whole packages guarantees the Millennia driver and pyserial transport are
  present.
- `--exclude-module pandas/scipy/openpyxl/...` — these are declared by
  `hardwarelibrary` but unused on this app's import path
  (`hardwarelibrary.sources.millennia`); excluding them trims ~100 MB.

## App icon

The icon (a 532 nm green laser beam on a dark squircle) is generated from code,
not a binary asset, so it is easy to tweak:

```bash
python packaging/make_icon.py     # -> packaging/MilleniaUI.icns
```

`build_app.sh` passes it via `--icon packaging/MilleniaUI.icns` (as an absolute
path — PyInstaller resolves `--icon` relative to `--specpath`, so a bare
`packaging/…` would wrongly become `packaging/packaging/…`). After rebuilding,
if Finder/Dock still show the old icon, it is just the icon cache:
`touch packaging/dist/MilleniaUI.app` or `killall Dock Finder`.

## First launch / Gatekeeper

The `.app` is only ad-hoc signed. A copy you built yourself runs by
double-click. A copy downloaded or AirDropped to another Mac is quarantined —
the user right-clicks the app and chooses **Open** once to approve it. For wider
distribution, sign and notarize with a Developer ID:

```bash
codesign --deep --force --sign "Developer ID Application: NAME (TEAMID)" \
    packaging/dist/MilleniaUI.app
xcrun notarytool submit packaging/dist/MilleniaUI.app \
    --apple-id you@example.com --team-id TEAMID --wait
xcrun stapler staple packaging/dist/MilleniaUI.app
```

## Architecture note

PyInstaller does **not** cross-compile and freezes for the architecture it runs
on. This bundle was built on an Intel Mac, so it is **x86_64** (it also runs on
Apple Silicon via Rosetta 2). To ship a native arm64 build, run the same script
on an Apple Silicon Mac.

## Automated builds for all three platforms

`.github/workflows/build-app.yml` builds macOS, Windows, and Linux bundles on
GitHub Actions (PyInstaller can't cross-compile, so each runs on its own
runner). Two triggers:

- **Publish a GitHub Release** — builds all three and attaches the archives to
  that release.
- **Actions → "Build standalone app" → Run workflow** — manual run; download the
  archives from the run's Artifacts section.

Archives produced:

- `MilleniaUI-macOS.zip` — contains `MilleniaUI.app`. Built on `macos-13`
  (**Intel/x86_64**, matching the lab bench); for an Apple Silicon build add a
  `macos-14` matrix entry.
- `MilleniaUI-Windows.zip` — a `MilleniaUI/` folder with `MilleniaUI.exe` and
  its DLLs.
- `MilleniaUI-Linux.tar.gz` — a `MilleniaUI/` folder with the executable, built
  against Ubuntu 22.04's glibc.

The workflow installs Tk on Linux (`python3-tk`), uses the per-platform icon
(`.icns` on macOS, `.ico` on Windows; Linux embeds none), and does no code
signing — so the macOS build still triggers Gatekeeper (see above). The Windows
and Linux icons come from `packaging/MilleniaUI.ico`, regenerated alongside the
`.icns` by `python packaging/make_icon.py`.

## Connecting to the real laser

The bundled app behaves exactly like the script: it auto-detects the Millennia
serial port. If the Windows **Spectra-Physics** app (running under Parallels)
still holds the laser's USB port, macOS exposes no serial device and the app's
status line will say the port is unavailable — quit that app first. To exercise
the GUI without hardware, launch from a terminal with `--simulate`:

```bash
packaging/dist/MilleniaUI.app/Contents/MacOS/MilleniaUI --simulate
```
