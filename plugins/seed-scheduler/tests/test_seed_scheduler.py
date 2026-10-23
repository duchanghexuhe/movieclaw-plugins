"""只测纯函数：不启动宿主，不发网络请求。

做种时长的口径、状态归类、以及「删不删」的判断都在模块顶层的纯函数里。这几处一错
就会误删种子、或者该删的永远不删，所以钉在这里；接口返回的脏数据兜底也一并测。
"""

import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402 -- 先把桩模块装好，下面才能导入
from seed_scheduler import (  # noqa: E402
    _url_host,
    empty_relations,
    file_ref,
    has_hr_tag,
    has_keep_tag,
    hash_key,
    ingest_entry,
    items_of,
    ledger_add,
    ledger_trim,
    library_verdict,
    num,
    open_until,
    parse_time,
    recently_opened,
    relations_from_ledger,
    removal_blocker,
    seeded_hours,
    should_remind,
    state_group,
    uniq,
    uniq_units,
)

NOW = 1_800_000_000.0


class FakeConfig:
    """只带纯函数用到的开关，避免为了跑单测去构造整个设置模型。"""

    def __init__(self, **overrides):
        self.seed_hours = 72.0
        self.overdue_days = 3.0
        self.hr_min_hours = 72.0
        self.respect_hr = True
        self.respect_boost = True
        self.library_required = False
        self.keep_waiting_subscriptions = True
        self.protect_unwatched = False
        self.handle_seeding = True
        self.handle_missing_files = False
        self.handle_downloading = False
        self.handle_errored = False
        self.handle_checking = False
        self.handle_unknown = False
        for key, value in overrides.items():
            setattr(self, key, value)


class ItemsOfTest(unittest.TestCase):
    def test_wrapped_and_plain(self):
        self.assertEqual(items_of({"items": [1, 2]}), [1, 2])
        self.assertEqual(items_of([3]), [3])

    def test_other_payloads(self):
        self.assertEqual(items_of(None), [])
        self.assertEqual(items_of("x"), [])
        self.assertEqual(items_of({"items": None}), [])


class NumTest(unittest.TestCase):
    def test_numbers(self):
        self.assertEqual(num("3.5"), 3.5)
        self.assertEqual(num(7), 7.0)

    def test_none_bool_and_garbage_fall_back(self):
        self.assertEqual(num(None), 0.0)
        self.assertEqual(num(True), 0.0)
        self.assertEqual(num("x"), 0.0)
        self.assertEqual(num("x", 9.0), 9.0)


class ParseTimeTest(unittest.TestCase):
    def test_zulu(self):
        moment = parse_time("2026-10-20T10:00:00Z")
        self.assertEqual(moment, datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc))

    def test_naive_treated_as_utc(self):
        moment = parse_time("2026-10-20 10:00:00")
        self.assertEqual(moment.tzinfo, timezone.utc)
        self.assertEqual(moment.hour, 10)

    def test_datetime_passthrough(self):
        naive = datetime(2026, 10, 20, 10, 0)
        self.assertEqual(parse_time(naive).tzinfo, timezone.utc)

    def test_unparsable(self):
        self.assertIsNone(parse_time(""))
        self.assertIsNone(parse_time(None))
        self.assertIsNone(parse_time("not-a-time"))


class HashKeyTest(unittest.TestCase):
    def test_lowercased_and_trimmed(self):
        self.assertEqual(hash_key(" ABCdef "), "abcdef")

    def test_missing(self):
        self.assertEqual(hash_key(None), "")


class UrlHostTest(unittest.TestCase):
    def test_strips_scheme_path_and_userinfo(self):
        self.assertEqual(_url_host("http://10.0.0.2:8080/qb"), "10.0.0.2:8080")
        self.assertEqual(_url_host("https://me:pw@host.lan:8443/"), "host.lan:8443")

    def test_default_ports(self):
        self.assertEqual(_url_host("10.0.0.2"), "10.0.0.2:80")
        self.assertEqual(_url_host("https://example.com"), "example.com:443")

    def test_empty(self):
        self.assertEqual(_url_host(""), "")
        self.assertEqual(_url_host(None), "")

    def test_two_urls_of_same_host_match(self):
        # 多下载器匹配就是靠这个：路径、协议、末尾斜杠不同也得算同一台
        self.assertEqual(
            _url_host("http://10.0.0.2:8080/"), _url_host("10.0.0.2:8080/qb")
        )


