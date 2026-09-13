"""Target-window discovery and coordinate anchoring.

Probe coordinates are stored **relative to the game's client area** at the
resolution they were picked on.  That way a profile survives the player
moving the window, and survives a resolution change as long as the UI scales
proportionally.  Absolute screen coordinates are supported too, for people
who never move the window.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
from typing import List, Optional, Tuple

from . import winapi as w
from .capture import Box

# Blade & Soul's Unreal 3 client. Kept as a hint, not a hard requirement --
# private/classic servers rename the window freely.
DEFAULT_CLASS_HINTS = ("LaunchUnrealUWindowsClient", "UnrealWindow")
DEFAULT_TITLE_HINTS = ("Blade & Soul", "BNS", "剑灵")

ANCHOR_CLIENT = "client"
ANCHOR_SCREEN = "screen"


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    class_name: str
    client: Box  # client area, in screen coordinates

    @property
    def size(self) -> Tuple[int, int]:
        return self.client.width, self.client.height


def _text(fn, hwnd, length: int = 512) -> str:
    buf = ctypes.create_unicode_buffer(length)
    fn(hwnd, buf, length)
    return buf.value


def client_box(hwnd: int) -> Optional[Box]:
    """Client rectangle of *hwnd*, translated into screen coordinates."""
    rect = w.RECT()
    if not w.user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    origin = w.POINT(0, 0)
    if not w.user32.ClientToScreen(hwnd, ctypes.byref(origin)):
        return None
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return None
    return Box(origin.x, origin.y, width, height)


def describe(hwnd: int) -> Optional[WindowInfo]:
    if not hwnd or not w.user32.IsWindow(hwnd):
        return None
    box = client_box(hwnd)
    if box is None:
        return None
    return WindowInfo(
        hwnd=hwnd,
        title=_text(w.user32.GetWindowTextW, hwnd),
        class_name=_text(w.user32.GetClassNameW, hwnd, 256),
        client=box,
    )


_ENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
w.user32.EnumWindows.argtypes = [_ENUMPROC, ctypes.c_void_p]
w.user32.IsWindowVisible.argtypes = [ctypes.c_void_p]


def list_windows(min_area: int = 200 * 200) -> List[WindowInfo]:
    """Every visible top-level window big enough to be a game client."""
    found: List[WindowInfo] = []

    def cb(hwnd, _lparam):
        if not w.user32.IsWindowVisible(hwnd):
            return True
        info = describe(hwnd)
        if info and info.title and info.client.width * info.client.height >= min_area:
            found.append(info)
        return True

    w.user32.EnumWindows(_ENUMPROC(cb), None)
    return found


def find_game_window(title_hint: str = "", class_hint: str = "") -> Optional[WindowInfo]:
    """Best-effort search for the game client.

    An explicit *title_hint* wins; otherwise we fall back to the known Unreal
    class names and the usual titles.  Returns ``None`` rather than guessing
    wildly -- the UI lets the user pick from a list.
    """
    windows = list_windows()
    if title_hint:
        hint = title_hint.lower()
        for info in windows:
            if hint in info.title.lower():
                return info
    if class_hint:
        for info in windows:
            if info.class_name == class_hint:
                return info
    for info in windows:
        if info.class_name in DEFAULT_CLASS_HINTS:
            return info
    for info in windows:
        low = info.title.lower()
        if any(h.lower() in low for h in DEFAULT_TITLE_HINTS):
            return info
    return None


def foreground_hwnd() -> int:
    return w.user32.GetForegroundWindow()


class Anchor:
    """Translates profile coordinates to live screen coordinates."""

    def __init__(
        self,
        mode: str = ANCHOR_CLIENT,
        reference_size: Optional[Tuple[int, int]] = None,
        scale_with_resolution: bool = True,
    ):
        self.mode = mode
        self.reference_size = reference_size
        self.scale_with_resolution = scale_with_resolution
        self.window: Optional[WindowInfo] = None

    def bind(self, window: Optional[WindowInfo]) -> None:
        self.window = window

    def to_screen(self, x: int, y: int) -> Tuple[int, int]:
        if self.mode == ANCHOR_SCREEN or self.window is None:
            return int(x), int(y)
        box = self.window.client
        fx, fy = float(x), float(y)
        ref = self.reference_size
        if self.scale_with_resolution and ref and ref[0] > 0 and ref[1] > 0:
            fx *= box.width / ref[0]
            fy *= box.height / ref[1]
        return int(round(box.left + fx)), int(round(box.top + fy))

    def to_profile(self, screen_x: int, screen_y: int) -> Tuple[int, int]:
        if self.mode == ANCHOR_SCREEN or self.window is None:
            return int(screen_x), int(screen_y)
        box = self.window.client
        fx, fy = screen_x - box.left, screen_y - box.top
        ref = self.reference_size
        if self.scale_with_resolution and ref and box.width > 0 and box.height > 0:
            fx *= ref[0] / box.width
            fy *= ref[1] / box.height
        return int(round(fx)), int(round(fy))
