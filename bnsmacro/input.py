"""Synthetic keyboard / mouse input.

``SendInput`` with **scancodes** is the only mode worth having: DirectX
clients read raw input, so virtual-key-only events and ``PostMessage`` are
simply ignored by the game.  Every event we inject carries ``INJECT_TAG`` in
``dwExtraInfo`` so the hotkey hook can tell our own keystrokes apart from the
player's.
"""
from __future__ import annotations

import ctypes
import time
from typing import Iterable

from . import keys as K
from . import winapi as w


class InputError(RuntimeError):
    pass


def _send(inputs: Iterable[w.INPUT]) -> int:
    arr = list(inputs)
    if not arr:
        return 0
    buf = (w.INPUT * len(arr))(*arr)
    sent = w.user32.SendInput(len(arr), buf, ctypes.sizeof(w.INPUT))
    if sent != len(arr):
        raise InputError(
            f"SendInput sent {sent}/{len(arr)} events "
            f"(err={ctypes.get_last_error()}). If the game runs elevated, "
            f"run this tool as administrator too."
        )
    return sent


def _key_event(scancode: int, extended: bool, up: bool) -> w.INPUT:
    flags = w.KEYEVENTF_SCANCODE
    if extended:
        flags |= w.KEYEVENTF_EXTENDEDKEY
    if up:
        flags |= w.KEYEVENTF_KEYUP
    ev = w.INPUT(type=w.INPUT_KEYBOARD)
    ev.ki = w.KEYBDINPUT(wVk=0, wScan=scancode, dwFlags=flags, time=0,
                         dwExtraInfo=w.INJECT_TAG)
    return ev


_MOUSE_DOWN = {
    "mouseleft": (w.MOUSEEVENTF_LEFTDOWN, 0),
    "mouseright": (w.MOUSEEVENTF_RIGHTDOWN, 0),
    "mousemiddle": (w.MOUSEEVENTF_MIDDLEDOWN, 0),
    "x1": (w.MOUSEEVENTF_XDOWN, w.XBUTTON1),
    "x2": (w.MOUSEEVENTF_XDOWN, w.XBUTTON2),
}
_MOUSE_UP = {
    "mouseleft": (w.MOUSEEVENTF_LEFTUP, 0),
    "mouseright": (w.MOUSEEVENTF_RIGHTUP, 0),
    "mousemiddle": (w.MOUSEEVENTF_MIDDLEUP, 0),
    "x1": (w.MOUSEEVENTF_XUP, w.XBUTTON1),
    "x2": (w.MOUSEEVENTF_XUP, w.XBUTTON2),
}


def _mouse_event(flags: int, data: int = 0, dx: int = 0, dy: int = 0) -> w.INPUT:
    ev = w.INPUT(type=w.INPUT_MOUSE)
    ev.mi = w.MOUSEINPUT(dx=dx, dy=dy, mouseData=data, dwFlags=flags, time=0,
                         dwExtraInfo=w.INJECT_TAG)
    return ev


class Sender:
    """Stateful sender that remembers which keys it is holding down.

    ``release_all`` matters: if the macro is stopped mid-sequence we must not
    leave a key latched in the game.
    """

    def __init__(self) -> None:
        self._held_keys = set()      # scancode, extended
        self._held_buttons = set()   # button name

    # -- keyboard ----------------------------------------------------------
    def key_down(self, name: str) -> None:
        btn = K.mouse_button(name)
        if btn:
            return self.button_down(btn)
        resolved = K.resolve(name)
        if not resolved:
            raise InputError(f"unknown key name: {name!r}")
        _, sc, ext = resolved
        _send([_key_event(sc, ext, up=False)])
        self._held_keys.add((sc, ext))

    def key_up(self, name: str) -> None:
        btn = K.mouse_button(name)
        if btn:
            return self.button_up(btn)
        resolved = K.resolve(name)
        if not resolved:
            raise InputError(f"unknown key name: {name!r}")
        _, sc, ext = resolved
        _send([_key_event(sc, ext, up=True)])
        self._held_keys.discard((sc, ext))

    def tap(self, name: str, hold_ms: int = 30) -> None:
        """Press and release, holding for *hold_ms*.

        Games sample the keyboard once a frame; a 0 ms tap is regularly
        missed, so the default hold spans at least one 60 Hz frame.
        """
        self.key_down(name)
        if hold_ms > 0:
            time.sleep(hold_ms / 1000.0)
        self.key_up(name)

    def combo(self, modifiers: Iterable[str], key: str, hold_ms: int = 30) -> None:
        mods = list(modifiers)
        for m in mods:
            self.key_down(m)
        try:
            self.tap(key, hold_ms)
        finally:
            for m in reversed(mods):
                self.key_up(m)

    # -- mouse -------------------------------------------------------------
    def button_down(self, button: str) -> None:
        if button in ("wheelup", "wheeldown"):
            delta = 120 if button == "wheelup" else -120
            _send([_mouse_event(w.MOUSEEVENTF_WHEEL, data=delta & 0xFFFFFFFF)])
            return
        flags, data = _MOUSE_DOWN[button]
        _send([_mouse_event(flags, data)])
        self._held_buttons.add(button)

    def button_up(self, button: str) -> None:
        if button in ("wheelup", "wheeldown"):
            return
        flags, data = _MOUSE_UP[button]
        _send([_mouse_event(flags, data)])
        self._held_buttons.discard(button)

    def click(self, button: str, hold_ms: int = 30) -> None:
        self.button_down(button)
        if hold_ms > 0:
            time.sleep(hold_ms / 1000.0)
        self.button_up(button)

    def move_relative(self, dx: int, dy: int) -> None:
        """Relative mouse move -- this is what rotates the camera in-game."""
        _send([_mouse_event(w.MOUSEEVENTF_MOVE, dx=int(dx), dy=int(dy))])

    # -- safety ------------------------------------------------------------
    def release_all(self) -> None:
        for sc, ext in list(self._held_keys):
            try:
                _send([_key_event(sc, ext, up=True)])
            except InputError:
                pass
        self._held_keys.clear()
        for button in list(self._held_buttons):
            try:
                flags, data = _MOUSE_UP[button]
                _send([_mouse_event(flags, data)])
            except (InputError, KeyError):
                pass
        self._held_buttons.clear()


def cursor_pos() -> tuple:
    pt = w.POINT()
    w.user32.GetCursorPos(ctypes.byref(pt))
    return int(pt.x), int(pt.y)