class KeepTagTest(unittest.TestCase):
    def test_known_tags(self):
        self.assertTrue(has_keep_tag(["免清理"]))
        self.assertTrue(has_keep_tag(["noreap"]))
        self.assertTrue(has_keep_tag(["别的", "SEED-KEEP"]))

    def test_absent(self):
        self.assertFalse(has_keep_tag(["普通"]))
        self.assertFalse(has_keep_tag([]))
        self.assertFalse(has_keep_tag(None))


class HrTagTest(unittest.TestCase):
    def test_known_tags(self):
        self.assertTrue(has_hr_tag(["H&R"]))
        self.assertTrue(has_hr_tag(["hit&run"]))
        self.assertTrue(has_hr_tag(["考核"]))
        self.assertTrue(has_hr_tag(["别的", "HR"]))

    def test_absent(self):
        # 「hr」以外的普通标签不能被当成 H&R，否则种子永远删不掉
        self.assertFalse(has_hr_tag(["普通"]))
        self.assertFalse(has_hr_tag(["hr-something"]))
        self.assertFalse(has_hr_tag([]))
        self.assertFalse(has_hr_tag(None))


class StateGroupTest(unittest.TestCase):
    def test_known_states(self):
        self.assertEqual(state_group({"state": "uploading"}), "seeding")
        self.assertEqual(state_group({"state": "pausedUP"}), "seeding")
        self.assertEqual(state_group({"state": "downloading"}), "downloading")
        self.assertEqual(state_group({"state": "missingFiles"}), "missing")
        self.assertEqual(state_group({"state": "error"}), "errored")
        self.assertEqual(state_group({"state": "checkingUP"}), "checking")

    def test_unknown_state_falls_back_to_progress(self):
        self.assertEqual(state_group({"state": "", "completed": True}), "seeding")
        self.assertEqual(state_group({"state": "", "completed": False}), "downloading")

    def test_unknown_state_without_progress_is_other(self):
        self.assertEqual(state_group({"state": "weird"}), "other")


class SeededHoursTest(unittest.TestCase):
    def test_uses_qbittorrent_seeding_seconds(self):
        self.assertEqual(seeded_hours({"seeding_seconds": 7200}, 0, NOW), 2.0)

    def test_uses_longest_signal(self):
        brief = {"seeding_seconds": 7200, "completed_on": NOW - 36000}
        self.assertEqual(seeded_hours(brief, 0, NOW), 10.0)

    def test_added_on_is_ignored_once_completed(self):
        # 下了三天才下完、刚做种一小时：不能因为 added_on 早就算成做了三天
        brief = {"seeding_seconds": 3600, "added_on": NOW - 3 * 86400}
        self.assertEqual(seeded_hours(brief, 0, NOW), 1.0)

    def test_added_on_counts_when_no_completion(self):
        self.assertEqual(seeded_hours({"added_on": NOW - 1800}, 0, NOW), 0.5)

    def test_falls_back_to_first_seen(self):
        self.assertEqual(seeded_hours({}, NOW - 7200, NOW), 2.0)

    def test_no_information_at_all(self):
        self.assertEqual(seeded_hours({}, 0, NOW), 0.0)


