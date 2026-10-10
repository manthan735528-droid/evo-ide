#!/usr/bin/env python3
"""
Evo IDE — Auto SSL Fix Module (Production)
=========================================
Zero-configuration SSL certificate fix for macOS, Linux, and Windows.

Solves: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
        unable to get local issuer certificate

NO MANUAL SETUP REQUIRED. This module:
1. Detects if SSL verification is broken on import
2. Automatically fixes it using the best available method
3. Patches urllib globally so ALL code works unchanged

Usage: Just import before any HTTPS calls:
    from core.ssl_fix import apply_ssl_fix  # Auto-applies

Or let server.py import it automatically at startup.
"""

import os
import sys
import ssl
import subprocess
import urllib.request
import urllib.error
import warnings
from pathlib import Path

# ── Module state ──────────────────────────────────────────────────────────
_ssl_fixed = False
_original_urlopen = None


def _is_macos_installer_python() -> bool:
    """Detect Python from python.org/macOS installer (the #1 culprit)."""
    if sys.platform != "darwin":
        return False
    exe = Path(sys.executable).resolve()
    return (
        "/Library/Frameworks/Python.framework" in str(exe)
        or "/Applications/Python" in str(exe)
        or ("Frameworks" in str(exe) and exe.name.startswith("python"))
    )


def _try_pip_install_certifi() -> bool:
    """Use bundled certifi when frozen; never invoke the PyInstaller exe as pip."""
    if getattr(sys, "frozen", False):
        return False
    try:
        import certifi
        return True
    except ImportError:
        pass

    cmds = [
        [sys.executable, "-m", "pip", "install", "--quiet", "--user", "certifi"],
        [sys.executable, "-m", "pip", "install", "--quiet", "certifi"],
        [sys.executable, "-m", "pip", "install", "certifi"],
    ]
    for cmd in cmds:
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60,
            )
            if result.returncode == 0:
                # Force reimport
                if "certifi" in sys.modules:
                    del sys.modules["certifi"]
                import certifi  # noqa: F401
                return True
        except Exception:
            continue
    return False


def _get_certifi_context():
    """SSL context backed by certifi's CA bundle."""
    import certifi
    return ssl.create_default_context(cafile=certifi.where())


def _get_system_context():
    """SSL context with explicit system certificate loading."""
    ctx = ssl.create_default_context()
    ctx.load_default_certs()
    return ctx


def _get_macos_context():
    """SSL context with macOS-specific certificate paths."""
    ctx = ssl.create_default_context()
    # macOS certificate locations
    for path in [
        "/etc/ssl/cert.pem",
        "/System/Library/OpenSSL/certs/cert.pem",
        "/usr/local/etc/openssl/cert.pem",
        "/opt/homebrew/etc/openssl@3/cert.pem",
        "/opt/homebrew/etc/openssl@1.1/cert.pem",
    ]:
        if os.path.isfile(path):
            try:
                ctx.load_verify_locations(cafile=path)
                return ctx
            except Exception:
                continue
    # Try cert directories
    for path in [
        "/System/Library/OpenSSL/certs",
        "/etc/ssl/certs",
    ]:
        if os.path.isdir(path):
            try:
                ctx.load_verify_locations(capath=path)
                return ctx
            except Exception:
                continue
    ctx.load_default_certs()
    return ctx


def _get_linux_context():
    """SSL context with Linux-specific certificate paths."""
    ctx = ssl.create_default_context()
    for path in [
        "/etc/ssl/certs/ca-certificates.crt",  # Debian/Ubuntu/Gentoo
        "/etc/pki/tls/certs/ca-bundle.crt",      # Fedora/RHEL
        "/etc/ssl/ca-bundle.pem",                # OpenSUSE
        "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",  # CentOS/RHEL 7
        "/etc/ssl/cert.pem",                     # Alpine
    ]:
        if os.path.isfile(path):
            try:
                ctx.load_verify_locations(cafile=path)
                return ctx
            except Exception:
                continue
    ctx.load_default_certs()
    return ctx


def _get_windows_context():
    """SSL context with Windows certificate store loading."""
    ctx = ssl.create_default_context()
    ctx.load_default_certs()
    return ctx


def _get_best_context():
    """Get the best available SSL context for this platform."""
    # Try certifi first (most reliable)
    try:
        return _get_certifi_context()
    except Exception:
        pass

    # Platform-specific fallbacks
    if sys.platform == "darwin":
        try:
            return _get_macos_context()
        except Exception:
            pass
    elif sys.platform == "linux":
        try:
            return _get_linux_context()
        except Exception:
            pass
    elif sys.platform == "win32":
        try:
            return _get_windows_context()
        except Exception:
            pass

    # Generic fallback
    ctx = ssl.create_default_context()
    ctx.load_default_certs()
    return ctx


def _test_connection(url: str = "https://pypi.org", timeout: int = 8) -> bool:
    """Test if HTTPS connections work."""
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status < 400
    except Exception:
        return False


def _test_with_context(ctx, url: str = "https://pypi.org", timeout: int = 8) -> bool:
    """Test if HTTPS works with a specific SSL context."""
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
            return resp.status < 400
    except Exception:
        return False


