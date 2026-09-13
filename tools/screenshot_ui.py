"""Regenerate the README screenshots.

    pip install pillow
    python tools/screenshot_ui.py

Opens the real window, seeds it with a demo profile, walks every tab and
captures each one with the project's own capture code.  Writes to docs/img/.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from bnsmacro.capture import Box, ScreenCapture  # noqa: E402
from bnsmacro.profile import Action, Condition, Probe, Rule  # noqa: E402
from bnsmacro.ui.app import App  # noqa: E402
from bnsmacro.winapi import (begin_high_resolution_timer,  # noqa: E402
                             enable_dpi_awareness, end_high_resolution_timer)

TABS = ["run", "probes", "rules", "settings", "help"]


def demo_profile(app: App) -> None:
    p = app.profile
    p.name = "演示"
    p.settings.anchor_mode = "screen"
    p.settings.require_foreground = False
    p.probes = [Probe("p1", 300, 300, "技能1 图标"),
                Probe("p2", 340, 300, "技能2 图标"),
                Probe("p3", 380, 300, "buff 格")]
    p.rules = [
        Rule(id="r1", name="枫叶飘", priority=100, cooldown_ms=300,
             conditions=[Condition(probe="p1", color="#C83C28", tolerance=25)],
             actions=[Action(type="key", key="1", hold_ms=30),
                      Action(type="delay", ms=40),
                      Action(type="key", key="f", hold_ms=30)]),
        Rule(id="r2", name="狗尾草", priority=50,
             conditions=[Condition(probe="p2", color="#3C7ACD", tolerance=20)],
             actions=[Action(type="key", key="2")]),
        Rule(id="r3", name="低内不右键", priority=10, enabled=False,
             conditions=[Condition(probe="p3", color="#202020", tolerance=15,
                                   negate=True)],
             actions=[Action(type="key", key="mouseright")]),
    ]
    app.engine.set_profile(p)
    app._apply_profile_to_ui()


def main() -> int:
    enable_dpi_awareness()
    begin_high_resolution_timer()
    outdir = ROOT / "docs" / "img"
    outdir.mkdir(parents=True, exist_ok=True)

    app = App(ROOT / "profiles")
    demo_profile(app)
    capture = ScreenCapture()
    state = {"i": 0}

    def shoot() -> None:
        i = state["i"]
        if i >= len(TABS):
            app._dirty = False
            app._on_close()
            return
        app.nb.select(i)
        app.update_idletasks()
        app.update()
        time.sleep(0.35)
        app.update()
        box = Box(app.winfo_rootx() - 1, app.winfo_rooty() - 1,
                  app.winfo_width() + 2, app.winfo_height() + 2)
        Image.fromarray(capture.grab(box)).save(outdir / f"{TABS[i]}.png")
        print("saved", TABS[i])
        state["i"] += 1
        app.after(120, shoot)

    app.after(700, shoot)
    try:
        app.mainloop()
    finally:
        capture.close()
        end_high_resolution_timer()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
