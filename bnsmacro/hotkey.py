"""Global hotkeys via low-level hooks.

``RegisterHotKey`` cannot bind mouse side-buttons, and side-buttons are what
most people want for a macro trigger, so we install ``WH_KEYBOARD_LL`` and
``WH_MOUSE_LL`` hooks on a dedicated thread with its own message pump.

Events we injected ourselves are tagged with ``INJECT_TAG`` in
``dwExtraInfo`` and are skipped -- otherwise the macro's own keystrokes would
re-trigger it.
"""
from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set

from . import keys as K
from . import winapi as w

HOOKPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
)

w.user32.SetWindowsHookExW.argtypes = [
    ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD
]
w.user32.SetWindowsHookExW.restype = ctypes.c_void_p
w.user32.CallNextHookEx.argtypes = [
    ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
]
w.user32.CallNextHookEx.restype = ctypes.c_ssize_t
w.user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]

_KEY_DOWN_MSGS = {w.WM_KEYDOWN, w.WM_SYSKEYDOWN}
_KEY_UP_MSGS = {w.WM_KEYUP, w.WM_SYSKEYUP}
_MOUSE_DOWN_MSGS = {
    w.WM_LBUTTONDOWN: "mouseleft", w.WM_RBUTTONDOWN: "mouseright",
    w.WM_MBUTTONDOWN: "mousemiddle", w.WM_XBUTTONDOWN: None,
}
_MOUSE_UP_MSGS = {
    w.WM_LBUTTONUP: "mouseleft", w.WM_RBUTTONUP: "mouseright",
    w.WM_MBUTTONUP: "mousemiddle", w.WM_XBUTTONUP: None,
}

MODIFIER_VKS = {
    "ctrl": (0xA2, 0xA3), "shift": (0xA0, 0xA1), "alt": (0xA4, 0xA5),
}


@dataclass
class Binding:
    """One hotkey.

    ``mode``:
      ``"toggle"``  press once to start, again to stop
      ``"hold"``    active only while held down
      ``"press"``   fire ``on_press`` once per press, no on/off state
    """
    name: str
    key: str
    modifiers: List[str] = field(default_factory=list)
    mode: str = "toggle"
    swallow: bool = False
    on_press: Optional[Callable[[], None]] = None
    on_release: Optional[Callable[[], None]] = None
    enabled: bool = True

    def describe(self) -> str:
        parts = [m.capitalize() for m in self.modifiers] + [self.key]
        return "+".join(p for p in parts if p)


