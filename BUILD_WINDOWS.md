# Evo IDE Windows EXE build

1. Install Python 3 on the Windows build machine and make sure `py` or `python` works in CMD.
2. Put this project folder on Windows.
3. Double-click `build_windows.bat`.
4. The executable is created at `dist\EvoIDE\EvoIDE.exe`.

The PyInstaller build embeds the `static/` directory, so `index.html`, `app.js`, `styles.css`, and `ai_model_manager.html` remain connected to the Python HTTP server.

The writable `sandbox/` is intentionally kept outside the PyInstaller bundle. This prevents one-file extraction cleanup from deleting user projects, logs, memory, indexes, and transactions.

The browser talks to the same local HTTP server, so the HTML does not need a separate web server.

## Runtime requirements

- Windows 10/11
- Python 3 on PATH if the IDE needs to execute user Python files or create the optional sandbox venv
- Ollama installed/running for local Ollama models
- Internet only for features that explicitly use web/cloud services

## Terminal commands

`/api/terminal` continues to use `subprocess.run(..., shell=True)` with the Evo sandbox as the working directory. On Windows, this uses the normal CMD shell, so normal CMD commands work from the IDE terminal.
