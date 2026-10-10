# Evo IDE Windows / macOS PyInstaller build
# Build: pyinstaller --clean --noconfirm evo_ide.spec
# Output: dist/EvoIDE/EvoIDE[.exe]

from PyInstaller.utils.hooks import collect_submodules
import importlib.util

hidden = []

# Keep optional modules available when installed on the build machine.
for optional in ("curl_cffi", "certifi"):
    if importlib.util.find_spec(optional):
        try:
            hidden += collect_submodules(optional)
        except Exception:
            hidden.append(optional)

block_cipher = None

a = Analysis(
    ["server.py"],
    pathex=["."],
    binaries=[],
    datas=[
        ("static", "static"),
        ("config.json", "."),
        ("evo_config.json", "."),
    ],
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "numpy",
        "pandas",
        "scipy",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="EvoIDE",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
)
