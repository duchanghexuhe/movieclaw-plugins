"""aerial 配置注入：命令构造与广播执行（设计稿 §13.2）。"""

import unittest

from tv_home import operations as ops
from .fake_adb import FakeLink


class ConfigureAerialTest(unittest.TestCase):
    def test_command_carries_screensaver_mode(self):
        cmd = ops.configure_aerial_command()
        self.assertIn("am broadcast -a app.orchard.tv.CONFIG", cmd)
        self.assertIn("-n app.orchard.tv/.guard.ConfigReceiver", cmd)
        self.assertIn("--es screensaver aerial", cmd)

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            ops.configure_aerial_command("netflix")

    def test_broadcast_runs_over_link_and_surfaces_errors(self):
        link = FakeLink()
        calls = []
        link.shell = lambda command, timeout_s=60.0: calls.append(command) or ""
        ops.configure_aerial(link)
        self.assertEqual(calls, [ops.configure_aerial_command()])
        link.shell = lambda command, timeout_s=60.0: "Error: SecurityException"
        with self.assertRaises(RuntimeError):
            ops.configure_aerial(link)


if __name__ == "__main__":
    unittest.main()
