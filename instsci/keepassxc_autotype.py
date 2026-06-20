"""KeePassXC Auto-Type assist helpers.

This module must never read, return, log, or store credential values. It only
models safe metadata and, when explicitly requested by the user, sends the
configured KeePassXC global Auto-Type hotkey to the focused window.
"""

from __future__ import annotations

import ctypes
import re
import sys
import time
from urllib.parse import urlparse


DEFAULT_KEEPASSXC_AUTOTYPE_HOTKEY = "ctrl+alt+a"

_MODIFIER_ALIASES = {
    "ctrl": "ctrl",
    "control": "ctrl",
    "alt": "alt",
    "shift": "shift",
    "win": "win",
    "windows": "win",
    "meta": "win",
}
_MODIFIER_ORDER = ("ctrl", "alt", "shift", "win")
_NAMED_KEYS = {
    "enter": 0x0D,
    "tab": 0x09,
    "space": 0x20,
    "esc": 0x1B,
    "escape": 0x1B,
}
_VK_MODIFIERS = {
    "ctrl": 0x11,
    "alt": 0x12,
    "shift": 0x10,
    "win": 0x5B,
}
_KEYEVENTF_KEYUP = 0x0002


def _hostname(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    parsed = urlparse(text if "://" in text else f"//{text}")
    host = parsed.hostname or ""
    return host.rstrip(".").lower()


def domain_matches(url: str, expected_domain: str) -> bool:
    """Return whether *url* belongs to *expected_domain* or one of its subdomains."""
    host = _hostname(url)
    expected = _hostname(expected_domain)
    if not host or not expected:
        return False
    return host == expected or host.endswith(f".{expected}")


def normalize_hotkey(hotkey: str) -> tuple[str, ...]:
    """Validate and canonicalize a single KeePassXC global hotkey combination."""
    parts = [part.strip().lower() for part in (hotkey or "").split("+")]
    if any(not part for part in parts):
        raise ValueError("Hotkey must be a plus-separated key combination.")

    modifiers: set[str] = set()
    regular_keys: list[str] = []
    for part in parts:
        modifier = _MODIFIER_ALIASES.get(part)
        if modifier:
            if modifier in modifiers:
                raise ValueError(f"Duplicate hotkey modifier: {part}")
            modifiers.add(modifier)
            continue
        if _is_regular_key(part):
            regular_keys.append("esc" if part == "escape" else part)
            continue
        raise ValueError(f"Unsupported hotkey key: {part}")

    if not modifiers:
        raise ValueError("Hotkey must include at least one modifier.")
    if len(regular_keys) != 1:
        raise ValueError("Hotkey must include exactly one non-modifier key.")

    ordered_modifiers = [modifier for modifier in _MODIFIER_ORDER if modifier in modifiers]
    return tuple(ordered_modifiers + regular_keys)


def format_hotkey(hotkey: str | tuple[str, ...]) -> str:
    """Return a user-facing hotkey label such as ``Ctrl+Alt+A``."""
    parts = normalize_hotkey(hotkey) if isinstance(hotkey, str) else hotkey
    labels = {
        "ctrl": "Ctrl",
        "alt": "Alt",
        "shift": "Shift",
        "win": "Win",
        "esc": "Esc",
        "enter": "Enter",
        "tab": "Tab",
        "space": "Space",
    }
    return "+".join(labels.get(part, part.upper()) for part in parts)


def trigger_keepassxc_autotype(hotkey: str = DEFAULT_KEEPASSXC_AUTOTYPE_HOTKEY) -> None:
    """Send KeePassXC's global Auto-Type hotkey to the currently focused window."""
    keys = normalize_hotkey(hotkey)
    if sys.platform != "win32":
        raise RuntimeError("KeePassXC Auto-Type hotkey triggering is currently supported on Windows only.")

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    vk_codes = [_vk_code(key) for key in keys]
    pressed: list[int] = []
    try:
        for vk in vk_codes:
            user32.keybd_event(vk, 0, 0, 0)
            pressed.append(vk)
            time.sleep(0.03)
    finally:
        for vk in reversed(pressed):
            user32.keybd_event(vk, 0, _KEYEVENTF_KEYUP, 0)
            time.sleep(0.03)


def _is_regular_key(key: str) -> bool:
    return bool(
        re.fullmatch(r"[a-z0-9]", key)
        or re.fullmatch(r"f(?:[1-9]|1[0-9]|2[0-4])", key)
        or key in _NAMED_KEYS
    )


def _vk_code(key: str) -> int:
    if key in _VK_MODIFIERS:
        return _VK_MODIFIERS[key]
    if key in _NAMED_KEYS:
        return _NAMED_KEYS[key]
    if re.fullmatch(r"[a-z]", key):
        return ord(key.upper())
    if re.fullmatch(r"[0-9]", key):
        return ord(key)
    if re.fullmatch(r"f(?:[1-9]|1[0-9]|2[0-4])", key):
        return 0x70 + int(key[1:]) - 1
    raise ValueError(f"Unsupported hotkey key: {key}")