class RemovalBlockerTest(unittest.TestCase):
    """决定「删不删」的第一道关：只看种子自己的事实。"""

    def blocked(self, brief=None, facts=None, **config):
        return removal_blocker(
            brief or {},
            facts or {},
            FakeConfig(**config),
            first_seen=0,
            now=NOW,
        )

    def test_clean_seeding_torrent_is_not_blocked(self):
        brief = {"seeding_seconds": 100 * 3600}
        self.assertIsNone(self.blocked(brief, {"group": "seeding"}))

    def test_keep_tag_always_wins(self):
        brief = {"tags": ["免清理"], "seeding_seconds": 100 * 3600}
        self.assertIn("免清理", self.blocked(brief, {"group": "seeding"}))

    def test_group_not_enabled(self):
        # 文件已丢失默认不处理：挂载一抖全部种子都会变成 missingFiles
        reason = self.blocked({"state": "missingFiles"}, {"group": "missing"})
        self.assertIn("文件已丢失", reason)
        self.assertIsNone(
            self.blocked({"state": "missingFiles"}, {"group": "missing"}, handle_missing_files=True)
        )

    def test_boost(self):
        reason = self.blocked({}, {"group": "seeding", "boost": True})
        self.assertIn("刷流", reason)
        self.assertIsNone(
            self.blocked({}, {"group": "seeding", "boost": True}, respect_boost=False)
        )

    def test_watching(self):
        reason = self.blocked({}, {"group": "seeding", "watching": True})
        self.assertIn("正在播放", reason)

    def test_hr_blocks_until_minimum_is_reached(self):
        short = {"seeding_seconds": 10 * 3600}
        reason = self.blocked(short, {"group": "seeding", "hr": True})
        self.assertIn("H&R", reason)
        self.assertIn("72", reason)

    def test_hr_releases_after_minimum(self):
        long_enough = {"seeding_seconds": 100 * 3600}
        self.assertIsNone(self.blocked(long_enough, {"group": "seeding", "hr": True}))

    def test_hr_can_be_switched_off(self):
        short = {"seeding_seconds": 10 * 3600}
        self.assertIsNone(
            self.blocked(short, {"group": "seeding", "hr": True}, respect_hr=False)
        )

    def test_hr_min_hours_follows_config(self):
        brief = {"seeding_seconds": 100 * 3600}
        self.assertIsNotNone(
            self.blocked(brief, {"group": "seeding", "hr": True}, hr_min_hours=200)
        )


class LibraryVerdictTest(unittest.TestCase):
    """第二道关：媒体库这一侧。默认一点不拦，只拦「删了会出真事」的两种。"""

    def verdict(self, relations=None, **config):
        return library_verdict(relations or empty_relations(), FakeConfig(**config))

    def test_default_is_permissive(self):
        self.assertEqual(self.verdict(), (None, False))

    def test_waiting_subscription_blocks(self):
        reason, in_library = self.verdict(
            {"waiting_units": [[1, 2]], "file_ids": [7]}
        )
        self.assertIn("订阅还在等", reason)
        self.assertTrue(in_library)

    def test_waiting_can_be_switched_off(self):
        self.assertEqual(
            self.verdict({"waiting_units": [[1, 2]]}, keep_waiting_subscriptions=False),
            (None, False),
        )

    def test_other_entries_block(self):
        reason, in_library = self.verdict({"other_file_ids": [9]})
        self.assertIn("别的条目", reason)
        self.assertTrue(in_library)

    def test_library_required_blocks_unless_ingested(self):
        self.assertEqual(
            self.verdict(library_required=True)[0], "媒体库还没有这份文件"
        )
        self.assertEqual(
            self.verdict({"file_ids": [7]}, library_required=True), (None, True)
        )

    def test_protect_unwatched_blocks_only_guarded_units(self):
        guarded = {"guarded_units": [[1, 1]], "file_ids": [7]}
        self.assertIn("还没看完", self.verdict(dict(guarded), protect_unwatched=True)[0])
        self.assertEqual(self.verdict({"file_ids": [7]}, protect_unwatched=True), (None, True))


class ShouldRemindTest(unittest.TestCase):
    def test_without_first_seen_never_reminds(self):
        self.assertFalse(
            should_remind({"completed": True}, 0, NOW, FakeConfig())
        )

    def test_grace_uses_days_for_overdue_days(self):
        # seed_hours=72、overdue_days=3 → 72 + 72 = 144 小时。
        # 原来把天当小时算，144 小时的兜底只剩 75 小时。
        config = FakeConfig(seed_hours=72, overdue_days=3)
        self.assertFalse(
            should_remind({"completed": True}, NOW - 100 * 3600, NOW, config)
        )
        self.assertTrue(
            should_remind({"completed": True}, NOW - 145 * 3600, NOW, config)
        )

    def test_unfinished_torrents_are_not_reminded(self):
        self.assertFalse(
            should_remind({"completed": False}, NOW - 1000 * 3600, NOW, FakeConfig())
        )