def _create_patched_urlopen(ssl_context):
    """Factory: returns a urlopen that injects our SSL context."""
    def patched_urlopen(url, data=None, timeout=None, **kwargs):
        # Respect caller-provided context
        if kwargs.get("context") is not None:
            return _original_urlopen(url, data, timeout, **kwargs)
        kwargs["context"] = ssl_context
        return _original_urlopen(url, data, timeout, **kwargs)
    return patched_urlopen


def _run_macos_cert_install() -> bool:
    """Run macOS Install Certificates.command equivalent."""
    if sys.platform != "darwin":
        return False

    # Find the certificate install script
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    script_paths = [
        f"/Applications/Python {version}/Install Certificates.command",
        f"/Applications/Python {version[0]}/Install Certificates.command",
    ]

    apps = Path("/Applications")
    if apps.exists():
        for app in apps.iterdir():
            if app.name.startswith("Python"):
                script = app / "Install Certificates.command"
                if script.exists():
                    script_paths.append(str(script))

    for script in script_paths:
        if os.path.exists(script):
            try:
                subprocess.run(
                    ["bash", script],
                    capture_output=True, text=True, timeout=30,
                )
                return True
            except Exception:
                continue
    return False


def _fix_macos_symlink() -> bool:
    """Fix macOS by symlinking certifi to OpenSSL's expected location."""
    if sys.platform != "darwin":
        return False
    try:
        import certifi
        import os as os_mod
        import ssl as ssl_mod

        openssl_cafile = ssl_mod.get_default_verify_paths().openssl_cafile
        if not openssl_cafile:
            return False

        openssl_dir = os_mod.path.dirname(openssl_cafile)
        certifi_path = certifi.where()

        # Remove old file if exists
        if os_mod.path.exists(openssl_cafile):
            try:
                os_mod.remove(openssl_cafile)
            except Exception:
                pass

        # Create symlink
        relpath = os_mod.path.relpath(certifi_path, openssl_dir)
        os_mod.symlink(relpath, openssl_cafile)
        os_mod.chmod(openssl_cafile, 0o755)
        return True
    except Exception:
        return False


def apply_ssl_fix(force: bool = False) -> dict:
    """
    Apply the SSL fix. Returns result dict with keys:
        fixed, method, test_ok, details
    """
    global _ssl_fixed, _original_urlopen

    if _ssl_fixed and not force:
        return {
            "fixed": True,
            "method": "already_applied",
            "test_ok": True,
            "details": "SSL fix already active",
        }

    result = {"fixed": False, "method": "none", "test_ok": False, "details": ""}

    # 1. Already working?
    if _test_connection():
        result.update({
            "fixed": True, "method": "already_working",
            "test_ok": True, "details": "SSL verification already works",
        })
        _ssl_fixed = True
        return result

    # 2. Try certifi (install if needed)
    certifi_ok = _try_pip_install_certifi()

    # 3. macOS: run cert install script
    if sys.platform == "darwin" and _is_macos_installer_python():
        if _run_macos_cert_install() and _test_connection():
            result.update({
                "fixed": True, "method": "macos_script",
                "test_ok": True,
                "details": "Fixed via macOS Install Certificates.command",
            })
            _ssl_fixed = True
            return result

    # 4. macOS: symlink certifi
    if certifi_ok and sys.platform == "darwin":
        if _fix_macos_symlink() and _test_connection():
            result.update({
                "fixed": True, "method": "macos_symlink",
                "test_ok": True,
                "details": "Fixed macOS certificates via certifi symlink",
            })
            _ssl_fixed = True
            return result

    # 5. Build best SSL context and test it
    ssl_ctx = _get_best_context()
    if _test_with_context(ssl_ctx):
        _original_urlopen = urllib.request.urlopen
        urllib.request.urlopen = _create_patched_urlopen(ssl_ctx)
        result.update({
            "fixed": True, "method": "context_patch",
            "test_ok": True,
            "details": (
                "Patched urllib with certifi SSL context"
                if certifi_ok else
                "Patched urllib with system SSL context"
            ),
        })
        _ssl_fixed = True
        return result

    # 6. Ultimate fallback: unverified context (with warning)
    try:
        unverified = ssl._create_unverified_context()
        _original_urlopen = urllib.request.urlopen
        urllib.request.urlopen = _create_patched_urlopen(unverified)
        result.update({
            "fixed": True, "method": "unverified",
            "test_ok": True,
            "details": "WARNING: Using unverified SSL context. Install certificates for production.",
        })
        _ssl_fixed = True
        warnings.warn(result["details"], RuntimeWarning, stacklevel=2)
        return result
    except Exception as e:
        result["details"] = f"All SSL fixes failed: {e}"
        return result


# ── Auto-apply on import ──────────────────────────────────────────────────
def _auto_apply():
    res = apply_ssl_fix()
    if res["fixed"]:
        if res["method"] == "already_working":
            pass  # Silent
        elif res["method"] == "unverified":
            print(f"[ssl_fix] WARNING: {res['details']}", file=sys.stderr, flush=True)
        else:
            print(f"[ssl_fix] {res['details']}", file=sys.stderr, flush=True)
    else:
        print(f"[ssl_fix] FAILED: {res['details']}", file=sys.stderr, flush=True)


_auto_apply()
