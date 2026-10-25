"""probe 与输出解析：getprop、query-activities（两种形状）、内存、版本。"""

import unittest

from tv_home import operations as ops
from .fake_adb import DANGBEI, FALLBACK, MEMINFO_TEXT, PROPS_TEXT, FakeLink, candidates_text


class ParseTest(unittest.TestCase):
    def test_parse_props(self):
        props = ops.parse_props(PROPS_TEXT)
        self.assertEqual(props["ro.product.model"], "BRAVIA 4K UR2")
        self.assertEqual(props["ro.adb.secure"], "1")

    def test_parse_candidates_sony_shape(self):
        text = candidates_text([
            {"component": DANGBEI, "priority": 0},
            {"component": FALLBACK, "priority": -1000},
        ])
        parsed = ops.parse_candidates(text)
        self.assertEqual([c["component"] for c in parsed], [DANGBEI, FALLBACK])
        self.assertEqual([c["priority"] for c in parsed], [0, -1000])

    def test_parse_candidates_tolerates_missing_priority_and_case(self):
        text = "Priority=2 preferredOrder=0\n  com.google.android.tvlauncher/com.google.android.tvlauncher.HomeActivity\n" \
               "some line without priority\n  com.example.app/com.example.app.HomeActivity\n"
        parsed = ops.parse_candidates(text)
        self.assertEqual(parsed[0]["priority"], 2)
        self.assertIsNone(parsed[1]["priority"])

    def test_single_component_takes_last(self):
        text = "priority=0 …\n  com.a.b/com.a.b.Main\nnoise\n  com.x.y/com.x.y.Home\n"
        self.assertEqual(ops.parse_single_component(text), "com.x.y/com.x.y.Home")
        self.assertIsNone(ops.parse_single_component("No activity found"))


class ProbeTest(unittest.TestCase):
    def setUp(self):
        ops.SLEEP_AFTER_HOME_KEY = 0

    def test_probe_reports_device_and_candidates(self):
        link = FakeLink(candidates=[{"component": DANGBEI, "priority": 0},
                                    {"component": FALLBACK, "priority": -1000}],
                        resolved=DANGBEI, foreground="com.dangbei.TVHomeLauncher")
        res = ops.probe(link, "192.168.1.50")
        self.assertEqual(res["model"], "BRAVIA 4K UR2")
        self.assertEqual(res["android"], "10")
        self.assertEqual(res["serial"], "fc3b17011402353")
        self.assertEqual(res["mem_total_mb"], 2314)
        self.assertEqual(res["home_resolved"], DANGBEI)
        self.assertEqual(res["home_candidates"][0]["priority"], 0)
        self.assertEqual(res["level"], "B")
        self.assertFalse(res["movieclaw"]["installed"])
        self.assertFalse(res["shell"]["installed"])

    def test_probe_reports_installed_versions(self):
        link = FakeLink(candidates=[], resolved=None,
                        installed={ops.MAIN_PKG, ops.SHELL_PKG})
        res = ops.probe(link, "192.168.1.50")
        self.assertEqual(res["movieclaw"]["version"], "0.5.0")
        self.assertTrue(res["shell"]["installed"])


if __name__ == "__main__":
    unittest.main()
