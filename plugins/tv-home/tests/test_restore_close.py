"""安装、还原顺序（§8.6：先启用、设回桌面，再卸桌面壳）与 close（只认连接被拒绝）。"""

import unittest

from tv_home import operations as ops
from .fake_adb import (DANGBEI, MAIN_PKG, SHELL_ACTIVITY, SHELL_PKG, TVLAUNCHER,
                       FakeLink)
from .test_records import a_snapshot


class InstallTest(unittest.TestCase):
    def setUp(self):
        ops.SLEEP_AFTER_HOME_KEY = 0

    def test_install_main_and_shell_push_and_compile(self):
        link = FakeLink(candidates=[{"component": DANGBEI, "priority": 0}], resolved=DANGBEI)
        ops.install_main(link, b"main-apk-bytes")
        ops.install_shell(link, b"shell-apk-bytes")
        pushed = {path: data for _, path, data in link.pushed}
        self.assertEqual(pushed["/data/local/tmp/movieclaw-app.apk"], b"main-apk-bytes")
        self.assertEqual(pushed["/data/local/tmp/movieclaw-home.apk"], b"shell-apk-bytes")
        self.assertTrue(link.called("pm install -r /data/local/tmp/movieclaw-app.apk"))
        self.assertTrue(link.called("cmd package compile -m speed-profile -f " + MAIN_PKG))
        self.assertTrue(link.called("cmd package compile -m speed -f " + SHELL_PKG))

    def test_install_without_success_raises(self):
        link = FakeLink(install_output="Failure [INSTALL_PARSE_FAILED_NO_CERTIFICATES]")
        with self.assertRaises(RuntimeError) as ctx:
            ops.install_shell(link, b"apk")
        self.assertIn("Success", str(ctx.exception))

    def test_record_keeps_pre_install_state(self):
        """install 先快照再安装：记录里「原先是否已装」不被安装动作污染。"""
        link = FakeLink(candidates=[{"component": DANGBEI, "priority": 0}], resolved=DANGBEI)
        snap = ops.snapshot(link, link.host)
        ops.install_shell(link, b"apk")
        record = ops.merge_record(None, snap)
        self.assertFalse(record["packages"][SHELL_PKG]["was_installed"])


class RestoreTest(unittest.TestCase):
    def setUp(self):
        ops.SLEEP_AFTER_HOME_KEY = 0

    def test_restore_order_enable_before_uninstall(self):
        link = FakeLink(candidates=[], resolved=SHELL_ACTIVITY,
                        installed={SHELL_PKG, MAIN_PKG},
                        disabled={"com.google.android.tvlauncher"})
        record = ops.merge_record(None, a_snapshot(
            original_home=TVLAUNCHER,
            packages={"com.google.android.tvlauncher": {"was_enabled": True},
                      SHELL_PKG: {"was_installed": False},
                      MAIN_PKG: {"was_installed": True}}))
        res = ops.run_restore(link, record)
        self.assertTrue(res["ok"], res.get("say"))
        enable_at = link.shell_calls.index("pm enable com.google.android.tvlauncher")
        uninstall_at = link.calls_of("pm uninstall " + SHELL_PKG)[0]
        index_uninstall = link.shell_calls.index(uninstall_at)
        self.assertLess(enable_at, index_uninstall, "顺序不能反：先启用厂商桌面再卸桌面壳")
        self.assertTrue(link.called(f"cmd package set-home-activity {TVLAUNCHER}"))
        self.assertNotIn(SHELL_PKG, link.installed)
        self.assertIn(MAIN_PKG, link.installed, "MovieClaw 默认保留")

    def test_restore_uninstall_main_only_when_asked(self):
        link = FakeLink(candidates=[], resolved=TVLAUNCHER, installed={SHELL_PKG, MAIN_PKG})
        record = ops.merge_record(None, a_snapshot(original_home=TVLAUNCHER))
        res = ops.run_restore(link, record, uninstall_main=True)
        self.assertTrue(res["ok"], res.get("say"))
        self.assertNotIn(MAIN_PKG, link.installed)

    def test_restore_without_record_refuses_to_touch_device(self):
        link = FakeLink()
        res = ops.run_restore(link, None)
        self.assertFalse(res["ok"])
        self.assertEqual(link.shell_calls, [], "没有记录就不该碰设备")


class CloseTest(unittest.TestCase):
    """check_port_closed 的判据：只认「连接被拒绝」。OS 行为用桩替，真机验收再实测。"""

    @staticmethod
    def _with_connect(exc):
        import socket as socket_mod
        from unittest import mock

        s = mock.MagicMock()
        s.connect.side_effect = exc
        return mock.patch.object(socket_mod, "socket", mock.MagicMock(return_value=s))

    def test_connection_refused_counts_as_closed(self):
        with self._with_connect(ConnectionRefusedError()):
            self.assertTrue(ops.check_port_closed("192.168.1.50", 5555))

    def test_timeout_does_not_count_as_closed(self):
        with self._with_connect(TimeoutError()):
            self.assertFalse(ops.check_port_closed("192.168.1.50", 5555))

    def test_open_port_does_not_count_as_closed(self):
        with self._with_connect(None):
            self.assertFalse(ops.check_port_closed("192.168.1.50", 5555))


if __name__ == "__main__":
    unittest.main()