class OpenUntilTest(unittest.TestCase):
    def test_new_format_uses_until(self):
        self.assertEqual(open_until({"at": NOW, "until": NOW + 60}), NOW + 60)

    def test_new_format_without_until_falls_back_to_window(self):
        self.assertEqual(open_until({"at": NOW, "until": 0}), NOW + 240 * 60)

    def test_legacy_timestamp(self):
        self.assertEqual(open_until(NOW), NOW + 240 * 60)

    def test_missing(self):
        self.assertEqual(open_until(None), 0.0)
        self.assertEqual(open_until({}), 0.0)
        self.assertEqual(open_until(0), 0.0)

    def test_long_film_stays_protected(self):
        # 一部三小时的片：播放开始后按片长把窗口推到片尾之后
        opened = {"5": {"at": NOW, "until": NOW + 3 * 3600}}
        self.assertTrue(recently_opened(opened, 5, NOW + 2.5 * 3600))


class UniqTest(unittest.TestCase):
    def test_uniq(self):
        self.assertEqual(uniq([1, 1, 2, 1]), [1, 2])

    def test_uniq_units_skips_malformed(self):
        self.assertEqual(uniq_units([["1", "2"], [1, 2], [None, "x"]]), [[1, 2]])


class RecentlyOpenedTest(unittest.TestCase):
    def test_recent_visit(self):
        self.assertTrue(recently_opened({"5": NOW - 60}, 5, NOW))

    def test_old_visit(self):
        # 4 小时的窗口：两小时前打开过还在保护里（原来 30 分钟，两小时的片后半段就没保护了）
        self.assertTrue(recently_opened({"5": NOW - 7200}, 5, NOW))
        self.assertFalse(recently_opened({"5": NOW - 5 * 3600}, 5, NOW))

    def test_missing_media(self):
        self.assertFalse(recently_opened({}, None, NOW))


class FileRefTest(unittest.TestCase):
    def test_accepts_every_id_spelling(self):
        for key in ("id", "file_id", "media_file_id"):
            self.assertEqual(file_ref({key: 12})["file_id"], 12)

    def test_rejects_unusable(self):
        # 认不出 id 就扔掉：宁可台账少一条，也不要记一条永远对不上的假账
        self.assertEqual(file_ref({}), {})
        self.assertEqual(file_ref({"id": 0}), {})
        self.assertEqual(file_ref({"id": -3}), {})
        self.assertEqual(file_ref({"id": "x"}), {})
        self.assertEqual(file_ref(None), {})

    def test_fields(self):
        ref = file_ref(
            {
                "id": "7",
                "media_item_id": "31",
                "season": "2",
                "episode": 5,
                "units": [[2, 5], [2, 6]],
                "status": "imported",
            }
        )
        self.assertEqual(ref["media_item_id"], 31)
        self.assertEqual((ref["season"], ref["episode"]), (2, 5))
        self.assertEqual(ref["unit_count"], 2)
        self.assertFalse(ref["gone"])

    def test_missing_media_item_id_is_none(self):
        self.assertIsNone(file_ref({"id": 7})["media_item_id"])

    def test_gone_states(self):
        self.assertTrue(file_ref({"id": 7, "status": "trashed"})["gone"])
        self.assertTrue(file_ref({"id": 7, "state": "purged"})["gone"])
        self.assertFalse(file_ref({"id": 7, "status": "imported"})["gone"])


