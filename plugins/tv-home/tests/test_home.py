"""换桌面（§8.4 七步）：优先级 0 只设默认、优先级 2 要停用、可疑候选拒绝、失败回滚。"""

import unittest

from tv_home import operations as ops
from .fake_adb import (DANGBEI, FALLBACK, MAIN_PKG, SHELL_ACTIVITY, SHELL_PKG,
                       SETUPWRAITH, TVLAUNCHER, FakeLink)

APK = b"fake-shell-apk-bytes"


def sony_like(**kw):
    """国行索尼的形状：当贝 priority 0、FallbackHome -1000、MovieClaw 已装。"""
    kw.setdefault("resolved", DANGBEI)
    kw.setdefault("installed", {MAIN_PKG})
    kw.setdefault("foreground", "com.dangbei.TVHomeLauncher")
    return FakeLink(candidates=[{"component": DANGBEI, "priority": 0},
                                {"component": FALLBACK, "priority": -1000},
                                {"component": SHELL_ACTIVITY, "priority": 0}], **kw)


class HomePriorityZeroTest(unittest.TestCase):
    def setUp(self):
        ops.SLEEP_AFTER_HOME_KEY = 0

    def test_no_disable_on_priority_zero(self):
        link = sony_like(foreground_after_home=MAIN_PKG)
        res = ops.run_home(link, None, APK)
        self.assertTrue(res["ok"], res.get("say"))
        self.assertFalse(link.called("pm disable-user"))
        self.assertIsNone(res["disabled"])
        self.assertEqual(res["foreground"], MAIN_PKG)
        # 记录：原桌面是当贝，桌面壳原先没装
        self.assertEqual(res["record"]["original_home"], DANGBEI)
        self.assertFalse(res["record"]["packages"][SHELL_PKG]["was_installed"])
        # 第 3 步的「先单独启动再停掉」与预编译都要发生
        self.assertTrue(link.called("am start -W"))
        self.assertTrue(link.called("am force-stop"))
        self.assertTrue(link.called("cmd package compile -m speed -f " + SHELL_PKG))

    def test_shell_already_home_does_not_record_shell_as_original(self):
        link = sony_like(resolved=SHELL_ACTIVITY, foreground_after_home=MAIN_PKG)
        res = ops.run_home(link, None, APK)
        self.assertTrue(res["ok"])
        self.assertIsNone(res["record"]["original_home"], "原桌面不该记成桌面壳自己")

    def test_install_failure_stops_before_touching_home(self):
        link = sony_like(install_output="Failure [INSTALL_FAILED_INVALID_URI]")
        res = ops.run_home(link, None, APK)
        self.assertFalse(res["ok"])
        self.assertIn("C 级", res["say"])
        self.assertFalse(link.called("cmd package set-home-activity"), "装不上就不该动桌面")
        self.assertFalse(link.called("pm disable-user"))
        self.assertTrue(res["rescue"])

    def test_am_start_failure_is_reported(self):
        link = sony_like(am_start_output="Error: Activity class does not exist.")
        res = ops.run_home(link, None, APK)
        self.assertFalse(res["ok"])
        self.assertIn("Status: ok", res["say"])
        self.assertFalse(link.called("cmd package set-home-activity"))


class HomeDisableTest(unittest.TestCase):
    def setUp(self):
        ops.SLEEP_AFTER_HOME_KEY = 0

    def native_like(self, **kw):
        """原生 Android TV：tvlauncher priority 2（set-home 不生效），必须停用。"""
        kw.setdefault("set_home_effective", False)
        return FakeLink(candidates=[{"component": TVLAUNCHER, "priority": 2},
                                    {"component": FALLBACK, "priority": -1000},
                                    {"component": SHELL_ACTIVITY, "priority": 0}],
                        resolved=TVLAUNCHER, installed={MAIN_PKG},
                        foreground="com.google.android.tvlauncher", **kw)

    def test_disables_vendor_when_priority_higher(self):
        link = self.native_like(foreground_after_home=MAIN_PKG)
        res = ops.run_home(link, None, APK)
        self.assertTrue(res["ok"], res.get("say"))
        self.assertTrue(link.called("pm disable-user --user 0 com.google.android.tvlauncher"))
        self.assertEqual(res["disabled"], TVLAUNCHER)
        self.assertEqual(res["record"]["packages"]["com.google.android.tvlauncher"], {"was_enabled": True})

    def test_refuses_when_another_viable_candidate_remains(self):
        # 设置向导仍启用（priority 1）：停掉 tvlauncher 后它会抢 HOME，不敢动
        link = self.native_like()
        link.candidates.append({"component": SETUPWRAITH, "priority": 1})
        res = ops.run_home(link, None, APK)
        self.assertFalse(res["ok"])
        self.assertFalse(link.called("pm disable-user"), "没有停干净的把握就不该停")
        self.assertTrue(res["rescue"])

    def test_rolls_back_when_verify_after_disable_fails(self):
        link = self.native_like(foreground_after_home="com.google.android.tvlauncher")
        res = ops.run_home(link, None, APK)
        self.assertFalse(res["ok"])
        self.assertTrue(link.called("pm enable com.google.android.tvlauncher"), "验证失败要重新启用")
        self.assertTrue(link.called(f"cmd package set-home-activity {TVLAUNCHER}"),
                        "回滚要把默认桌面设回原来的")
        self.assertTrue(res["rescue"])

    def test_tied_top_candidates_refuse(self):
        link = self.native_like()
        link.candidates.append({"component": SETUPWRAITH, "priority": 2})
        res = ops.run_home(link, None, APK)
        self.assertFalse(res["ok"])
        self.assertFalse(link.called("pm disable-user"))


if __name__ == "__main__":
    unittest.main()
