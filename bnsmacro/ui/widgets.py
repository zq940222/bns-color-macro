"""Small reusable tkinter pieces."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Optional, Sequence

import numpy as np


def rgb_to_hex(rgb: Sequence[int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(int(rgb[0]), int(rgb[1]), int(rgb[2]))


def readable_on(rgb: Sequence[int]) -> str:
    """Black or white, whichever stays legible on *rgb*."""
    luma = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
    return "#000000" if luma > 140 else "#FFFFFF"


class ColorSwatch(tk.Canvas):
    """A flat colour chip that can show its own hex value."""

    def __init__(self, master, width: int = 54, height: int = 20,
                 show_text: bool = True, **kw):
        super().__init__(master, width=width, height=height, highlightthickness=1,
                         highlightbackground="#8a8a8a", bd=0, **kw)
        self._show_text = show_text
        self._rect = self.create_rectangle(0, 0, width, height, width=0, fill="#000000")
        self._text = self.create_text(width // 2, height // 2, text="",
                                      font=("Consolas", 8))
        self.set((0, 0, 0))

    def set(self, rgb: Sequence[int]) -> None:
        hex_value = rgb_to_hex(rgb)
        self.itemconfigure(self._rect, fill=hex_value)
        self.itemconfigure(
            self._text,
            text=hex_value if self._show_text else "",
            fill=readable_on(rgb),
        )


def ppm_photo(image: np.ndarray) -> tk.PhotoImage:
    """Wrap an ``(h, w, 3)`` uint8 RGB array in a Tk PhotoImage."""
    height, width = image.shape[0], image.shape[1]
    header = f"P6 {width} {height} 255 ".encode("ascii")
    return tk.PhotoImage(data=header + np.ascontiguousarray(image, np.uint8).tobytes())


class LabeledEntry(ttk.Frame):
    """Label + entry bound to a tk variable, laid out on one row."""

    def __init__(self, master, label: str, variable, width: int = 10,
                 suffix: str = "", **kw):
        super().__init__(master, **kw)
        ttk.Label(self, text=label).pack(side="left")
        self.entry = ttk.Entry(self, textvariable=variable, width=width)
        self.entry.pack(side="left", padx=(4, 2))
        if suffix:
            ttk.Label(self, text=suffix).pack(side="left")


class ScrolledTree(ttk.Frame):
    """A Treeview with a vertical scrollbar, because it is always wanted."""

    def __init__(self, master, columns, height: int = 8, **kw):
        super().__init__(master, **kw)
        self.tree = ttk.Treeview(self, columns=[c[0] for c in columns],
                                 show="headings", height=height,
                                 selectmode="browse")
        for key, title, width, anchor in columns:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor=anchor, stretch=(width >= 140))
        bar = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")

    def selected(self) -> Optional[str]:
        sel = self.tree.selection()
        return sel[0] if sel else None

    def clear(self) -> None:
        self.tree.delete(*self.tree.get_children())


def hint(master, text: str) -> ttk.Label:
    label = ttk.Label(master, text=text, foreground="#666666", wraplength=760,
                      justify="left")
    return label