class IngestEntryTest(unittest.TestCase):
    def test_empty_without_usable_files(self):
        self.assertEqual(ingest_entry([], 3, NOW), {})
        self.assertEqual(ingest_entry([{"x": 1}], 3, NOW), {})

    def test_dedupes_by_file_id(self):
        entry = ingest_entry([{"id": 7}, {"id": 7, "episode": 9}], 3, NOW)
        self.assertEqual(len(entry["files"]), 1)

    def test_picks_owner_and_units(self):
        entry = ingest_entry(
            [
                {"id": 7, "media_item_id": 31, "season": 1, "episode": 1},
                {"id": 8, "media_item_id": 31, "season": 1, "episode": 2},
            ],
            3,
            NOW,
        )
        self.assertEqual(entry["library_id"], 3)
        self.assertEqual(entry["media_item_id"], 31)
        self.assertEqual(entry["units"], [[1, 1], [1, 2]])
        self.assertEqual(entry["at"], int(NOW))


class LedgerTest(unittest.TestCase):
    def test_add_merges_files_of_same_torrent(self):
        ledger = ledger_add(
            {}, "AA", ingest_entry([{"id": 7, "media_item_id": 31, "season": 1, "episode": 1}], 3, NOW)
        )
        ledger = ledger_add(
            ledger, "aa", ingest_entry([{"id": 8, "media_item_id": 31, "season": 1, "episode": 2}], 3, NOW + 10)
        )
        self.assertEqual(list(ledger), ["aa"])
        self.assertEqual([ref["file_id"] for ref in ledger["aa"]["files"]], [7, 8])
        self.assertEqual(ledger["aa"]["units"], [[1, 1], [1, 2]])
        self.assertEqual(ledger["aa"]["at"], int(NOW + 10))

    def test_add_keeps_unknown_values(self):
        ledger = {"aa": {"files": [], "library_id": 3, "media_item_id": 31, "units": [], "at": 5}}
        same = ledger_add(ledger, "aa", {"files": [], "units": [], "at": 0})
        self.assertEqual(same["aa"]["library_id"], 3)
        self.assertEqual(same["aa"]["media_item_id"], 31)
        self.assertEqual(same["aa"]["at"], 5)

    def test_add_ignores_empty_entry_and_empty_hash(self):
        self.assertEqual(ledger_add({}, "", {}), {})
        self.assertEqual(ledger_add({"aa": {"at": 1}}, "aa", {}), {"aa": {"at": 1}})

    def test_trim_keeps_newest(self):
        ledger = {str(index): {"at": index} for index in range(10)}
        trimmed = ledger_trim(ledger, limit=3)
        self.assertEqual(sorted(trimmed), ["7", "8", "9"])


class RelationsFromLedgerTest(unittest.TestCase):
    def test_empty_ledger_entry(self):
        self.assertEqual(relations_from_ledger({}), empty_relations())
        self.assertEqual(relations_from_ledger(None), empty_relations())

    def test_splits_own_and_others(self):
        entry = {
            "media_item_id": 31,
            "files": [
                {"file_id": 7, "media_item_id": 31, "status": "imported"},
                {"file_id": 8, "media_item_id": 99, "status": "imported"},
            ],
        }
        relations = relations_from_ledger(entry)
        self.assertEqual(relations["file_ids"], [7])
        self.assertEqual(relations["other_file_ids"], [8])

    def test_skips_gone_files(self):
        entry = {
            "media_item_id": 31,
            "files": [{"file_id": 7, "media_item_id": 31, "status": "trashed"}],
        }
        relations = relations_from_ledger(entry)
        self.assertEqual(relations["file_ids"], [])
        self.assertEqual(relations["guarded_units"], [])

    def test_guards_only_imported_units(self):
        entry = {
            "media_item_id": 31,
            "files": [
                {"file_id": 7, "media_item_id": 31, "season": 1, "episode": 1, "status": "imported"},
                {"file_id": 8, "media_item_id": 31, "season": 1, "episode": 2, "status": "downloading"},
            ],
        }
        self.assertEqual(relations_from_ledger(entry)["guarded_units"], [[1, 1]])

    def test_single_item_torrent_without_owner(self):
        # 台账里没记 media_item_id 时不做「别的条目」判断，文件都算自己的
        entry = {"files": [{"file_id": 7, "media_item_id": 99, "status": "imported"}]}
        relations = relations_from_ledger(entry)
        self.assertEqual(relations["file_ids"], [7])
        self.assertEqual(relations["other_file_ids"], [])


if __name__ == "__main__":
    unittest.main()