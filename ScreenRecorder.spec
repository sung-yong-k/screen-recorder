# PyInstaller spec: builds a single ScreenRecorder.exe with ffmpeg inside.
# Build with:  pyinstaller --noconfirm ScreenRecorder.spec

import os

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

binaries = []
datas = []

# ffmpeg.exe (from imageio-ffmpeg) goes next to the app inside the bundle.
try:
    from imageio_ffmpeg import get_ffmpeg_exe

    binaries.append((get_ffmpeg_exe(), "."))
except Exception as exc:  # pragma: no cover
    raise SystemExit("imageio-ffmpeg is required to bundle ffmpeg: %s" % exc)

# portaudio DLL used by sounddevice
binaries += collect_dynamic_libs("sounddevice")
for package in ("sounddevice", "_sounddevice_data"):
    try:
        datas += collect_data_files(package)
    except Exception:
        pass

project_dir = os.path.abspath(SPECPATH)  # noqa: F821 - injected by PyInstaller

a = Analysis(
    [os.path.join(project_dir, "run.py")],
    pathex=[project_dir],
    binaries=binaries,
    datas=datas,
    hiddenimports=[
        "mss", "mss.windows", "numpy",
        "sounddevice", "soundcard", "soundcard.mediafoundation", "cffi", "_cffi_backend",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["matplotlib", "PIL", "scipy", "pandas", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ScreenRecorder",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,      # no console window
    icon=None,
)
