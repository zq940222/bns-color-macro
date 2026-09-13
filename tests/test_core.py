"""Unit tests for the parts that do not need a screen or a game.

Run with:  python -m pytest -q      (or: python tests/test_core.py)
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bnsmacro import keys as K  # noqa: E402
from bnsmacro.capture import Box  # noqa: E402
from bnsmacro.engine import MacroEngine, ProbeReading, color_matches  # noqa: E402
from bnsmacro.profile import (Action, Condition, Probe, Profile, Rule,  # noqa: E402
                              format_color, parse_color)
from bnsmacro.window import Anchor, WindowInfo  # noqa: E402


class TestKeys(unittest.TestCase):
    def test_normalize_is_idempotent(self):
        for name in ("MouseForward", "x2", "小键盘3", "A", "空格", "侧键1"):
            once = K.normalize(name)
            self.assertEqual(once, K.normalize(once), name)

    def test_mouse_aliases_collapse(self):
        self.assertEqual(K.normalize("MouseForward"), "x2")
        self.assertEqual(K.normalize("MiddleClick"), "mousemiddle")
        self.assertEqual(K.normalize("前进键"), "x2")

    def test_chinese_numpad_alias(self):
        self.assertEqual(K.normalize("小键盘7"), "num7")

    def test_arrow_keys_are_not_mouse_buttons(self):
        # "left"/"right" are arrow keys; the mouse needs the mouse* prefix
        self.assertFalse(K.is_mouse("right"))
        self.assertTrue(K.is_mouse("mouseright"))
        self.assertEqual(K.vk_of("right"), 0x27)
        self.assertEqual(K.vk_of("mouseright"), 0x02)

    def test_side_buttons_resolve_to_mouse_vks(self):
        for name in ("x2", "MouseForward", "前进键", "侧键2"):
            self.assertEqual(K.normalize(name), "x2", name)
            self.assertEqual(K.mouse_button(name), "x2", name)
            self.assertEqual(K.vk_of(name), 0x06, name)

    def test_resolve_known_scancodes(self):
        # these are fixed by the PS/2 set-1 layout and must not drift
        self.assertEqual(K.resolve("a")[1], 30)
        self.assertEqual(K.resolve("space")[1], 57)
        self.assertEqual(K.resolve("f1")[1], 59)

    def test_arrows_are_extended(self):
        self.assertTrue(K.resolve("right")[2])
        self.assertFalse(K.resolve("a")[2])

    def test_unknown_key_is_none(self):
        self.assertIsNone(K.resolve("不存在的键"))


class TestColor(unittest.TestCase):
    def test_parse_formats(self):
        self.assertEqual(parse_color("#C83C28"), (0xC8, 0x3C, 0x28))
        self.assertEqual(parse_color("c83c28"), (0xC8, 0x3C, 0x28))
        self.assertEqual(parse_color([200, 60, 40]), (200, 60, 40))
        self.assertEqual(parse_color(0xC83C28), (0xC8, 0x3C, 0x28))

    def test_format_roundtrip(self):
        self.assertEqual(format_color(parse_color("#0A0B0C")), "#0A0B0C")

    def test_bad_colour_raises(self):
        with self.assertRaises(ValueError):
            parse_color("nope")

    def test_channel_metric_uses_worst_channel(self):
        # green is 20 off, so tolerance 19 must fail and 20 must pass
        self.assertFalse(color_matches((100, 120, 100), (100, 100, 100), 19))
        self.assertTrue(color_matches((100, 120, 100), (100, 100, 100), 20))

    def test_distance_metric_is_euclidean(self):
        # (3,4,0) away -> distance 5
        self.assertFalse(color_matches((103, 104, 100), (100, 100, 100), 4, "distance"))
        self.assertTrue(color_matches((103, 104, 100), (100, 100, 100), 5, "distance"))


class TestRuleMatching(unittest.TestCase):
    def _samples(self, **kw):
        return {pid: ProbeReading(pid, (0, 0), rgb) for pid, rgb in kw.items()}

    def test_all_requires_every_condition(self):
        rule = Rule(id="r", match="all", conditions=[
            Condition(probe="a", color="#FF0000", tolerance=5),
            Condition(probe="b", color="#00FF00", tolerance=5),
        ])
        self.assertTrue(MacroEngine._rule_matches(
            rule, self._samples(a=(255, 0, 0), b=(0, 255, 0))))
        self.assertFalse(MacroEngine._rule_matches(
            rule, self._samples(a=(255, 0, 0), b=(0, 0, 255))))

    def test_any_requires_one(self):
        rule = Rule(id="r", match="any", conditions=[
            Condition(probe="a", color="#FF0000", tolerance=5),
            Condition(probe="b", color="#00FF00", tolerance=5),
        ])
        self.assertTrue(MacroEngine._rule_matches(
            rule, self._samples(a=(255, 0, 0), b=(0, 0, 255))))

    def test_negate_inverts(self):
        rule = Rule(id="r", conditions=[
            Condition(probe="a", color="#FF0000", tolerance=5, negate=True)])
        self.assertFalse(MacroEngine._rule_matches(rule, self._samples(a=(255, 0, 0))))
        self.assertTrue(MacroEngine._rule_matches(rule, self._samples(a=(0, 0, 0))))

    def test_missing_probe_never_matches(self):
        rule = Rule(id="r", conditions=[Condition(probe="ghost", color="#000000",
                                                  tolerance=255)])
        self.assertFalse(MacroEngine._rule_matches(rule, self._samples(a=(0, 0, 0))))

    def test_rule_without_conditions_never_fires(self):
        self.assertFalse(MacroEngine._rule_matches(Rule(id="r"), {}))

    def test_priority_order_is_descending(self):
        profile = Profile(rules=[Rule(id="low", priority=1), Rule(id="high", priority=9)])
        self.assertEqual([r.id for r in profile.sorted_rules()], ["high", "low"])


class TestProfile(unittest.TestCase):
    def _sample(self) -> Profile:
        return Profile(
            name="测试",
            probes=[Probe("p1", 10, 20, "备注")],
            rules=[Rule(id="r1", name="规则", priority=5, cooldown_ms=100,
                        conditions=[Condition(probe="p1", color="#112233",
                                              tolerance=7)],
                        actions=[Action(type="key", key="1", hold_ms=25),
                                 Action(type="delay", ms=40)])],
        )

    def test_json_roundtrip_is_lossless(self):
        original = self._sample().to_dict()
        restored = Profile.from_dict(json.loads(json.dumps(original))).to_dict()
        self.assertEqual(original, restored)

    def test_unknown_keys_survive_a_roundtrip(self):
        data = self._sample().to_dict()
        data["some_future_field"] = {"a": 1}
        restored = Profile.from_dict(data).to_dict()
        self.assertEqual(restored["some_future_field"], {"a": 1})

    def test_problems_flags_dangling_probe(self):
        profile = self._sample()
        profile.rules[0].conditions[0].probe = "gone"
        self.assertTrue(any("gone" in p for p in profile.problems()))

    def test_problems_flags_empty_rule(self):
        profile = Profile(rules=[Rule(id="r1", name="空")])
        issues = profile.problems()
        self.assertEqual(len(issues), 2)  # no conditions, no actions

    def test_id_generators_skip_used_ids(self):
        profile = Profile(probes=[Probe("p1", 0, 0), Probe("p2", 0, 0)],
                          rules=[Rule(id="r1")])
        self.assertEqual(profile.next_probe_id(), "p3")
        self.assertEqual(profile.next_rule_id(), "r2")

    def test_save_and_load(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.json"
            self._sample().save(path)
            self.assertEqual(Profile.load(path).to_dict(), self._sample().to_dict())


class TestAnchor(unittest.TestCase):
    def _window(self, left=100, top=50, width=1920, height=1080) -> WindowInfo:
        return WindowInfo(1, "t", "c", Box(left, top, width, height))

    def test_client_anchor_offsets_by_window_origin(self):
        anchor = Anchor("client", reference_size=(1920, 1080))
        anchor.bind(self._window())
        self.assertEqual(anchor.to_screen(10, 20), (110, 70))

    def test_screen_anchor_is_identity(self):
        anchor = Anchor("screen")
        anchor.bind(self._window())
        self.assertEqual(anchor.to_screen(10, 20), (10, 20))

    def test_resolution_scaling(self):
        anchor = Anchor("client", reference_size=(1920, 1080))
        anchor.bind(self._window(0, 0, 960, 540))
        self.assertEqual(anchor.to_screen(1920, 1080), (960, 540))

    def test_to_profile_is_the_inverse_of_to_screen(self):
        anchor = Anchor("client", reference_size=(1920, 1080))
        anchor.bind(self._window(37, 11, 1280, 720))
        for point in ((0, 0), (640, 360), (1919, 1079)):
            screen = anchor.to_screen(*point)
            back = anchor.to_profile(*screen)
            self.assertLessEqual(abs(back[0] - point[0]), 2, point)
            self.assertLessEqual(abs(back[1] - point[1]), 2, point)

    def test_unbound_anchor_passes_through(self):
        anchor = Anchor("client", reference_size=(1920, 1080))
        self.assertEqual(anchor.to_screen(5, 6), (5, 6))


class TestBox(unittest.TestCase):
    def test_bounding_covers_all_points(self):
        box = Box.bounding([(10, 20), (50, 5), (30, 80)], pad=0)
        for x, y in ((10, 20), (50, 5), (30, 80)):
            self.assertTrue(box.contains(x, y), (x, y))

    def test_bounding_is_tight(self):
        box = Box.bounding([(10, 20), (12, 25)], pad=0)
        self.assertEqual((box.left, box.top, box.width, box.height), (10, 20, 3, 6))

    def test_single_point_box_is_one_pixel(self):
        box = Box.bounding([(7, 9)], pad=0)
        self.assertEqual((box.width, box.height), (1, 1))


class TestImporter(unittest.TestCase):
    def test_maps_legacy_settings(self):
        import tempfile
        from bnsmacro.importer import import_legacy_folder
        legacy = {
            "faction_name": "zhao_huan",
            "global_key": {"modifier": "Ctrl", "code": "q"},
            "main_macro": {"checked": True, "hotkey": "MouseForward",
                           "trigger": "Auto"},
            "input_backend": "SendInput",
        }
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "settings.json").write_text(json.dumps(legacy),
                                                  encoding="utf-8")
            (folder / "zhao_huan.json").write_text(
                json.dumps({"按键间隔时间": 5, "colors": [{}, {}, {}]}),
                encoding="utf-8")
            profile, notes = import_legacy_folder(folder)
        self.assertEqual(profile.game_class, "召唤师")
        self.assertEqual(profile.hotkeys["toggle"].key, "q")
        self.assertEqual(profile.hotkeys["toggle"].modifiers, ["ctrl"])
        self.assertEqual(profile.hotkeys["hold"].key, "x2")
        self.assertEqual(profile.hotkeys["hold"].mode, "hold")
        self.assertEqual(profile.settings.key_interval_ms, 5)
        self.assertTrue(any("重新取" in n for n in notes))

    def test_missing_settings_raises(self):
        import tempfile
        from bnsmacro.importer import import_legacy_folder
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                import_legacy_folder(Path(tmp))


if __name__ == "__main__":
    unittest.main(verbosity=2)
