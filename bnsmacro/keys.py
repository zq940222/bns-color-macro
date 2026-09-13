"""Key name <-> virtual-key / scancode tables.

Names are case-insensitive and accept a few Chinese aliases so profiles read
naturally (``"小键盘1"``, ``"空格"``, ``"侧键1"`` ...).
"""
from __future__ import annotations

from typing import Dict, Optional, Tuple

from . import winapi as w

# virtual-key codes we care about
VK: Dict[str, int] = {}

for _c in "0123456789":
    VK[_c] = ord(_c)
for _c in "abcdefghijklmnopqrstuvwxyz":
    VK[_c] = ord(_c.upper())
for _i in range(1, 25):
    VK[f"f{_i}"] = 0x6F + _i

VK.update({
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "return": 0x0D,
    "shift": 0xA0, "lshift": 0xA0, "rshift": 0xA1,
    "ctrl": 0xA2, "lctrl": 0xA2, "rctrl": 0xA3,
    "alt": 0xA4, "lalt": 0xA4, "ralt": 0xA5,
    "pause": 0x13, "capslock": 0x14, "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "pageup": 0x21, "pagedown": 0x22,
    "end": 0x23, "home": 0x24,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "printscreen": 0x2C, "insert": 0x2D, "delete": 0x2E,
    "lwin": 0x5B, "rwin": 0x5C, "apps": 0x5D,
    "numlock": 0x90, "scrolllock": 0x91,
    "multiply": 0x6A, "add": 0x6B, "subtract": 0x6D,
    "decimal": 0x6E, "divide": 0x6F,
    ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF,
    "`": 0xC0, "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
})
for _i in range(10):
    VK[f"num{_i}"] = 0x60 + _i

# keys that live on the "extended" half of the keyboard and need
# KEYEVENTF_EXTENDEDKEY, otherwise games read them as the numpad twin.
EXTENDED = {
    0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,  # pgup/pgdn/end/home/arrows
    0x2C, 0x2D, 0x2E,                                 # printscreen/insert/delete
    0x5B, 0x5C, 0x5D,                                 # win keys / apps
    0x6F,                                             # numpad divide
    0x90,                                             # numlock
    0xA3, 0xA5,                                       # right ctrl / right alt
    0x0D,  # only true for numpad enter; harmless for the main one in practice
}

ALIASES = {
    "空格": "space", "回车": "enter", "退格": "backspace", "上档": "shift",
    "控制": "ctrl", "换挡": "alt", "制表": "tab", "退出": "esc",
    "上": "up", "下": "down", "左": "left", "右": "right",
    "插入": "insert", "删除": "delete", "翻上": "pageup", "翻下": "pagedown",
}
for _i in range(10):
    ALIASES[f"小键盘{_i}"] = f"num{_i}"

# Canonical mouse names are deliberately prefixed: plain "left"/"right" are
# already arrow keys, and silently turning 方向键右 into 右键 would be a very
# confusing bug to chase.
MOUSE_BUTTONS = {
    "mouseleft": "mouseleft", "leftclick": "mouseleft", "左键": "mouseleft",
    "mouseright": "mouseright", "rightclick": "mouseright", "右键": "mouseright",
    "mousemiddle": "mousemiddle", "middleclick": "mousemiddle",
    "中键": "mousemiddle",
    "x1": "x1", "mouseback": "x1", "xbutton1": "x1", "侧键1": "x1",
    "后退键": "x1",
    "x2": "x2", "mouseforward": "x2", "xbutton2": "x2", "侧键2": "x2",
    "前进键": "x2",
    "wheelup": "wheelup", "滚轮上": "wheelup",
    "wheeldown": "wheeldown", "滚轮下": "wheeldown",
}

MOUSE_VK = {"mouseleft": 0x01, "mouseright": 0x02, "mousemiddle": 0x04,
            "x1": 0x05, "x2": 0x06}
VK_TO_MOUSE = {v: k for k, v in MOUSE_VK.items()}


def normalize(name: str) -> str:
    """Canonical, lower-case form of a key name.

    Mouse buttons collapse onto ``left/right/middle/x1/x2/wheel*`` so that a
    profile saying ``"MouseForward"`` and a hook event saying ``"x2"`` compare
    equal.  Idempotent.
    """
    name = (name or "").strip()
    lowered = name.lower()
    resolved = ALIASES.get(name, ALIASES.get(lowered, lowered))
    return MOUSE_BUTTONS.get(resolved, resolved)


def is_mouse(name: str) -> bool:
    return normalize(name) in MOUSE_BUTTONS


def mouse_button(name: str) -> Optional[str]:
    return MOUSE_BUTTONS.get(normalize(name))


def vk_of(name: str) -> Optional[int]:
    n = normalize(name)
    if n in MOUSE_BUTTONS:
        return MOUSE_VK.get(MOUSE_BUTTONS[n])
    return VK.get(n)


def scancode_of(vk: int) -> Tuple[int, bool]:
    """Return ``(scancode, extended)`` for a virtual-key code."""
    sc = w.user32.MapVirtualKeyW(vk, w.MAPVK_VK_TO_VSC_EX)
    extended = bool(sc & 0xE000) or vk in EXTENDED
    return sc & 0xFF, extended


def resolve(name: str) -> Optional[Tuple[int, int, bool]]:
    """``name`` -> ``(vk, scancode, extended)``; ``None`` if unknown."""
    vk = vk_of(name)
    if vk is None:
        return None
    sc, ext = scancode_of(vk)
    return vk, sc, ext


_VK_NAMES = {}
for _name, _vk in VK.items():
    _VK_NAMES.setdefault(_vk, _name)


def name_of_vk(vk: int) -> str:
    if vk in VK_TO_MOUSE:
        return VK_TO_MOUSE[vk]
    return _VK_NAMES.get(vk, f"vk{vk:02X}")


def parse_combo(text: str) -> Tuple[list, str]:
    """Split ``"ctrl+alt+q"`` into ``(["ctrl", "alt"], "q")``."""
    parts = [p for p in str(text).replace("＋", "+").split("+") if p.strip()]
    if not parts:
        return [], ""
    return [normalize(p) for p in parts[:-1]], normalize(parts[-1])
