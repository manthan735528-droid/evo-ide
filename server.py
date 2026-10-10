#!/usr/bin/env python3
"""
Evo IDE V5 - Main Entry Point
=============================
Run with:  python3 server.py
Then open: http://localhost:8000

IMPORTANT: Use ABSOLUTE imports, not relative imports!
This fixes: ImportError: attempted relative import with no known parent package
"""
from core.ssl_fix import apply_ssl_fix  # Auto-fix SSL certificates on startup
import socketserver
import http.server
import signal
import webbrowser
import threading
import socket
import sys
from pathlib import Path

from core.config import PORT, SANDBOX_DIR, VENV_DIR, bootstrap_sandbox, setup_logging, logger
from core.indexing import vector_index
from core.server_handler import IDEHandler


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def signal_handler(sig, frame):
    print("\nReceived shutdown signal. Cleaning up...")
    sys.exit(0)


def get_lan_ip() -> str:
    """Best-effort LAN IP so other devices on the same Wi-Fi can reach this server."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Doesn't actually send anything; just used to pick the outbound interface.
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def main():
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    setup_logging()
    bootstrap_sandbox()

    logger.info("Building code index...")
    vector_index.rebuild()

    with ThreadedHTTPServer(("", PORT), IDEHandler) as httpd:
        lan_ip = get_lan_ip()
        print("=" * 70)
        print(f"  EVO IDE V5  |  http://localhost:{PORT}")
        if lan_ip:
            print(f"  Same Wi-Fi:  http://{lan_ip}:{PORT}   <- open this on your phone/other laptop")
        else:
            print("  Same Wi-Fi:  couldn't detect a LAN IP (not connected to a network?)")
        print("=" * 70)
        print(f"  Sandbox:     {SANDBOX_DIR}")
        print(f"  Virtual Env: {VENV_DIR}")
        print()
        if lan_ip:
            print("  First connection from another device may prompt for a macOS Firewall")
            print("  'Allow incoming connections' permission for python3 — click Allow.")
            print()
        print("  Features:")
        print("    - Structural code parsing (AST-based)")
        print("    - Lightweight vector indexing with proximity scoring")
        print("    - Import validation (skips system modules)")
        print("    - Smart selective fixes (not full rewrites)")
        print("    - Sandboxed virtualenv with automatic pip-install")
        print("    - File/folder delete, ZIP project import")
        print("    - Inline error diagnostics + complexity metrics")
        print("    - Terminal with command execution")
        print("    - Project templates (6 built-in)")
        print("    - Web fetch for live data integration")
        print("    - Canvas panel: visual project structure diagram")
        print("    - Dependency graph visualization")
        print("    - Self-healing with original file tracking")
        print("=" * 70)

        # Auto-open browser after a short delay
        def open_browser():
            import time
            time.sleep(1.5)
            webbrowser.open(f"http://localhost:{PORT}/")

        threading.Thread(target=open_browser, daemon=True).start()

        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n  Shutting down gracefully.")
            httpd.server_close()


if __name__ == "__main__":
    main()
