"""The macro engine.

One background thread owns the whole hot path: resolve the game window,
grab **one** rectangle that covers every probe, sample the pixels out of it,
pick the highest-priority rule whose colour conditions hold, and play that
rule's key sequence.

Why one rectangle: a GDI read off the live screen costs a full display
refresh (~16.7 ms at 60 Hz) *per call*, regardless of how big the rectangle
is.  Ten separate ``GetPixel`` calls would cost 167 ms a tick; one blit of a
box covering all ten costs 16.7 ms.  The loop therefore paces itself to the
monitor's refresh rate, which is also the fastest rate at which new pixels
can possibly exist.
"""
from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import window as win
from .capture import Box, CaptureError, ScreenCapture
from .input import InputError, Sender
from .profile import (METRIC_CHANNEL, MATCH_ANY, Action, Profile, Rule,
                      parse_color)

LOG_DEBUG, LOG_INFO, LOG_WARN, LOG_ERROR = "debug", "info", "warn", "error"


@dataclass
class ProbeReading:
    probe_id: str
    screen: Tuple[int, int]
    rgb: Tuple[int, int, int]
    ok: bool = True


@dataclass
class EngineStatus:
    running: bool = False
    active: bool = False
    fps: float = 0.0
    tick_ms: float = 0.0
    window_title: str = ""
    window_size: Tuple[int, int] = (0, 0)
    foreground_ok: bool = True
    last_rule: str = ""
    last_fire_ago: float = 0.0
    fired_total: int = 0
    readings: List[ProbeReading] = field(default_factory=list)
    error: str = ""


def color_matches(sample: Sequence[int], target: Sequence[int],
                  tolerance: int, metric: str = METRIC_CHANNEL) -> bool:
    dr = int(sample[0]) - int(target[0])
    dg = int(sample[1]) - int(target[1])
    db = int(sample[2]) - int(target[2])
    if metric == METRIC_CHANNEL:
        return max(abs(dr), abs(dg), abs(db)) <= tolerance
    return (dr * dr + dg * dg + db * db) <= tolerance * tolerance


