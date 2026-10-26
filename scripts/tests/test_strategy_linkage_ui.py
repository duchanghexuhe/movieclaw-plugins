import unittest
from pathlib import Path


class StrategyLinkageUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[2] / "plugins" / "strategy-linkage"
        cls.html = (root / "web" / "index.html").read_text(encoding="utf-8")

    def test_site_picker_uses_individual_checkboxes(self):
        self.assertIn('id="siteChoices"', self.html)
        self.assertNotIn('id="allSites"', self.html)
        self.assertNotIn('id="siteSelect"', self.html)

    def test_empty_state_has_clickable_create_rule_action(self):
        self.assertIn('id="emptyNewRuleBtn"', self.html)
        self.assertIn("$('emptyNewRuleBtn').onclick=()=>openRule()", self.html)

    def test_new_rules_do_not_preselect_ingest_event(self):
        self.assertIn("fillChoices('eventChoices',EVENT_OPTIONS,rule?.required_events||[])", self.html)
        self.assertIn("fillChoices('eventChoices',EVENT_OPTIONS,[])", self.html)
        self.assertNotIn("rule?.required_events||['library.ingest.completed']", self.html)

    def test_ingest_event_and_elapsed_time_are_explained_separately(self):
        self.assertIn('整理入库成功（事件）', self.html)
        self.assertIn('整理入库成功后（计时起点）', self.html)

    def test_simulation_and_diagnostics_share_one_mode_aware_button(self):
        self.assertNotIn('id="previewBtn"', self.html)
        self.assertIn('id="runBtn" type="button">模拟运行</button>', self.html)
        self.assertIn("dry?'模拟运行':'执行规则'", self.html)
        self.assertIn("api('/preview'", self.html)
        self.assertIn("api('/run'", self.html)
        self.assertIn('id="previewResults"', self.html)
        self.assertIn('逐条规则诊断', self.html)
        self.assertIn('事件采集概览', self.html)
        self.assertIn('AbortController', self.html)


    def test_site_scope_is_optional_and_unchecked_means_unrestricted(self):
        self.assertIn('PT 站点（可选条件；不勾选 = 不限制站点）', self.html)
        self.assertIn("all_sites:siteIds.length===0,site_ids:siteIds", self.html)
        self.assertNotIn("if(!siteIds.length)throw new Error('请至少勾选一个已配置的 PT 站点')", self.html)

    def test_site_picker_is_compact_not_card_like(self):
        self.assertIn('width:13px;height:13px', self.html)
        self.assertIn('padding:4px 5px;border:0', self.html)
        self.assertIn("站点（可选条件；不勾选 = 不限制站点）", self.html)

    def test_downloader_picker_is_optional_and_persisted(self):
        self.assertIn('id="downloaderChoices"', self.html)
        self.assertIn('下载器（可选条件；不勾选 = 所有下载器）', self.html)
        self.assertIn("downloader_ids:checkedValues('downloaderChoices').map(Number)", self.html)

    def test_conditions_are_optional_and_hr_has_no_hidden_global_gate(self):
        self.assertIn('事件或当前状态', self.html)
        self.assertIn('留空表示不关心 H&amp;R', self.html)
        self.assertIn('未配置的条件不参与判断', self.html)
        self.assertNotIn('真实删种遇到 H&amp;R 标记时默认拦截', self.html)
        self.assertNotIn('allowHrDelete', self.html)

    def test_subscription_requeue_is_an_explicit_optional_condition(self):
        self.assertIn('id="avoidSubscriptionRequeue"', self.html)
        self.assertIn('avoid_subscription_requeue:', self.html)
        self.assertIn('未勾选时不限制，但结果会提示', self.html)
        self.assertIn('id="avoidSubscriptionRequeueField" hidden', self.html)

    def test_author_metadata_uses_amzxyz(self):
        import json
        store = json.loads((Path(__file__).resolve().parents[2] / "plugins" / "strategy-linkage" / "store.json").read_text(encoding="utf-8"))
        self.assertEqual(store["author"], "amzxyz")
        self.assertEqual(store["author_url"], "https://github.com/amzxyz")

    def test_actions_show_available_and_disabled_future_capabilities(self):
        self.assertIn('value="set_global_upload_limit"', self.html)
        self.assertIn('value="set_global_download_limit"', self.html)
        self.assertIn('value="delete_torrent_with_files"', self.html)
        self.assertIn('value="enable_alt_speed"', self.html)
        self.assertIn('value="set_global_queue_limits"', self.html)
        self.assertIn('value="replace_stalled_task"', self.html)
        self.assertNotIn('value="set_torrent_upload_limit"', self.html)
        self.assertNotIn('value="pause_torrent"', self.html)
        self.assertNotIn('id="allowGlobalLimits"', self.html)
        self.assertNotIn('id="allowDeleteFiles"', self.html)
        self.assertIn('id="globalDownloadLimitField"', self.html)
        self.assertIn('id="globalQueueLimitsField"', self.html)

    def test_current_task_speed_conditions_are_editable_and_future_seed_time_is_disabled(self):
        self.assertIn('id="minUploadSpeed"', self.html)
        self.assertIn('id="maxDownloadSpeed"', self.html)
        self.assertNotIn('实际累计做种时长（需要主程序提供可靠的做种时长）', self.html)
        self.assertIn('library.items.arrived', self.html)

    def test_ui_surfaces_are_opaque_and_primary_button_contrast_is_explicit(self):
        self.assertIn('--bg:#111214', self.html)
        self.assertIn('background:var(--bg)!important', self.html)
        self.assertIn('background:#191b1f!important', self.html)
        self.assertIn('background:#a91925!important', self.html)
        self.assertNotIn('--panel:var(--mc-glass-fill', self.html)

    def test_new_rule_button_is_inside_rules_section_header(self):
        section = self.html.split('<section class="section">\n  <div class="section-head"><h2>策略规则</h2>', 1)[1].split('</section>', 1)[0]
        self.assertIn('id="newRuleBtn"', section)
        header = self.html.split('<header class="top">', 1)[1].split('</header>', 1)[0]
        self.assertNotIn('id="newRuleBtn"', header)

    def test_history_defaults_to_ten_and_has_view_all_toggle(self):
        self.assertIn('const HISTORY_COLLAPSED_LIMIT=10', self.html)
        self.assertIn('id="historyToggleBtn"', self.html)
        self.assertIn('historyRows.slice(0,HISTORY_COLLAPSED_LIMIT)', self.html)
        self.assertIn('查看全部（${historyRows.length}）', self.html)

    def test_rule_run_button_distinguishes_saved_simulation_from_real_execution(self):
        self.assertIn("dry?'模拟运行':'执行规则'", self.html)
        self.assertIn('模拟模式：只演练，不修改下载器', self.html)
        self.assertNotIn('id="previewBtn"', self.html)
        self.assertNotIn('运行规则（模拟）', self.html)

    def test_compact_toolbar_replaces_global_settings_module(self):
        self.assertNotIn('全局安全与扫描设置', self.html)
        self.assertIn('class="actions top-controls"', self.html)
        self.assertIn('id="dryRun"', self.html)
        self.assertIn('id="scanInterval"', self.html)
        self.assertIn("addEventListener('change',queueSettingsSave)", self.html)
        self.assertIn("api('/settings',{method:'PUT'", self.html)
        self.assertNotIn('id="allowDelete"', self.html)
        self.assertNotIn('id="allowDeleteFiles"', self.html)
        self.assertNotIn('id="allowGlobalLimits"', self.html)
        self.assertNotIn('id="notifyOnAction"', self.html)
        self.assertNotIn('id="settingsRetryBtn"', self.html)
        self.assertNotIn('rel="manifest"', self.html)
        self.assertFalse((Path(__file__).resolve().parents[2] / "plugins" / "strategy-linkage" / "web" / "manifest.webmanifest").exists())

    def test_real_mode_executes_without_repeated_confirmation(self):
        self.assertNotIn("if(!dry&&!confirm(", self.html)
        self.assertIn("dry?'模拟运行':'执行规则'", self.html)
        py = (Path(__file__).resolve().parents[2] / "plugins" / "strategy-linkage" / "strategy_linkage.py").read_text(encoding="utf-8")
        self.assertNotIn('if not settings.get("allow_delete"', py)
        self.assertNotIn('if not settings.get("allow_delete_files"', py)
        self.assertNotIn('if not settings.get("allow_global_limits"', py)

    def test_rule_cards_always_put_action_on_a_separate_row(self):
        self.assertIn('class="expression-row conditions-row"', self.html)
        self.assertIn('class="expression-row action-row"', self.html)
        self.assertIn('触发条件', self.html)
        self.assertIn('联动动作', self.html)
        self.assertIn('flex-direction:column', self.html)
        self.assertIn('class="condition-chip"', self.html)
        self.assertIn('class="logic-plus"', self.html)
        self.assertIn('class="action-chip ${actionTone}"', self.html)
        self.assertIn('condition-legend', self.html)
        self.assertIn('action-legend', self.html)

    def test_clear_logs_button_and_endpoint_exist(self):
        self.assertIn('id="clearHistoryBtn"', self.html)
        self.assertIn("api('/history/clear'", self.html)
        py = (Path(__file__).resolve().parents[2] / "plugins" / "strategy-linkage" / "strategy_linkage.py").read_text(encoding="utf-8")
        self.assertIn('@router.post("/history/clear"', py)
        self.assertIn('await put(HISTORY_KEY, [])', py)


    def test_daily_time_is_a_real_rule_condition(self):
        self.assertIn('id="dailyTime"', self.html)
        self.assertIn("daily_time:$('dailyTime').value||''", self.html)
        py = (Path(__file__).resolve().parents[2] / "plugins" / "strategy-linkage" / "strategy_linkage.py").read_text(encoding="utf-8")
        self.assertIn('daily_time = str(raw.get("daily_time") or "").strip()', py)
        self.assertIn('每日定时时间未到', py)
        self.assertIn('schedule_date', py)

    def test_no_obsolete_action_gates_or_disabled_future_actions(self):
        self.assertNotIn('允许真实移除种子任务', self.html)
        self.assertNotIn('允许同时删除本地数据文件', self.html)
        self.assertNotIn('允许修改下载器全局设置', self.html)
        self.assertNotIn('计划支持（需要主程序公开单种操作接口）', self.html)
        self.assertNotIn('计划扩展的触发条件', self.html)
        self.assertNotIn('待接口', self.html)
        self.assertNotIn('点击上方“模拟运行”', self.html)

    def test_download_completed_does_not_falsely_mark_manual_tasks_as_subscription(self):
        py = (Path(__file__).resolve().parents[2] / "plugins" / "strategy-linkage" / "strategy_linkage.py").read_text(encoding="utf-8")
        handler = py.split("async def event_download_completed", 1)[1].split("async def event_ingest_completed", 1)[0]
        self.assertNotIn('source="subscription"', handler)

    def test_replace_action_requires_explicit_source_and_state_conditions(self):
        py = (Path(__file__).resolve().parents[2] / "plugins" / "strategy-linkage" / "strategy_linkage.py").read_text(encoding="utf-8")
        self.assertIn('停滞订阅任务换源的来源条件必须只选择“订阅”', py)
        self.assertIn('停滞订阅任务换源的状态条件必须只选择“停滞”', py)

    def test_preview_uses_first_matching_rule_per_task(self):
        py = (Path(__file__).resolve().parents[2] / "plugins" / "strategy-linkage" / "strategy_linkage.py").read_text(encoding="utf-8")
        self.assertIn('selected_match_tasks = set()', py)
        self.assertIn('candidate_identity in selected_match_tasks', py)
        self.assertNotIn('safe_ingest(state: dict)', py)
        self.assertNotIn('force_dry_run', py)

    def test_empty_daily_time_preserves_legacy_fingerprint(self):
        py = (Path(__file__).resolve().parents[2] / "plugins" / "strategy-linkage" / "strategy_linkage.py").read_text(encoding="utf-8")
        self.assertIn('if not fingerprint_rule.get("daily_time"):', py)
        self.assertIn('fingerprint_rule.pop("daily_time", None)', py)

    def test_progress_accepts_fraction_or_percent_snapshot(self):
        py = (Path(__file__).resolve().parents[2] / "plugins" / "strategy-linkage" / "strategy_linkage.py").read_text(encoding="utf-8")
        self.assertIn('progress_percent = progress if progress > 1 else progress * 100', py)


    def test_missing_downloader_task_is_miss_not_blocked(self):
        py = (Path(__file__).resolve().parents[2] / "plugins" / "strategy-linkage" / "strategy_linkage.py").read_text(encoding="utf-8")
        self.assertIn('preflight.get("exists") is False', py)
        self.assertIn('status": "miss", "message": message', py)
        self.assertIn('return "miss", message', py)
        self.assertIn('下载器中不存在对应任务，无需移除', py)
        self.assertIn('status": "blocked", "message": message', py)
        html = (Path(__file__).resolve().parents[2] / "plugins" / "strategy-linkage" / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("miss:'未命中'", html)
        self.assertIn("未命中 ${missed} 项", html)
        self.assertIn("未命中原因：", html)


    def test_evaluation_runs_are_serialized(self):
        py = (Path(__file__).resolve().parents[2] / "plugins" / "strategy-linkage" / "strategy_linkage.py").read_text(encoding="utf-8")
        self.assertIn("evaluation_lock = asyncio.Lock()", py)
        self.assertIn("async def _evaluate_all_unlocked(", py)
        self.assertIn("async with evaluation_lock:", py)
        self.assertIn("matched_count = len(notify_groups) + len(delete_groups)", py)


    def test_first_release_metadata_and_store_copy_are_clean(self):
        import json
        import tomllib
        root = Path(__file__).resolve().parents[2] / "plugins" / "strategy-linkage"
        manifest = tomllib.loads((root / "movieclaw-plugin.toml").read_text(encoding="utf-8"))["plugin"]
        store = json.loads((root / "store.json").read_text(encoding="utf-8"))
        changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertEqual(manifest["version"], "1.0.0")
        self.assertLessEqual(len(store["summary"]), 60)
        self.assertEqual(store["category"], "tools")
        self.assertIn("## 1.0.0", changelog)
        self.assertNotIn("0.1.13", changelog)
        self.assertTrue((root / "LICENSE").is_file())
        self.assertFalse((root / "web" / "icon.svg").exists())


if __name__ == "__main__":
    unittest.main()
