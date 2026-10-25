"""状态记录：第一次的记录不被覆盖，重跑只补新增（设计稿 §8.6）。"""

import unittest

from tv_home.records import merge_record, new_record, rescue_text


def a_snapshot(**overrides):
    snap = {
        "serial": "fc3b17011402353",
        "host": "192.168.1.50",
        "fingerprint": "Sony/…/release-keys",
        "original_home": "com.dangbei.TVHomeLauncher/com.dangbei…MainActivity",
        "packages": {
            "com.dangbei.TVHomeLauncher": {"was_enabled": True},
            "io.movieclaw.androidtv.home": {"was_installed": False},
            "io.movieclaw.androidtv": {"was_installed": True},
        },
    }
    snap.update(overrides)
    return snap


class MergeRecordTest(unittest.TestCase):
    def test_first_time_takes_the_snapshot_whole(self):
        record = merge_record(None, a_snapshot())
        self.assertEqual(record["original_home"], a_snapshot()["original_home"])
        self.assertEqual(record["packages"]["com.dangbei.TVHomeLauncher"], {"was_enabled": True})
        self.assertEqual(record["serial"], "fc3b17011402353")

    def test_existing_entries_are_never_overwritten(self):
        old = merge_record(None, a_snapshot())
        # 半年后重跑：厂商桌面已被停用（was_enabled 仍应记 True）、桌面壳已装上、原桌面字段不同
        again = merge_record(old, a_snapshot(
            original_home="io.movieclaw.androidtv.home/.HomeActivity",
            packages={
                "io.movieclaw.androidtv.home": {"was_installed": True},
                "com.sony.dtv.someextra": {"was_enabled": True},
            }))
        self.assertEqual(again["original_home"], a_snapshot()["original_home"])
        self.assertEqual(again["packages"]["io.movieclaw.androidtv.home"]["was_installed"], False)
        self.assertIn("com.sony.dtv.someextra", again["packages"])
        self.assertEqual(again["packages"]["com.dangbei.TVHomeLauncher"]["was_enabled"], True)

    def test_new_record_is_empty(self):
        record = new_record("s", "h", "f")
        self.assertEqual(record["packages"], {})
        self.assertIsNone(record["original_home"])


class RescueTextTest(unittest.TestCase):
    def test_rescue_restores_home_enables_packages_uninstalls_shell(self):
        record = merge_record(None, a_snapshot())
        record["packages"]["com.google.android.tvlauncher"] = {"was_enabled": True}
        text = rescue_text(record, ["已重新启用 com.google.android.tvlauncher"])
        self.assertIn("cmd package set-home-activity com.dangbei", text)
        self.assertIn("pm enable com.dangbei.TVHomeLauncher", text)
        self.assertIn("pm enable com.google.android.tvlauncher", text)
        self.assertIn("pm uninstall io.movieclaw.androidtv.home", text)
        self.assertIn("# 已重新启用", text)


if __name__ == "__main__":
    unittest.main()
