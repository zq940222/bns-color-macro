"""The colour picker: a live magnifier plus a global "grab this pixel" key.

Sampling runs on a worker thread.  It has to: one GDI read off the live
screen blocks for a whole display refresh, and doing that on the Tk main
thread would make the window visibly stutter.
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional, Tuple

import numpy as np

from ..capture import Box, CaptureError, ScreenCapture
from ..hotkey import Binding, HotkeyManager
from ..input import cursor_pos
from .widgets import ColorSwatch, ppm_photo, rgb_to_hex

RADIUS = 12          # magnifier samples a (2R+1) square around the cursor
ZOOM = 9             # pixels per sampled pixel
REFRESH_MS = 40


class _Sampler(threading.Thread):
    """Grabs a small square around the cursor as fast as the screen allows."""

    def __init__(self, out: "queue.Queue", capture_mode: str = "desktop"):
        super().__init__(name="picker-sampler", daemon=True)
        self._out = out
        self._stop = threading.Event()
        self._capture_mode = capture_mode

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        capture = ScreenCapture(self._capture_mode)
        try:
            while not self._stop.is_set():
                x, y = cursor_pos()
                box = Box(x - RADIUS, y - RADIUS, RADIUS * 2 + 1, RADIUS * 2 + 1)
                try:
                    frame = capture.grab(box).copy()
                    payload = ((x, y), frame, None)
                except CaptureError as exc:
                    payload = ((x, y), None, str(exc))
                if self._out.full():
                    try:
                        self._out.get_nowait()
                    except queue.Empty:
                        pass
                self._out.put(payload)
        finally:
            capture.close()


class PickerPanel(ttk.LabelFrame):
    """Magnifier + pick hotkey.

    *on_pick* is called on the Tk thread with ``(x, y, (r, g, b))`` in screen
    coordinates; the caller decides what to do with it (new probe, recolour
    an existing condition, ...).
    """

    def __init__(self, master, hotkeys: HotkeyManager,
                 on_pick: Callable[[int, int, Tuple[int, int, int]], None],
                 pick_key: str = "f8", **kw):
        super().__init__(master, text="取色器", **kw)
        self._hotkeys = hotkeys
        self._on_pick = on_pick
        self._pick_key = pick_key
        self._queue: "queue.Queue" = queue.Queue(maxsize=1)
        self._sampler: Optional[_Sampler] = None
        self._photo: Optional[tk.PhotoImage] = None
        self._last: Optional[Tuple[Tuple[int, int], Tuple[int, int, int]]] = None
        self._after_id: Optional[str] = None
        self._pending_pick = threading.Event()

        side = RADIUS * 2 + 1
        self.canvas = tk.Canvas(self, width=side * ZOOM, height=side * ZOOM,
                                highlightthickness=1, highlightbackground="#8a8a8a",
                                background="#1e1e1e")
        self.canvas.grid(row=0, column=0, rowspan=5, padx=8, pady=8)
        centre = side * ZOOM // 2
        half = ZOOM // 2 + 1
        # crosshair marking the exact pixel under the cursor
        self.canvas.create_rectangle(centre - half, centre - half,
                                     centre + half, centre + half,
                                     outline="#ff3b30", width=2, tags="cross")

        info = ttk.Frame(self)
        info.grid(row=0, column=1, sticky="nw", pady=8, padx=(0, 8))

        self.var_pos = tk.StringVar(value="坐标  -")
        self.var_hex = tk.StringVar(value="颜色  -")
        self.var_rgb = tk.StringVar(value="RGB   -")
        for var in (self.var_pos, self.var_hex, self.var_rgb):
            ttk.Label(info, textvariable=var, font=("Consolas", 10)).pack(anchor="w")

        self.swatch = ColorSwatch(info, width=150, height=28)
        self.swatch.pack(anchor="w", pady=(6, 8))

        self.btn = ttk.Button(info, text="开始取色", command=self.toggle)
        self.btn.pack(anchor="w", fill="x")
        self.var_state = tk.StringVar(value="")
        ttk.Label(info, textvariable=self.var_state, foreground="#666666",
                  wraplength=240, justify="left").pack(anchor="w", pady=(6, 0))
        self._set_idle_hint()

    # -- state -------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._sampler is not None and self._sampler.is_alive()

    def _set_idle_hint(self) -> None:
        self.var_state.set(
            f"点「开始取色」后把鼠标移到游戏画面上，按 {self._pick_key.upper()} "
            f"记录当前像素。"
        )

    def set_pick_key(self, key: str) -> None:
        was_running = self.running
        if was_running:
            self.stop()
        self._pick_key = key or "f8"
        self._set_idle_hint()
        if was_running:
            self.start()

    # -- lifecycle ---------------------------------------------------------
    def toggle(self) -> None:
        self.stop() if self.running else self.start()

    def start(self) -> None:
        if self.running:
            return
        self._sampler = _Sampler(self._queue)
        self._sampler.start()
        self._hotkeys.set(Binding(
            name="__picker__", key=self._pick_key, mode="press",
            swallow=True, on_press=self._pending_pick.set,
        ))
        self.btn.configure(text="停止取色")
        self.var_state.set(f"取色中 — 按 {self._pick_key.upper()} 记录当前像素")
        self._tick()

    def stop(self) -> None:
        if self._sampler:
            self._sampler.stop()
            self._sampler = None
        self._hotkeys.remove("__picker__")
        if self._after_id:
            try:
                self.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None
        self.btn.configure(text="开始取色")
        self._set_idle_hint()

    def destroy(self) -> None:  # noqa: D401 - tk override
        self.stop()
        super().destroy()

    # -- ui loop -----------------------------------------------------------
    def _tick(self) -> None:
        if not self.running:
            return
        try:
            pos, frame, error = self._queue.get_nowait()
        except queue.Empty:
            frame, error, pos = None, None, None
        if error:
            self.var_state.set(f"取色失败: {error}")
        elif frame is not None and pos is not None:
            self._render(pos, frame)
        if self._pending_pick.is_set():
            self._pending_pick.clear()
            self._emit_pick()
        self._after_id = self.after(REFRESH_MS, self._tick)

    def _render(self, pos: Tuple[int, int], frame: np.ndarray) -> None:
        centre = frame[frame.shape[0] // 2, frame.shape[1] // 2]
        rgb = (int(centre[0]), int(centre[1]), int(centre[2]))
        self._last = (pos, rgb)
        # nearest-neighbour zoom keeps individual pixels crisp and square
        zoomed = np.repeat(np.repeat(frame, ZOOM, axis=0), ZOOM, axis=1)
        self._photo = ppm_photo(zoomed)
        self.canvas.delete("mag")
        self.canvas.create_image(0, 0, anchor="nw", image=self._photo, tags="mag")
        self.canvas.tag_lower("mag")
        self.var_pos.set(f"坐标  {pos[0]}, {pos[1]}")
        self.var_hex.set(f"颜色  {rgb_to_hex(rgb)}")
        self.var_rgb.set(f"RGB   {rgb[0]}, {rgb[1]}, {rgb[2]}")
        self.swatch.set(rgb)

    def _emit_pick(self) -> None:
        if not self._last:
            return
        (x, y), rgb = self._last
        try:
            self._on_pick(x, y, rgb)
        except Exception as exc:  # a bad callback must not kill the picker
            self.var_state.set(f"取色回调出错: {exc}")
