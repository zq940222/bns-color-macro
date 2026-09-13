"""Screen capture.

The engine only ever needs a handful of pixels, but reading them one at a
time with ``GetPixel`` costs a round trip to the GDI driver each -- roughly
0.5 ms per pixel, which blows the frame budget once you have a dozen probes.
Instead we blit the smallest rectangle that covers every probe once per tick
and read the pixels out of the resulting numpy array.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

import numpy as np

from . import winapi as w

DESKTOP = "desktop"
WINDOW = "window"


@dataclass(frozen=True)
class Box:
    left: int
    top: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom

    @staticmethod
    def bounding(points: Sequence[Tuple[int, int]], pad: int = 1) -> "Box":
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        left, top = min(xs) - pad, min(ys) - pad
        right, bottom = max(xs) + pad + 1, max(ys) + pad + 1
        return Box(left, top, right - left, bottom - top)


class CaptureError(RuntimeError):
    pass


class _Surface:
    """A memory DC + DIB section we can blit into and read back as numpy."""

    def __init__(self, screen_dc):
        self._screen_dc = screen_dc
        self.dc = w.gdi32.CreateCompatibleDC(screen_dc)
        if not self.dc:
            raise CaptureError("CreateCompatibleDC failed")
        self.bitmap = None
        self._old = None
        self.size: Tuple[int, int] = (0, 0)
        self.buffer: Optional[np.ndarray] = None

    def resize(self, width: int, height: int) -> None:
        if self.size == (width, height):
            return
        if self.bitmap:
            if self._old:
                w.gdi32.SelectObject(self.dc, self._old)
            w.gdi32.DeleteObject(self.bitmap)
        self.bitmap = w.gdi32.CreateCompatibleBitmap(self._screen_dc, width, height)
        if not self.bitmap:
            raise CaptureError("CreateCompatibleBitmap failed")
        self._old = w.gdi32.SelectObject(self.dc, self.bitmap)
        self.size = (width, height)
        self.buffer = np.empty((height, width, 4), dtype=np.uint8)

    def read(self) -> np.ndarray:
        """Copy the bitmap into ``self.buffer`` and return it as RGB."""
        width, height = self.size
        info = w.BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(w.BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        # negative height -> top-down rows, which matches numpy's layout
        info.bmiHeader.biHeight = -height
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = w.BI_RGB
        assert self.buffer is not None
        copied = w.gdi32.GetDIBits(
            self.dc, self.bitmap, 0, height,
            self.buffer.ctypes.data_as(ctypes.c_void_p), ctypes.byref(info),
            w.DIB_RGB_COLORS,
        )
        if copied == 0:
            raise CaptureError("GetDIBits failed")
        # GDI hands back BGRA; drop alpha and flip to RGB.
        return self.buffer[:, :, 2::-1]

    def close(self) -> None:
        if self.bitmap:
            if self._old:
                w.gdi32.SelectObject(self.dc, self._old)
            w.gdi32.DeleteObject(self.bitmap)
            self.bitmap = None
        if self.dc:
            w.gdi32.DeleteDC(self.dc)
            self.dc = None


class ScreenCapture:
    """Reusable GDI capture surface.

    ``mode`` is either:

    ``"desktop"``
        BitBlt straight off the screen DC.  Fast, and the right default, but
        it reads whatever is *on screen* -- so the game window must be
        visible and not covered.

    ``"window"``
        PrintWindow with ``PW_RENDERFULLCONTENT``.  Slower (it renders the
        whole window every tick) but keeps working when the window is
        partially occluded.  Not all DirectX clients cooperate.

    Neither mode can read a *fullscreen-exclusive* DirectX client -- that
    returns black.  Run the game in 窗口模式 / 无边框窗口 instead.
    """

    def __init__(self, mode: str = DESKTOP, hwnd: Optional[int] = None):
        self.mode = mode
        self.hwnd = hwnd
        self._screen_dc = None
        self._surface: Optional[_Surface] = None

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        if self._surface:
            self._surface.close()
            self._surface = None
        if self._screen_dc:
            w.user32.ReleaseDC(None, self._screen_dc)
            self._screen_dc = None

    def __enter__(self) -> "ScreenCapture":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _ensure(self) -> _Surface:
        if self._screen_dc is None:
            self._screen_dc = w.user32.GetDC(None)
            if not self._screen_dc:
                raise CaptureError("GetDC(NULL) failed")
        if self._surface is None:
            self._surface = _Surface(self._screen_dc)
        return self._surface

    # -- public ------------------------------------------------------------
    def grab(self, box: Box) -> np.ndarray:
        """Return a ``(h, w, 3)`` uint8 RGB array for *box* in screen coords."""
        if box.width <= 0 or box.height <= 0:
            raise CaptureError(f"empty capture box: {box}")
        surface = self._ensure()

        if self.mode == WINDOW and self.hwnd:
            rect = w.RECT()
            if not w.user32.GetWindowRect(self.hwnd, ctypes.byref(rect)):
                raise CaptureError("GetWindowRect failed -- window gone?")
            win_w, win_h = rect.right - rect.left, rect.bottom - rect.top
            if win_w <= 0 or win_h <= 0:
                raise CaptureError("target window has no area")
            surface.resize(win_w, win_h)
            if not w.user32.PrintWindow(self.hwnd, surface.dc, w.PW_RENDERFULLCONTENT):
                raise CaptureError("PrintWindow failed on the target window")
            frame = surface.read()
            # translate the requested screen box into window-local coords
            x0, y0 = box.left - rect.left, box.top - rect.top
            x1, y1 = x0 + box.width, y0 + box.height
            if x0 < 0 or y0 < 0 or x1 > win_w or y1 > win_h:
                raise CaptureError("probe box falls outside the target window")
            return frame[y0:y1, x0:x1]

        surface.resize(box.width, box.height)
        ok = w.gdi32.BitBlt(
            surface.dc, 0, 0, box.width, box.height,
            self._screen_dc, box.left, box.top, w.SRCCOPY | w.CAPTUREBLT,
        )
        if not ok:
            raise CaptureError(
                "BitBlt failed -- if the game is in fullscreen-exclusive mode, "
                "switch it to 窗口模式 / 无边框窗口"
            )
        return surface.read()

    def pixel(self, x: int, y: int) -> Tuple[int, int, int]:
        px = self.grab(Box(x, y, 1, 1))[0, 0]
        return int(px[0]), int(px[1]), int(px[2])


def virtual_screen_box() -> Box:
    """Bounding box of every monitor, in screen coordinates."""
    x = w.user32.GetSystemMetrics(w.SM_XVIRTUALSCREEN)
    y = w.user32.GetSystemMetrics(w.SM_YVIRTUALSCREEN)
    cx = w.user32.GetSystemMetrics(w.SM_CXVIRTUALSCREEN)
    cy = w.user32.GetSystemMetrics(w.SM_CYVIRTUALSCREEN)
    return Box(x, y, cx, cy)