class MacroEngine:
    """Owns the capture/evaluate/fire loop.

    The GUI talks to it through :meth:`set_profile`, :meth:`start`,
    :meth:`stop`, :meth:`set_active` and :meth:`status`.
    """

    def __init__(self, profile: Profile,
                 log: Optional[Callable[[str, str], None]] = None):
        self._profile = profile
        self._log_cb = log
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        # The macro runs only when it is BOTH armed (master switch) and, if a
        # hold key is configured, that key is down.  Two separate flags: an
        # earlier version let the toggle key and the hold key write the same
        # boolean, so releasing the hold key switched the whole macro off.
        self._armed = False
        self._trigger = False
        self._requires_trigger = False
        self._active = threading.Event()
        self._abort_sequence = threading.Event()
        self._sender = Sender()
        self._capture: Optional[ScreenCapture] = None
        self._anchor = win.Anchor()
        self._cooldowns: Dict[str, float] = {}
        self._last_key_time = 0.0
        self._status = EngineStatus()
        self._window_checked_at = 0.0
        self._window: Optional[win.WindowInfo] = None
        self.on_fire: Optional[Callable[[Rule], None]] = None

    # -- logging -----------------------------------------------------------
    def log(self, message: str, level: str = LOG_INFO) -> None:
        if self._log_cb:
            try:
                self._log_cb(message, level)
            except Exception:
                pass

    # -- profile -----------------------------------------------------------
    @property
    def profile(self) -> Profile:
        return self._profile

    def set_profile(self, profile: Profile) -> None:
        with self._lock:
            self._profile = profile
            self._cooldowns.clear()
            self._window_checked_at = 0.0
            if self._capture is not None:
                self._capture.close()
                self._capture = None

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="macro-engine",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.set_active(False)
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        self._thread = None
        if self._capture is not None:
            self._capture.close()
            self._capture = None

    # The master switch.  This is what the toggle hotkey and the big button
    # drive; it survives the hold key going up and down.
    def set_armed(self, armed: bool) -> None:
        with self._lock:
            if self._armed == armed:
                return
            self._armed = armed
        self.log("宏已开启" if armed else "宏已关闭", LOG_INFO)
        self._recompute()

    def toggle(self) -> None:
        self.set_armed(not self._armed)

    # The hold key, if the profile has one.  Never touches _armed.
    def set_trigger(self, held: bool) -> None:
        with self._lock:
            if self._trigger == held:
                return
            self._trigger = held
        self._recompute()

    def set_requires_trigger(self, required: bool) -> None:
        """Tell the engine whether a hold key is bound at all.

        With no hold key, being armed is enough to run.  With one, the macro
        idles until it is pressed.
        """
        with self._lock:
            if self._requires_trigger == required:
                return
            self._requires_trigger = required
            self._trigger = False
        self._recompute()

    def _recompute(self) -> None:
        with self._lock:
            want = self._armed and (self._trigger or not self._requires_trigger)
        if want == self._active.is_set():
            return
        if want:
            self._active.set()
        else:
            self._active.clear()
            self._abort_sequence.set()
            try:
                self._sender.release_all()
            except InputError:
                pass

    def set_active(self, active: bool) -> None:
        """Hard stop / start, used by 急停 and by shutdown."""
        self.set_armed(active)
        if not active:
            self.set_trigger(False)

    @property
    def armed(self) -> bool:
        return self._armed

    @property
    def waiting_for_trigger(self) -> bool:
        return self._armed and self._requires_trigger and not self._trigger

    @property
    def active(self) -> bool:
        return self._active.is_set()

    def status(self) -> EngineStatus:
        with self._lock:
            s = self._status
            return EngineStatus(
                running=bool(self._thread and self._thread.is_alive()),
                active=self._active.is_set(),
                fps=s.fps, tick_ms=s.tick_ms,
                window_title=s.window_title, window_size=s.window_size,
                foreground_ok=s.foreground_ok,
                last_rule=s.last_rule, last_fire_ago=s.last_fire_ago,
                fired_total=s.fired_total,
                readings=list(s.readings), error=s.error,
            )

    # -- window ------------------------------------------------------------
    def _refresh_window(self, force: bool = False) -> Optional[win.WindowInfo]:
        now = time.perf_counter()
        if not force and self._window and now - self._window_checked_at < 0.5:
            # cheap re-read of the rect so a dragged window tracks instantly
            box = win.client_box(self._window.hwnd)
            if box is not None:
                self._window = win.WindowInfo(
                    self._window.hwnd, self._window.title,
                    self._window.class_name, box)
                return self._window
        self._window_checked_at = now
        settings = self._profile.settings
        if settings.anchor_mode == win.ANCHOR_SCREEN:
            self._window = None
            return None
        self._window = win.find_game_window(settings.window_title,
                                            settings.window_class)
        return self._window

    def snapshot_probes(self) -> List[ProbeReading]:
        """Read every probe once, outside the engine loop (used by the UI)."""
        with self._lock:
            profile = self._profile
        window = self._refresh_window(force=True)
        anchor = win.Anchor(profile.settings.anchor_mode,
                            profile.reference_size(),
                            profile.settings.scale_with_resolution)
        anchor.bind(window)
        capture = ScreenCapture(profile.settings.capture_mode,
                                window.hwnd if window else None)
        try:
            return self._read_probes(profile, anchor, capture)
        finally:
            capture.close()

    def _read_probes(self, profile: Profile, anchor: win.Anchor,
                     capture: ScreenCapture) -> List[ProbeReading]:
        if not profile.probes:
            return []
        points = [anchor.to_screen(p.x, p.y) for p in profile.probes]
        box = Box.bounding(points, pad=0)
        frame = capture.grab(box)
        readings: List[ProbeReading] = []
        h, wd = frame.shape[0], frame.shape[1]
        for probe, (sx, sy) in zip(profile.probes, points):
            ix, iy = sx - box.left, sy - box.top
            if 0 <= ix < wd and 0 <= iy < h:
                px = frame[iy, ix]
                readings.append(ProbeReading(probe.id, (sx, sy),
                                             (int(px[0]), int(px[1]), int(px[2]))))
            else:
                readings.append(ProbeReading(probe.id, (sx, sy), (0, 0, 0), ok=False))
        return readings

    # -- rule evaluation ---------------------------------------------------
    @staticmethod
    def _rule_matches(rule: Rule, samples: Dict[str, ProbeReading]) -> bool:
        if not rule.conditions:
            return False
        results = []
        for cond in rule.conditions:
            reading = samples.get(cond.probe)
            if reading is None or not reading.ok:
                results.append(False)
                continue
            try:
                target = parse_color(cond.color)
            except ValueError:
                results.append(False)
                continue
            hit = color_matches(reading.rgb, target, cond.tolerance, cond.metric)
            results.append(not hit if cond.negate else hit)
        return any(results) if rule.match == MATCH_ANY else all(results)

    def _jitter(self, base_ms: float) -> float:
        jitter = self._profile.settings.jitter_ms
        if jitter <= 0:
            return base_ms
        return max(0.0, base_ms + random.uniform(-jitter, jitter))

    def _pace_keys(self) -> None:
        """Keep at least ``key_interval_ms`` between two injected key events."""
        interval = self._jitter(self._profile.settings.key_interval_ms) / 1000.0
        if interval <= 0:
            return
        wait = self._last_key_time + interval - time.perf_counter()
        if wait > 0:
            time.sleep(wait)

    def _play(self, rule: Rule) -> None:
        self._abort_sequence.clear()
        default_hold = self._profile.settings.key_hold_ms
        for action in rule.actions:
            if self._abort_sequence.is_set() or not self._active.is_set():
                return
            try:
                self._run_action(action, default_hold)
            except InputError as exc:
                self.log(f"按键失败: {exc}", LOG_ERROR)
                self.set_active(False)
                return

    def _run_action(self, action: Action, default_hold: int) -> None:
        kind = action.type
        if kind == "delay":
            # wait() rather than sleep() so 急停 cuts a long delay short
            self._abort_sequence.wait(self._jitter(action.ms) / 1000.0)
            return
        if kind == "move":
            self._sender.move_relative(action.dx, action.dy)
            return
        if not action.key:
            return
        self._pace_keys()
        hold = action.hold_ms if action.hold_ms > 0 else default_hold
        if kind == "down":
            self._sender.key_down(action.key)
        elif kind == "up":
            self._sender.key_up(action.key)
        elif action.modifiers:
            self._sender.combo(action.modifiers, action.key, int(self._jitter(hold)))
        else:
            self._sender.tap(action.key, int(self._jitter(hold)))
        self._last_key_time = time.perf_counter()

    # -- main loop ---------------------------------------------------------
    def _run(self) -> None:
        last_frame = time.perf_counter()
        fps_accum, fps_count = 0.0, 0
        last_error = ""
        while not self._stop.is_set():
            if not self._active.is_set():
                self._active.wait(timeout=0.1)
                last_frame = time.perf_counter()
                continue
            tick_start = time.perf_counter()
            try:
                self._tick()
                last_error = ""
            except CaptureError as exc:
                if str(exc) != last_error:
                    last_error = str(exc)
                    self.log(f"取色失败: {exc}", LOG_ERROR)
                with self._lock:
                    self._status.error = last_error
                time.sleep(0.25)
            except Exception as exc:  # keep the thread alive no matter what
                if str(exc) != last_error:
                    last_error = str(exc)
                    self.log(f"引擎异常: {exc}", LOG_ERROR)
                time.sleep(0.25)

            now = time.perf_counter()
            elapsed_ms = (now - tick_start) * 1000.0
            fps_accum += now - last_frame
            fps_count += 1
            last_frame = now
            if fps_count >= 15:
                with self._lock:
                    self._status.fps = fps_count / fps_accum if fps_accum else 0.0
                    self._status.tick_ms = elapsed_ms
                fps_accum, fps_count = 0.0, 0

            # a screen grab already blocks for one display refresh, so this
            # only sleeps when the configured tick is deliberately slower
            budget = self._profile.settings.tick_ms / 1000.0
            remaining = budget - (time.perf_counter() - tick_start)
            if remaining > 0.001:
                time.sleep(remaining)

        try:
            self._sender.release_all()
        except InputError:
            pass

    def _tick(self) -> None:
        with self._lock:
            profile = self._profile
        settings = profile.settings

        window = self._refresh_window()
        self._anchor.mode = settings.anchor_mode
        self._anchor.reference_size = profile.reference_size()
        self._anchor.scale_with_resolution = settings.scale_with_resolution
        self._anchor.bind(window)

        foreground_ok = True
        if settings.require_foreground and window is not None:
            foreground_ok = win.foreground_hwnd() == window.hwnd

        with self._lock:
            self._status.window_title = window.title if window else "(未绑定窗口)"
            self._status.window_size = window.size if window else (0, 0)
            self._status.foreground_ok = foreground_ok

        if settings.anchor_mode != win.ANCHOR_SCREEN and window is None:
            raise CaptureError("找不到游戏窗口 -- 请在「设置」里指定窗口标题")

        if self._capture is None:
            self._capture = ScreenCapture(settings.capture_mode,
                                          window.hwnd if window else None)
        elif window is not None and self._capture.hwnd != window.hwnd:
            self._capture.hwnd = window.hwnd

        readings = self._read_probes(profile, self._anchor, self._capture)
        samples = {r.probe_id: r for r in readings}
        with self._lock:
            self._status.readings = readings
            self._status.error = ""

        if not foreground_ok:
            return

        now = time.perf_counter()
        for rule in profile.sorted_rules():
            if not rule.enabled:
                continue
            if now < self._cooldowns.get(rule.id, 0.0):
                continue
            if not self._rule_matches(rule, samples):
                continue
            self._cooldowns[rule.id] = now + rule.cooldown_ms / 1000.0
            with self._lock:
                self._status.last_rule = rule.name or rule.id
                self._status.fired_total += 1
            if self.on_fire:
                try:
                    self.on_fire(rule)
                except Exception:
                    pass
            self._play(rule)
            return  # one rule per tick keeps priorities meaningful