class HotkeyManager:
    def __init__(self) -> None:
        self._bindings: Dict[str, Binding] = {}
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._thread_id: Optional[int] = None
        self._down: Set[str] = set()
        self._kb_hook = None
        self._ms_hook = None
        self._kb_proc = None
        self._ms_proc = None
        self._ready = threading.Event()
        self._catch_all: Optional[Callable[[str], None]] = None
        self._catch_all_key: Optional[str] = None
        self.on_error: Optional[Callable[[BaseException], None]] = None

    # -- registration ------------------------------------------------------
    def set(self, binding: Binding) -> None:
        with self._lock:
            self._bindings[binding.name] = binding

    def remove(self, name: str) -> None:
        with self._lock:
            self._bindings.pop(name, None)

    def clear(self) -> None:
        with self._lock:
            self._bindings.clear()

    def set_catch_all(self, callback: Callable[[str], None]) -> None:
        """Route the *next* key/button press to *callback* and swallow it.

        This is how "按键捕获" works: while it is armed every key belongs to
        the capture dialog, not to the game or to any other binding.
        """
        with self._lock:
            self._catch_all = callback

    def clear_catch_all(self) -> None:
        with self._lock:
            self._catch_all = None

    # -- lifecycle ---------------------------------------------------------
    def start(self, timeout: float = 3.0) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="hotkeys", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("hotkey hook thread failed to start")

    def stop(self) -> None:
        if self._thread_id:
            w.user32.PostThreadMessageW(self._thread_id, w.WM_QUIT, 0, 0)
        if self._thread:
            self._thread.join(timeout=2.0)
        self._thread = None
        self._thread_id = None

    # -- hook thread -------------------------------------------------------
    def _run(self) -> None:
        self._thread_id = w.kernel32.GetCurrentThreadId()
        # keep references alive; a garbage-collected HOOKPROC crashes Windows
        self._kb_proc = HOOKPROC(self._on_keyboard)
        self._ms_proc = HOOKPROC(self._on_mouse)
        module = w.kernel32.GetModuleHandleW(None)
        self._kb_hook = w.user32.SetWindowsHookExW(
            w.WH_KEYBOARD_LL, self._kb_proc, module, 0)
        self._ms_hook = w.user32.SetWindowsHookExW(
            w.WH_MOUSE_LL, self._ms_proc, module, 0)
        self._ready.set()
        try:
            msg = wintypes.MSG()
            while w.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                w.user32.TranslateMessage(ctypes.byref(msg))
                w.user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            if self._kb_hook:
                w.user32.UnhookWindowsHookEx(self._kb_hook)
            if self._ms_hook:
                w.user32.UnhookWindowsHookEx(self._ms_hook)
            self._kb_hook = self._ms_hook = None

    # -- dispatch ----------------------------------------------------------
    def _modifiers_held(self, mods) -> bool:
        for m in mods:
            vks = MODIFIER_VKS.get(m)
            if not vks:
                continue
            if not any(w.user32.GetAsyncKeyState(vk) & 0x8000 for vk in vks):
                return False
        return True

    def _dispatch(self, key: str, pressed: bool) -> bool:
        """Return True if the event should be swallowed."""
        swallow = False
        with self._lock:
            catch_all = self._catch_all
            pending = self._catch_all_key
            bindings = list(self._bindings.values())

        if not pressed and pending == key:
            # tail of a captured press -- swallow it too, or the game sees a
            # lone key-up for a key it never saw go down
            with self._lock:
                self._catch_all_key = None
            self._down.discard(key)
            return True

        if catch_all is not None and pressed:
            # a capture is armed: this press belongs to it and to nobody else
            with self._lock:
                self._catch_all = None
                self._catch_all_key = key
            self._down.add(key)
            try:
                catch_all(key)
            except Exception as exc:
                if self.on_error:
                    self.on_error(exc)
            return True

        for b in bindings:
            if not b.enabled or K.normalize(b.key) != key:
                continue
            if pressed and not self._modifiers_held(b.modifiers):
                continue
            try:
                if pressed:
                    if key in self._down and b.mode != "press":
                        continue  # ignore auto-repeat
                    if b.on_press:
                        b.on_press()
                elif b.mode == "hold" and b.on_release:
                    b.on_release()
            except Exception as exc:  # never let a callback kill the hook
                if self.on_error:
                    self.on_error(exc)
            swallow = swallow or b.swallow
        if pressed:
            self._down.add(key)
        else:
            self._down.discard(key)
        return swallow

    def _on_keyboard(self, code, wparam, lparam):
        if code == 0:
            data = ctypes.cast(lparam, ctypes.POINTER(w.KBDLLHOOKSTRUCT)).contents
            if data.dwExtraInfo != w.INJECT_TAG:
                name = K.name_of_vk(data.vkCode)
                if wparam in _KEY_DOWN_MSGS:
                    if self._dispatch(name, True):
                        return 1
                elif wparam in _KEY_UP_MSGS:
                    if self._dispatch(name, False):
                        return 1
        return w.user32.CallNextHookEx(None, code, wparam, lparam)

    def _on_mouse(self, code, wparam, lparam):
        if code == 0:
            data = ctypes.cast(lparam, ctypes.POINTER(w.MSLLHOOKSTRUCT)).contents
            if data.dwExtraInfo != w.INJECT_TAG:
                button = None
                pressed = None
                if wparam in _MOUSE_DOWN_MSGS:
                    button, pressed = _MOUSE_DOWN_MSGS[wparam], True
                elif wparam in _MOUSE_UP_MSGS:
                    button, pressed = _MOUSE_UP_MSGS[wparam], False
                if pressed is not None:
                    if button is None:  # X button -- which one is in the hi word
                        button = "x2" if (data.mouseData >> 16) == w.XBUTTON2 else "x1"
                    if self._dispatch(button, pressed):
                        return 1
        return w.user32.CallNextHookEx(None, code, wparam, lparam)


w.user32.GetMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT
]
w.user32.PostThreadMessageW.argtypes = [
    wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
]
w.user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
w.user32.GetAsyncKeyState.restype = ctypes.c_short
w.kernel32.GetModuleHandleW.restype = wintypes.HMODULE
w.kernel32.GetCurrentThreadId.restype = wintypes.DWORD
