"""Small desktop-alert helpers for human-assist checkpoints."""

from __future__ import annotations

import os
import subprocess


def _truthy_env(name: str) -> bool:
    value = os.environ.get(name, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _ps_single_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def show_desktop_alert(title: str, message: str) -> bool:
    """Show a best-effort non-blocking desktop alert.

    The caller must not rely on this for control flow. It is purely a user
    attention signal for visible-browser checkpoints such as Cloudflare or SSO.
    """
    if _truthy_env("INSTSCI_DISABLE_DESKTOP_ALERTS"):
        return False
    if _truthy_env("CI") or os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    if os.name != "nt":
        return False

    script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "[System.Windows.Forms.MessageBox]::Show("
        f"{_ps_single_quote(message)},"
        f"{_ps_single_quote(title)},"
        "[System.Windows.Forms.MessageBoxButtons]::OK,"
        "[System.Windows.Forms.MessageBoxIcon]::Information"
        ") | Out-Null"
    )
    try:
        subprocess.Popen(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-WindowStyle",
                "Hidden",
                "-Command",
                script,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            close_fds=True,
        )
    except Exception:
        return False
    return True
