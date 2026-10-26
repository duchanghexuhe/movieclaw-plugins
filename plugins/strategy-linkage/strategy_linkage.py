"""策略联动：规则之间 OR，单条规则内部所有条件 AND。

所有站点、任务、入库与删种事实均通过 MovieClaw 已公开的 host-ops 获取；
不直连 qBittorrent / Transmission，也不把下载器全局限速伪装成单种限速。
"""

import asyncio
import hashlib
import json
import time
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from movieclaw_api.plugins.keys import PLUGIN_DATA, PLUGIN_ROUTES
from movieclaw_kernel import DURABLE_EVENTS
from movieclaw_sdk import Context, plugin
from movieclaw_sdk.events import (
    DOWNLOAD_COMPLETED,
    DOWNLOAD_IMPORTED,
    LIBRARY_INGEST_COMPLETED,
    LIBRARY_FILES_ADDED,
    LIBRARY_ITEMS_ARRIVED,
    PLUGIN_EVENTS,
    SUBSCRIPTION_DOWNLOAD_STARTED,
    SUBSCRIPTION_FULFILLED,
    DownloadCompleted,
    DownloadImported,
    IngestCompleted,
    FilesAdded,
    ItemsArrived,
    SubscriptionDownloadStarted,
    SubscriptionFulfilled,
    plugin_event,
)
from movieclaw_sdk.ops import HOST_OPS, OpsError

PLUGIN_ID = "strategy-linkage"
RULES_KEY = "rules"
SETTINGS_KEY = "settings"
HISTORY_KEY = "history"
SEEN_EVENTS_KEY = "seen_event_ids"
TORRENT_PREFIX = "torrent:"
MAX_RULES = 40
MAX_STATES = 1200
MAX_HISTORY = 60
MAX_SEEN_EVENTS = 500

EVENT_CHOICES = {
    "subscription.download_started": "订阅种子已提交下载器",
    "download.completed": "下载已完成",
    "library.ingest.completed": "整理入库成功（非暂存目录）",
    "library.files.added": "媒体库文件入账（有文件来源关联）",
    "download.imported": "手动下载内容已入库",
    "library.items.arrived": "媒体库发现新内容",
    "subscription.fulfilled": "订阅季集已确认入库",
}
ELAPSED_CHOICES = {
    "first_seen": "插件首次观测到任务",
    "subscription.download_started": "提交下载器后",
    "download.completed": "下载完成后",
    "library.ingest.completed": "整理入库成功后",
    "library.files.added": "媒体库文件入账后",
    "download.imported": "手动下载入库后",
    "library.items.arrived": "媒体库新增后",
    "subscription.fulfilled": "订阅季集确认入库后",
}
MEDIA_KINDS = {"movie", "tv", "other", "photo", "unknown"}
SOURCES = {"subscription", "manual", "boost", "external", "unknown"}
TASK_STATES = {"downloading", "stalled", "queued", "paused", "checking", "completed", "error", "missing", "unknown"}
ACTIONS = {
    "notify",
    "delete_torrent_keep_files",
    "delete_torrent_with_files",
    "set_global_upload_limit",
    "clear_global_upload_limit",
    "set_global_download_limit",
    "clear_global_download_limit",
    "enable_alt_speed",
    "disable_alt_speed",
    "enable_global_queue",
    "disable_global_queue",
    "set_global_queue_limits",
    "replace_stalled_task",
}
GLOBAL_ACTIONS = ACTIONS & {
    "set_global_upload_limit", "clear_global_upload_limit",
    "set_global_download_limit", "clear_global_download_limit",
    "enable_alt_speed", "disable_alt_speed",
    "enable_global_queue", "disable_global_queue",
    "set_global_queue_limits",
}
GLOBAL_LIMIT_FIELDS = (
    "download_limit_bytes", "upload_limit_bytes", "alt_speed_enabled", "queue_enabled",
    "max_active_downloads", "max_active_uploads", "max_active_torrents",
)
DEFAULT_SETTINGS = {
    "dry_run": True,
    "scan_interval_seconds": 60,
}


class RuleActionEvent(BaseModel):
    model_config = ConfigDict(frozen=True)
    rule_name: str
    action_label: str
    torrent_name: str
    site_name: str
    result: str
    info_hash: str


RULE_ACTION_EVENT = plugin_event(
    f"{PLUGIN_ID}.rule-action",
    RuleActionEvent,
    title="策略联动：规则执行结果",
    doc="策略联动规则模拟或实际执行后的结果。",
)


def now_ts() -> float:
    return time.time()


def norm_hash(value: Any) -> str:
    return str(value or "").strip().lower()


def norm_kind(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"series", "tvshow", "tv_show", "电视剧", "剧集", "show"}:
        return "tv"
    if text in {"movie", "film", "电影"}:
        return "movie"
    if text in {"other", "photo", "unknown", ""}:
        return text or "unknown"
    return text if text in MEDIA_KINDS else "unknown"


def norm_source(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text == "file":
        return "external"
    if not text:
        return "unknown"
    return text if text in SOURCES else "unknown"


def as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def items_of(payload: Any) -> list[dict]:
    if isinstance(payload, dict):
        items = payload.get("items")
        return [x for x in items if isinstance(x, dict)] if isinstance(items, list) else []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    return []


def rule_fingerprint(rule: dict) -> str:
    # 兼容旧规则：空的新字段不改变既有指纹，避免升级后重放已经执行过的动作。
    fingerprint_rule = dict(rule)
    if not fingerprint_rule.get("avoid_subscription_requeue"):
        fingerprint_rule.pop("avoid_subscription_requeue", None)
    if not fingerprint_rule.get("daily_time"):
        fingerprint_rule.pop("daily_time", None)
    packed = json.dumps(fingerprint_rule, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(packed.encode("utf-8")).hexdigest()[:16]


def build_global_limits_update(action_type: str, action: dict, current_limits: dict) -> tuple[dict, str]:
    """Build a read-modify-write payload, preserving every unrelated global setting.

    The host API's limit fields use null to mean unlimited, so sending only one field
    could accidentally clear another limit. Start from the live snapshot and override
    only the requested setting.
    """
    update = {field: current_limits.get(field) for field in GLOBAL_LIMIT_FIELDS}

    def fmt_limit(value: Any) -> str:
        parsed = as_int(value)
        return "不限速" if parsed is None or parsed == 0 else f"{parsed / 1000:g} KB/s"

    def fmt_count(value: Any) -> str:
        parsed = as_int(value)
        return "未知" if parsed is None else ("不限" if parsed == -1 else str(parsed))

    if action_type in {"set_global_upload_limit", "clear_global_upload_limit"}:
        field = "upload_limit_bytes"
        label = "上传"
        old = current_limits.get(field)
        target = action.get("upload_limit_bytes") if action_type == "set_global_upload_limit" else None
        update[field] = target
        target_text = fmt_limit(target)
        return update, f"全局{label}限速：{fmt_limit(old)} → {target_text}（影响该下载器所有种子）"

    if action_type in {"set_global_download_limit", "clear_global_download_limit"}:
        field = "download_limit_bytes"
        label = "下载"
        old = current_limits.get(field)
        target = action.get("download_limit_bytes") if action_type == "set_global_download_limit" else None
        update[field] = target
        target_text = fmt_limit(target)
        return update, f"全局{label}限速：{fmt_limit(old)} → {target_text}（影响该下载器所有种子）"

    if action_type in {"enable_alt_speed", "disable_alt_speed"}:
        target = action_type == "enable_alt_speed"
        update["alt_speed_enabled"] = target
        old = current_limits.get("alt_speed_enabled")
        old_text = "已启用" if old is True else "已关闭" if old is False else "状态未知"
        return update, f"备用限速模式：{old_text} → {'启用' if target else '关闭'}（使用下载器自身配置的备用速度）"

    if action_type in {"enable_global_queue", "disable_global_queue"}:
        target = action_type == "enable_global_queue"
        update["queue_enabled"] = target
        old = current_limits.get("queue_enabled")
        old_text = "已启用" if old is True else "已关闭" if old is False else "状态未知"
        return update, f"下载队列管理：{old_text} → {'启用' if target else '关闭'}（影响该下载器整体排队行为）"

    if action_type == "set_global_queue_limits":
        names = {
            "max_active_downloads": "最大同时下载数",
            "max_active_uploads": "最大同时做种数",
        }
        changes = []
        for field, label in names.items():
            if field not in action:
                continue
            old = current_limits.get(field)
            target = action[field]
            update[field] = target
            changes.append(f"{label}：{fmt_count(old)} → {fmt_count(target)}")
        return update, "；".join(changes) + "（影响该下载器整体队列）"

    raise ValueError(f"不是全局下载器动作：{action_type}")


def validate_rule(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("每条规则必须是对象")
    name = str(raw.get("name") or "").strip()
    if not name or len(name) > 80:
        raise ValueError("规则名称不能为空且不能超过 80 个字符")
    rule_id = str(raw.get("id") or "").strip() or uuid.uuid4().hex[:12]
    if len(rule_id) > 64:
        raise ValueError("规则 ID 太长")
    legacy_all_sites = bool(raw.get("all_sites", False))
    site_ids_raw = raw.get("site_ids", [])
    if not isinstance(site_ids_raw, list) or any(not isinstance(x, str) for x in site_ids_raw):
        raise ValueError("站点选择格式不正确")
    site_ids = list(dict.fromkeys(x.strip() for x in site_ids_raw if x.strip()))[:100]
    downloader_ids_raw = raw.get("downloader_ids", [])
    if not isinstance(downloader_ids_raw, list):
        raise ValueError("下载器选择格式不正确")
    downloader_ids: list[int] = []
    for value in downloader_ids_raw:
        if isinstance(value, bool):
            raise ValueError("下载器 ID 必须是正整数")
        if isinstance(value, float) and not value.is_integer():
            raise ValueError("下载器 ID 必须是正整数")
        try:
            downloader_id = int(value)
        except (TypeError, ValueError):
            raise ValueError("下载器 ID 必须是正整数") from None
        if downloader_id <= 0:
            raise ValueError("下载器 ID 必须是正整数")
        if downloader_id not in downloader_ids:
            downloader_ids.append(downloader_id)
    downloader_ids = downloader_ids[:100]
    # PT 站点只是可选筛选条件：未选择站点表示不按站点过滤。
    # 兼容旧版 all_sites=true 的规则，避免升级后把旧的“全部站点”误变成指定站点。
    if legacy_all_sites:
        site_ids = []
    all_sites = not bool(site_ids)

    def string_list(key: str, allowed: set[str], label: str) -> list[str]:
        value = raw.get(key, [])
        if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
            raise ValueError(f"{label}格式不正确")
        unique = list(dict.fromkeys(x for x in value if x))
        invalid = sorted(set(unique) - allowed)
        if invalid:
            raise ValueError(f"{label}包含不支持的值：{', '.join(invalid)}")
        return unique

    media_kinds = string_list("media_kinds", MEDIA_KINDS, "资源类别")
    sources = string_list("sources", SOURCES, "来源类型")
    required_events = string_list("required_events", set(EVENT_CHOICES), "触发事件")
    task_states = string_list("task_states", TASK_STATES, "下载器状态")

    daily_time = str(raw.get("daily_time") or "").strip()
    if daily_time:
        try:
            parsed_daily_time = datetime.strptime(daily_time, "%H:%M")
        except ValueError:
            raise ValueError("每日定时必须使用 HH:MM 格式") from None
        if parsed_daily_time.strftime("%H:%M") != daily_time:
            raise ValueError("每日定时必须使用 HH:MM 格式")
    else:
        daily_time = ""

    elapsed_from = str(raw.get("elapsed_from") or "")
    min_elapsed = as_float(raw.get("min_elapsed_hours"))
    if min_elapsed is not None and min_elapsed < 0:
        raise ValueError("经过时间不能小于 0 小时")
    if min_elapsed is not None and elapsed_from not in ELAPSED_CHOICES:
        raise ValueError("设置经过时间时，必须选择起算事件")
    if min_elapsed is None:
        elapsed_from = ""

    min_uploaded = as_int(raw.get("min_uploaded_bytes"))
    if min_uploaded is not None and min_uploaded < 0:
        raise ValueError("累计上传量不能小于 0")
    min_upload_speed = as_float(raw.get("min_upload_speed_kbps"))
    max_upload_speed = as_float(raw.get("max_upload_speed_kbps"))
    min_download_speed = as_float(raw.get("min_download_speed_kbps"))
    max_download_speed = as_float(raw.get("max_download_speed_kbps"))
    for label, value in (("上传速度下限", min_upload_speed), ("上传速度上限", max_upload_speed), ("下载速度下限", min_download_speed), ("下载速度上限", max_download_speed)):
        if value is not None and value < 0:
            raise ValueError(f"{label}不能小于 0")
    if min_upload_speed is not None and max_upload_speed is not None and min_upload_speed > max_upload_speed:
        raise ValueError("上传速度下限不能大于上限")
    if min_download_speed is not None and max_download_speed is not None and min_download_speed > max_download_speed:
        raise ValueError("下载速度下限不能大于上限")
    min_ratio = as_float(raw.get("min_share_ratio"))
    if min_ratio is not None and min_ratio < 0:
        raise ValueError("分享率不能小于 0")
    min_progress = as_float(raw.get("min_progress_percent"))
    if min_progress is not None and not 0 <= min_progress <= 100:
        raise ValueError("完成度必须在 0～100 之间")
    min_size_gb = as_float(raw.get("min_size_gb"))
    if min_size_gb is not None and min_size_gb < 0:
        raise ValueError("资源大小不能小于 0 GB")
    hit_and_run = raw.get("hit_and_run")
    if hit_and_run not in (None, True, False):
        raise ValueError("H&R 条件必须是不限、有 H&R 或无 H&R")
    avoid_subscription_requeue = raw.get("avoid_subscription_requeue", False)
    if not isinstance(avoid_subscription_requeue, bool):
        raise ValueError("避免订阅重新找源条件必须是布尔值")

    action = raw.get("action")
    if not isinstance(action, dict):
        action = {"type": "notify"}
    action_type = str(action.get("type") or "notify")
    if action_type not in ACTIONS:
        raise ValueError(f"不支持的执行动作：{action_type}")
    if avoid_subscription_requeue and action_type not in {"delete_torrent_keep_files", "delete_torrent_with_files"}:
        raise ValueError("避免订阅重新找源条件仅适用于移除种子动作")
    if action_type == "replace_stalled_task":
        if set(sources) != {"subscription"}:
            raise ValueError("停滞订阅任务换源的来源条件必须只选择“订阅”")
        if set(task_states) != {"stalled"}:
            raise ValueError("停滞订阅任务换源的状态条件必须只选择“停滞”")
    normalized_action = {"type": action_type}
    if action_type == "set_global_upload_limit":
        upload_limit_kbps = as_float(action.get("upload_limit_kbps"))
        if upload_limit_kbps is None or not 1 <= upload_limit_kbps <= 1_000_000:
            raise ValueError("下载器全局上传限速必须在 1～1000000 KB/s 之间")
        normalized_action["upload_limit_kbps"] = upload_limit_kbps
        # MovieClaw 统一接口使用字节/秒；UI 采用十进制 KB/s（1 KB = 1000 B）。
        normalized_action["upload_limit_bytes"] = int(round(upload_limit_kbps * 1000))
    if action_type == "set_global_download_limit":
        download_limit_kbps = as_float(action.get("download_limit_kbps"))
        if download_limit_kbps is None or not 1 <= download_limit_kbps <= 1_000_000:
            raise ValueError("下载器全局下载限速必须在 1～1000000 KB/s 之间")
        normalized_action["download_limit_kbps"] = download_limit_kbps
        normalized_action["download_limit_bytes"] = int(round(download_limit_kbps * 1000))
    if action_type == "set_global_queue_limits":
        for field, label in (("max_active_downloads", "最大同时下载数"), ("max_active_uploads", "最大同时做种数")):
            value = action.get(field)
            if value is None or (isinstance(value, str) and not value.strip()):
                continue
            if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
                raise ValueError(f"{label}必须是 -1～100000 的整数（-1 表示不限）")
            parsed = as_int(value)
            if parsed is None or not -1 <= parsed <= 100_000:
                raise ValueError(f"{label}必须是 -1～100000 的整数（-1 表示不限）")
            normalized_action[field] = parsed
        if not any(field in normalized_action for field in ("max_active_downloads", "max_active_uploads")):
            raise ValueError("请至少填写最大同时下载数或最大同时做种数")

    has_trigger = bool(daily_time or required_events or task_states or min_elapsed is not None or min_uploaded is not None or min_upload_speed is not None or max_upload_speed is not None or min_download_speed is not None or max_download_speed is not None or min_ratio is not None or min_progress is not None or min_size_gb is not None or hit_and_run is not None)
    if not has_trigger:
        raise ValueError("请至少设置一个触发条件；规则范围本身不能直接触发动作")

    return {
        "id": rule_id,
        "name": name,
        "enabled": bool(raw.get("enabled", True)),
        "all_sites": all_sites,
        "site_ids": site_ids,
        "downloader_ids": downloader_ids,
        "media_kinds": media_kinds,
        "sources": sources,
        "required_events": required_events,
        "daily_time": daily_time,
        "elapsed_from": elapsed_from,
        "min_elapsed_hours": min_elapsed,
        "task_states": task_states,
        "min_uploaded_bytes": min_uploaded,
        "min_upload_speed_kbps": min_upload_speed,
        "max_upload_speed_kbps": max_upload_speed,
        "min_download_speed_kbps": min_download_speed,
        "max_download_speed_kbps": max_download_speed,
        "min_share_ratio": min_ratio,
        "min_progress_percent": min_progress,
        "min_size_gb": min_size_gb,
        "hit_and_run": hit_and_run,
        "avoid_subscription_requeue": avoid_subscription_requeue,
        "action": normalized_action,
    }


def rule_matches(
    rule: dict,
    state: dict,
    task: dict | None,
    configured_site_ids: set[str],
    now: float,
    configured_downloader_ids: set[int] | None = None,
) -> tuple[bool, str]:
    """一条规则内部的 AND 判断；站点、下载器和类别各自多选时为集合 OR。"""
    selected_downloader_ids = {
        parsed for parsed in (as_int(value) for value in (rule.get("downloader_ids") or []))
        if parsed is not None
    }
    if selected_downloader_ids:
        downloader_id = as_int((task or {}).get("downloader_id")) or as_int(
            state.get("downloader_id")
        )
        if downloader_id is None:
            return False, "无法识别任务所属下载器"
        if configured_downloader_ids is not None and downloader_id not in configured_downloader_ids:
            return False, "下载器已不在当前配置中"
        if downloader_id not in selected_downloader_ids:
            return False, "下载器不匹配"

    site_id = str((task or {}).get("site_id") or state.get("site_id") or "")
    selected_site_ids = set(rule.get("site_ids") or [])
    # 只有用户明确勾选站点时，站点才参与 AND 判断；空选择表示不限站点。
    if selected_site_ids:
        if not site_id or site_id not in configured_site_ids:
            return False, "来源站点未知或已不在当前配置中"
        if site_id not in selected_site_ids:
            return False, "站点不匹配"

    kind = norm_kind(state.get("media_kind") or (task or {}).get("media_kind"))
    if rule.get("media_kinds") and kind not in rule["media_kinds"]:
        return False, "资源类别不匹配"

    source = norm_source((task or {}).get("source") or state.get("source"))
    if rule.get("sources") and source not in rule["sources"]:
        return False, "下载来源不匹配"

    daily_time = rule.get("daily_time") or ""
    if daily_time:
        local_clock = datetime.fromtimestamp(now).astimezone().strftime("%H:%M")
        if local_clock < daily_time:
            return False, f"每日定时时间未到：{daily_time}"

    event_times = state.get("event_times") if isinstance(state.get("event_times"), dict) else {}
    for event_name in rule.get("required_events") or []:
        if event_times.get(event_name):
            continue
        # 下载器当前状态是“已完成”时，可用于匹配“下载完成”条件，特别是插件安装前
        # 已完成的存量任务。它只证明当前完成状态，不伪造 download.completed 的历史时间；
        # 因此“下载完成后经过 N 小时”仍必须有真实事件时间戳。
        if event_name == "download.completed" and (
            (task or {}).get("state") == "completed" or (task or {}).get("completed") is True
        ):
            continue
        return False, f"尚未发生必需事件：{EVENT_CHOICES.get(event_name, event_name)} ({event_name})"

    elapsed = rule.get("min_elapsed_hours")
    if elapsed is not None:
        start_key = rule.get("elapsed_from")
        start_at = state.get("first_seen_at") if start_key == "first_seen" else event_times.get(start_key)
        start_num = as_float(start_at)
        if start_num is None:
            return False, f"缺少起算时间：{ELAPSED_CHOICES.get(start_key, start_key)}"
        actual_hours = max(0.0, now - start_num) / 3600.0
        if actual_hours < float(elapsed):
            return False, f"经过时间 {actual_hours:.2f} 小时，未达到 {float(elapsed):.2f} 小时"

    if rule.get("task_states"):
        state_value = str((task or {}).get("state") or "unknown")
        if state_value not in rule["task_states"]:
            return False, f"下载器状态 {state_value} 不匹配"

    if rule.get("min_uploaded_bytes") is not None:
        uploaded = as_float((task or {}).get("uploaded_bytes"))
        if uploaded is None or uploaded < float(rule["min_uploaded_bytes"]):
            return False, "累计上传量缺失或未达到阈值"

    for field, label, comparator in (
        ("min_upload_speed_kbps", "上传速度", "min"),
        ("max_upload_speed_kbps", "上传速度", "max"),
        ("min_download_speed_kbps", "下载速度", "min"),
        ("max_download_speed_kbps", "下载速度", "max"),
    ):
        threshold = rule.get(field)
        if threshold is None:
            continue
        speed_key = "upspeed_bytes" if "upload" in field else "dlspeed_bytes"
        speed_bytes = as_float((task or {}).get(speed_key))
        if speed_bytes is None:
            return False, f"{label}数据缺失，无法判断阈值"
        speed_kbps = speed_bytes / 1000.0
        if comparator == "min" and speed_kbps < float(threshold):
            return False, f"当前{label} {speed_kbps:.1f} KB/s，低于 {float(threshold):.1f} KB/s"
        if comparator == "max" and speed_kbps > float(threshold):
            return False, f"当前{label} {speed_kbps:.1f} KB/s，高于 {float(threshold):.1f} KB/s"

    if rule.get("min_share_ratio") is not None:
        uploaded = as_float((task or {}).get("uploaded_bytes"))
        downloaded = as_float((task or {}).get("completed_bytes"))
        if uploaded is None or downloaded is None or downloaded <= 0:
            return False, "无法从任务快照可靠计算分享率"
        ratio = uploaded / downloaded
        if ratio < float(rule["min_share_ratio"]):
            return False, f"分享率 {ratio:.3f}，未达到 {float(rule['min_share_ratio']):.3f}"

    if rule.get("min_progress_percent") is not None:
        progress = as_float((task or {}).get("progress"))
        if progress is None:
            return False, "完成度缺失或未达到阈值"
        progress_percent = progress if progress > 1 else progress * 100
        if progress_percent < float(rule["min_progress_percent"]):
            return False, "完成度缺失或未达到阈值"

    if rule.get("min_size_gb") is not None:
        size_bytes = as_float((task or {}).get("size_bytes"))
        if size_bytes is None or size_bytes / (1024 ** 3) < float(rule["min_size_gb"]):
            return False, "资源大小缺失或未达到阈值"

    if rule.get("hit_and_run") is not None:
        if not site_id or site_id not in configured_site_ids:
            return False, "来源站点未知，无法判断 H&R 条件"
        actual_hr = state.get("hit_and_run")
        if actual_hr is None:
            return False, "已选择 H&R 条件，但站点状态未知，无法匹配"
        if bool(actual_hr) != bool(rule["hit_and_run"]):
            return False, "站点 H&R 状态不匹配"

    return True, "所有条件满足（AND）"


def check_requeue_condition(rule: dict, requeued_units: list | tuple | None) -> tuple[bool, str]:
    """订阅重找源是可选 AND 条件；未勾选时不拦截，只在结果中提示。"""
    if rule.get("avoid_subscription_requeue") and requeued_units:
        return False, "未满足可选条件：移除该任务会触发订阅重新寻找资源"
    return True, ""


def make_state(info_hash: str, now: float) -> dict:
    return {
        "info_hash": norm_hash(info_hash),
        "site_id": None,
        "downloader_id": None,
        "media_kind": "unknown",
        "media_item_id": None,
        "media_title": "",
        "torrent_name": "",
        "source": "unknown",
        "hit_and_run": None,
        "owned_by_movieclaw": None,
        "library_id": None,
        "staging": False,
        "first_seen_at": now,
        "last_seen_at": now,
        "event_times": {},
        "fired": {},
    }


def merge_state(old: dict | None, info_hash: str, now: float, *, site_id: Any = None,
                downloader_id: Any = None, media_kind: Any = None, media_item_id: Any = None,
                media_title: Any = None, torrent_name: Any = None, source: Any = None,
                library_id: Any = None, staging: Any = None, event_name: str | None = None) -> dict:
    state = dict(old) if isinstance(old, dict) else make_state(info_hash, now)
    state["info_hash"] = norm_hash(info_hash)
    state["first_seen_at"] = as_float(state.get("first_seen_at")) or now
    state["last_seen_at"] = now
    if site_id:
        state["site_id"] = str(site_id)
    parsed_downloader = as_int(downloader_id)
    if parsed_downloader is not None:
        state["downloader_id"] = parsed_downloader
    if media_kind:
        state["media_kind"] = norm_kind(media_kind)
    parsed_media_id = as_int(media_item_id)
    if parsed_media_id is not None:
        state["media_item_id"] = parsed_media_id
    if media_title:
        state["media_title"] = str(media_title)[:300]
    if torrent_name:
        state["torrent_name"] = str(torrent_name)[:300]
    if source:
        state["source"] = norm_source(source)
    parsed_library = as_int(library_id)
    if parsed_library is not None:
        state["library_id"] = parsed_library
    if staging is not None:
        state["staging"] = bool(staging)
    event_times = dict(state.get("event_times") or {})
    if event_name:
        event_times.setdefault(event_name, now)
    state["event_times"] = event_times
    return state


@plugin(
    PLUGIN_ID,
    title="策略联动",
    inject=(HOST_OPS, PLUGIN_DATA, PLUGIN_ROUTES, PLUGIN_EVENTS, DURABLE_EVENTS),
    permissions=(
        "site.list",
        "site.catalog",
        "dl.list",
        "dl.tasks",
        "dl.torrent.delete",
        "dl.torrent.replace",
        "dl.limits",
        "dl.limits.set",
        "dl.file-sources.list",
        "library.items.relations",
        "library.list",
    ),
)
async def strategy_linkage(ctx: Context) -> None:
    ops = await ctx.use(HOST_OPS).client(ctx)
    store = ctx.use(PLUGIN_DATA).scoped(ctx)
    publisher = await ctx.use(PLUGIN_EVENTS).register(ctx, RULE_ACTION_EVENT)
    log = ctx.logger

    async def data(key: str, default: Any = None) -> Any:
        return await store.get(key, default=default)

    async def put(key: str, value: Any) -> None:
        await store.set(key, value)

    async def settings_now() -> dict:
        raw = await data(SETTINGS_KEY, {})
        settings = dict(DEFAULT_SETTINGS)
        if isinstance(raw, dict):
            settings.update(raw)
        settings["dry_run"] = bool(settings.get("dry_run", True))
        # 旧版全局授权开关不再参与执行；执行模式由顶部“模拟”勾选控制。
        for legacy_key in ("allow_delete", "allow_delete_files", "allow_global_limits", "allow_hr_delete", "notify_on_action"):
            settings.pop(legacy_key, None)
        interval = as_int(settings.get("scan_interval_seconds")) or 60
        settings["scan_interval_seconds"] = max(30, min(3600, interval))
        return settings

    async def rules_now() -> list[dict]:
        raw = await data(RULES_KEY, [])
        if not isinstance(raw, list):
            return []
        result = []
        for rule in raw[:MAX_RULES]:
            try:
                result.append(validate_rule(rule))
            except Exception as exc:
                log.warning("忽略无效策略规则：%s", str(exc)[:200])
        return result

    async def add_history(row: dict) -> None:
        history = await data(HISTORY_KEY, [])
        if not isinstance(history, list):
            history = []
        entry = {
            "at": now_ts(),
            "rule_id": str(row.get("rule_id") or ""),
            "rule_name": str(row.get("rule_name") or "")[:100],
            "info_hash": norm_hash(row.get("info_hash")),
            "torrent_name": str(row.get("torrent_name") or "")[:120],
            "site_id": str(row.get("site_id") or ""),
            "action": str(row.get("action") or ""),
            "status": str(row.get("status") or ""),
            "message": str(row.get("message") or "")[:300],
        }
        for last in history[:30]:
            if not isinstance(last, dict):
                continue
            same = all(last.get(k) == entry.get(k) for k in ("rule_id", "info_hash", "action", "status", "message"))
            last_at = as_float(last.get("at")) or 0
            if same and entry["at"] - last_at < 3600:
                return
        history.insert(0, entry)
        await put(HISTORY_KEY, history[:MAX_HISTORY])

    async def event_seen(event_id: Any) -> bool:
        if not event_id:
            return False
        seen = await data(SEEN_EVENTS_KEY, [])
        return isinstance(seen, list) and str(event_id) in seen

    async def mark_event_seen(event_id: Any) -> None:
        if not event_id:
            return
        seen = await data(SEEN_EVENTS_KEY, [])
        if not isinstance(seen, list):
            seen = []
        seen = [str(event_id), *[str(x) for x in seen if str(x) != str(event_id)]]
        await put(SEEN_EVENTS_KEY, seen[:MAX_SEEN_EVENTS])

    async def get_state(info_hash: str) -> dict | None:
        key = norm_hash(info_hash)
        if not key:
            return None
        value = await data(TORRENT_PREFIX + key, None)
        return value if isinstance(value, dict) else None

    async def save_state(info_hash: str, state: dict) -> None:
        key = norm_hash(info_hash)
        if not key:
            return
        await put(TORRENT_PREFIX + key, state)

    async def state_rows() -> list[dict]:
        try:
            all_data = await store.items()
        except Exception:
            log.warning("读取策略联动台账失败", exc_info=True)
            return []
        rows = []
        if isinstance(all_data, dict):
            for key, value in all_data.items():
                if isinstance(key, str) and key.startswith(TORRENT_PREFIX) and isinstance(value, dict):
                    row = dict(value)
                    row.setdefault("info_hash", key[len(TORRENT_PREFIX):])
                    rows.append(row)
        rows.sort(key=lambda x: as_float(x.get("last_seen_at")) or 0, reverse=True)
        return rows

    async def prune_states() -> None:
        rows = await state_rows()
        for row in rows[MAX_STATES:]:
            key = norm_hash(row.get("info_hash"))
            if key:
                await store.delete(TORRENT_PREFIX + key)

    async def task_snapshot() -> tuple[dict[str, dict], list[dict], str | None]:
        """只使用当前主程序已公开的 dl.tasks；不要求用户先修改主程序。"""
        try:
            payload = await ops.call("dl.tasks", {})
        except OpsError as exc:
            return {}, [], exc.message
        except Exception as exc:
            return {}, [], str(exc)
        items = items_of(payload)
        index = {}
        for task in items:
            key = norm_hash(task.get("info_hash"))
            if key:
                index[key] = task
        return index, items, None

    async def configured_sites() -> tuple[set[str], dict[str, dict], str | None]:
        try:
            configured = await ops.call("site.list", {})
        except Exception as exc:
            return set(), {}, str(getattr(exc, "message", exc))
        try:
            catalog = await ops.call("site.catalog", {})
        except Exception:
            catalog = []
        catalog_by_id = {str(x.get("site_id")): x for x in items_of(catalog) if x.get("site_id")}
        # 站点目录返回列表；site.list 返回已配置的站点。只允许用户配置过的站点参与规则。
        sites = {}
        for site in items_of(configured):
            site_id = str(site.get("site_id") or "")
            if not site_id:
                continue
            meta = catalog_by_id.get(site_id, {})
            sites[site_id] = {
                "site_id": site_id,
                "display_name": str(meta.get("display_name") or site_id),
                "base_url": str(meta.get("base_url") or ""),
                "enabled": bool(site.get("enabled", False)),
                "usable": bool(site.get("usable", False)),
                "status": str(site.get("status") or "unknown"),
            }
        return set(sites), sites, None

    async def configured_downloaders() -> tuple[set[int], dict[int, dict], str | None]:
        try:
            configured = await ops.call("dl.list", {})
        except Exception as exc:
            return set(), {}, str(getattr(exc, "message", exc))
        downloaders: dict[int, dict] = {}
        for row in items_of(configured):
            downloader_id = as_int(row.get("id"))
            if downloader_id is None:
                continue
            downloaders[downloader_id] = {
                "id": downloader_id,
                "name": str(row.get("name") or f"下载器 {downloader_id}"),
                "client_type": str(row.get("client_type") or "unknown"),
                "enabled": bool(row.get("enabled", True)),
                "usable": bool(row.get("usable", True)),
                "status": str(row.get("status") or "unknown"),
            }
        return set(downloaders), downloaders, None

    async def save_event_state(info_hash: str, event_name: str, **fields) -> dict:
        now = now_ts()
        state = merge_state(await get_state(info_hash), info_hash, now, event_name=event_name, **fields)
        await save_state(info_hash, state)
        return state

    async def enrich_from_task(info_hash: str, task_index: dict[str, dict]) -> None:
        key = norm_hash(info_hash)
        task = task_index.get(key)
        if not task:
            return
        old = await get_state(key)
        state = merge_state(
            old, key, now_ts(),
            site_id=(old or {}).get("site_id") or task.get("site_id"), downloader_id=task.get("downloader_id"),
            media_kind=(old or {}).get("media_kind") if (old or {}).get("media_kind") not in (None, "unknown") else task.get("media_kind"),
            media_item_id=(old or {}).get("media_item_id") or task.get("media_item_id"),
            media_title=(old or {}).get("media_title") or task.get("media_title"),
            torrent_name=(old or {}).get("torrent_name") or task.get("name"), source=task.get("source") or (old or {}).get("source"),
        )
        await save_state(key, state)

    async def relate_media_to_torrents(library_id: int, media_id: int, media: Any, event_name: str,
                                       expected_subscription_id: int | None = None) -> list[str]:
        try:
            result = await ops.call("library.items.relations", {"library_id": library_id, "media_item_id": media_id})
        except OpsError as exc:
            # 404 表示该库里没有这个条目/库存文件；不是可删除目标。其他错误应让可靠事件重试。
            if exc.status == 404:
                log.debug("媒体库 %s 没有可关联的条目 %s", library_id, media_id)
                return []
            raise
        if expected_subscription_id is not None and as_int(result.get("subscription_id")) != expected_subscription_id:
            return []
        torrents = result.get("torrents", []) if isinstance(result, dict) else []
        touched = []
        now = now_ts()
        for torrent in torrents if isinstance(torrents, list) else []:
            if not isinstance(torrent, dict):
                continue
            key = norm_hash(torrent.get("info_hash"))
            if not key:
                continue
            state = merge_state(
                await get_state(key), key, now,
                site_id=torrent.get("site_id"), downloader_id=torrent.get("downloader_id"),
                media_kind=getattr(media, "kind", None) if not isinstance(media, dict) else media.get("kind"),
                media_item_id=media_id,
                media_title=getattr(media, "title", None) if not isinstance(media, dict) else media.get("title"),
                torrent_name=torrent.get("title"), source=torrent.get("source"), library_id=library_id,
                event_name=event_name,
            )
            if torrent.get("hit_and_run") is not None:
                state["hit_and_run"] = bool(torrent.get("hit_and_run"))
            if torrent.get("owned_by_movieclaw") is not None:
                state["owned_by_movieclaw"] = bool(torrent.get("owned_by_movieclaw"))
            # download.imported 是“手动下载内容全部入库”的成功事件，可以作为安全入库凭据。
            if event_name == "download.imported":
                state["staging"] = False
            await save_state(key, state)
            touched.append(key)
        return touched

    async def event_download_started(event: SubscriptionDownloadStarted) -> None:
        event_id = ctx.delivery.event_id if ctx.delivery else None
        if await event_seen(event_id):
            return
        torrent = event.torrent
        key = norm_hash(getattr(torrent, "info_hash", None))
        if key:
            await save_event_state(
                key, "subscription.download_started",
                site_id=getattr(torrent, "site_id", None),
                media_kind=getattr(event.media, "kind", None), media_item_id=getattr(event.media, "id", None),
                media_title=getattr(event.media, "title", None), torrent_name=getattr(torrent, "title", None),
                source="subscription",
            )
            index, _, _ = await task_snapshot()
            await enrich_from_task(key, index)
        await mark_event_seen(event_id)
        await evaluate_all(reason="subscription.download_started")

    async def event_download_completed(event: DownloadCompleted) -> None:
        event_id = ctx.delivery.event_id if ctx.delivery else None
        if await event_seen(event_id):
            return
        key = norm_hash(event.info_hash)
        if key:
            await save_event_state(
                key, "download.completed",
                site_id=event.site_id, downloader_id=event.downloader_id,
                media_kind=getattr(event.media, "kind", None), media_item_id=getattr(event.media, "id", None),
                media_title=getattr(event.media, "title", None), torrent_name=event.title or event.download_name,
            )
            index, _, _ = await task_snapshot()
            await enrich_from_task(key, index)
        await mark_event_seen(event_id)
        await evaluate_all(reason="download.completed")

    async def event_ingest_completed(event: IngestCompleted) -> None:
        event_id = ctx.delivery.event_id if ctx.delivery else None
        if await event_seen(event_id):
            return
        hashes = [norm_hash(x) for x in (event.info_hashes or ()) if norm_hash(x)]
        index, _, _ = await task_snapshot()
        # 入库事件有精确 info_hash；再用条目关系补齐站点和下载器身份，避免只因
        # 插件晚启动、错过 download.completed 就把来源错误地当成“未知”。
        relations_by_hash = {}
        media_id = getattr(event.media, "id", None)
        if event.library_id is not None and media_id is not None:
            try:
                relation_view = await ops.call("library.items.relations", {
                    "library_id": event.library_id, "media_item_id": media_id,
                })
                if isinstance(relation_view, dict):
                    relations_by_hash = {
                        norm_hash(row.get("info_hash")): row
                        for row in (relation_view.get("torrents") or [])
                        if isinstance(row, dict) and norm_hash(row.get("info_hash"))
                    }
            except OpsError as exc:
                if exc.status != 404:
                    raise
                log.debug("入库事件对应的媒体条目关系暂不可读：media=%s", media_id)
        for key in hashes:
            old = await get_state(key) or {}
            task = index.get(key, {})
            relation = relations_by_hash.get(key, {})
            actual_library = event.library_id is not None and not bool(event.staging)
            old_source = str(old.get("source") or "unknown")
            source = old_source if old_source != "unknown" else (relation.get("source") or task.get("source"))
            state = merge_state(
                old, key, now_ts(), event_name="library.ingest.completed" if actual_library else None,
                site_id=old.get("site_id") or relation.get("site_id") or task.get("site_id"),
                downloader_id=old.get("downloader_id") or relation.get("downloader_id") or task.get("downloader_id"),
                media_kind=getattr(event.media, "kind", None), media_item_id=getattr(event.media, "id", None),
                media_title=getattr(event.media, "title", None),
                torrent_name=old.get("torrent_name") or relation.get("title") or task.get("name"),
                source=source, library_id=event.library_id, staging=bool(event.staging),
            )
            if relation.get("hit_and_run") is not None:
                state["hit_and_run"] = bool(relation.get("hit_and_run"))
            if relation.get("owned_by_movieclaw") is not None:
                state["owned_by_movieclaw"] = bool(relation.get("owned_by_movieclaw"))
            if event.staging or event.library_id is None:
                state["event_times"].setdefault("library.staging.completed", now_ts())
            await save_state(key, state)
        await mark_event_seen(event_id)
        await evaluate_all(reason="library.ingest.completed")

    async def event_download_imported(event: DownloadImported) -> None:
        event_id = ctx.delivery.event_id if ctx.delivery else None
        if await event_seen(event_id):
            return
        touched = []
        for item in event.items:
            media = item.media
            touched.extend(await relate_media_to_torrents(event.library_id, media.id, media, "download.imported"))
        await mark_event_seen(event_id)
        await evaluate_all(reason="download.imported", only_hashes=touched)

    async def event_items_arrived(event: ItemsArrived) -> None:
        event_id = ctx.delivery.event_id if ctx.delivery else None
        if await event_seen(event_id):
            return
        touched = []
        for item in event.items:
            media = item.media
            touched.extend(await relate_media_to_torrents(event.library_id, media.id, media, "library.items.arrived"))
        await mark_event_seen(event_id)
        await evaluate_all(reason="library.items.arrived", only_hashes=touched)

    async def event_files_added(event: FilesAdded) -> None:
        event_id = ctx.delivery.event_id if ctx.delivery else None
        if await event_seen(event_id):
            return
        # library.files.added 给出确切 file_id；通过 dl.file-sources.list 反查来源种子，
        # 不按标题猜关联。仅将与本次事件 file_id 相交的 info_hash 记入台账。
        files = [f for f in (event.files or ()) if as_int(getattr(f, "id", None)) is not None]
        file_ids = list(dict.fromkeys(as_int(f.id) for f in files if as_int(f.id) is not None))
        if not file_ids:
            await mark_event_seen(event_id)
            return
        rows = []
        for start in range(0, len(file_ids), 100):
            result = await ops.call("dl.file-sources.list", {"file_ids": ",".join(str(x) for x in file_ids[start:start + 100])})
            rows.extend(items_of(result))
        file_by_id = {as_int(f.id): f for f in files if as_int(f.id) is not None}
        task_index, _, _ = await task_snapshot()
        touched = []
        now = now_ts()
        for relation in rows:
            key = norm_hash(relation.get("info_hash"))
            if not key:
                continue
            related_ids = [as_int(x) for x in (relation.get("file_ids") or [])]
            exact_ids = [x for x in related_ids if x in file_by_id]
            if not exact_ids:
                continue
            file_ref = file_by_id[exact_ids[0]]
            media_id = as_int(getattr(file_ref, "media_item_id", None))
            old = await get_state(key) or {}
            task = task_index.get(key, {})
            source = old.get("source") if old.get("source") not in (None, "unknown") else (relation.get("source") or task.get("source"))
            state = merge_state(
                old, key, now, event_name="library.files.added",
                site_id=old.get("site_id") or relation.get("site_id") or task.get("site_id"),
                downloader_id=old.get("downloader_id") or relation.get("downloader_id") or task.get("downloader_id"),
                media_kind=old.get("media_kind") if old.get("media_kind") not in (None, "unknown") else task.get("media_kind"),
                media_item_id=old.get("media_item_id") or media_id or task.get("media_item_id"),
                media_title=old.get("media_title") or task.get("media_title"),
                torrent_name=old.get("torrent_name") or relation.get("title") or task.get("name"),
                source=source, library_id=event.library_id,
            )
            state["library_file_ids"] = list(dict.fromkeys([*(state.get("library_file_ids") or []), *exact_ids]))[-200:]
            if relation.get("hit_and_run") is not None:
                state["hit_and_run"] = bool(relation.get("hit_and_run"))
            if relation.get("owned_by_movieclaw") is not None:
                state["owned_by_movieclaw"] = bool(relation.get("owned_by_movieclaw"))
            state["staging"] = False
            await save_state(key, state)
            touched.append(key)
        await mark_event_seen(event_id)
        await evaluate_all(reason="library.files.added", only_hashes=touched)

    async def event_subscription_fulfilled(event: SubscriptionFulfilled) -> None:
        event_id = ctx.delivery.event_id if ctx.delivery else None
        if await event_seen(event_id):
            return
        index, _, task_error = await task_snapshot()
        if task_error:
            # 这是可靠事件：当前任务快照暂时不可用时抛错，让宿主重试，而不是吞掉触发信号。
            raise RuntimeError("读取下载任务失败，稍后重试：" + task_error)
        touched = []
        for key, task in index.items():
            if task.get("media_item_id") != event.media.id:
                continue
            subs = task.get("subscriptions") or []
            if not any(as_int(sub.get("id")) == event.subscription_id for sub in subs if isinstance(sub, dict)):
                continue
            state = merge_state(
                await get_state(key), key, now_ts(), event_name="subscription.fulfilled",
                site_id=task.get("site_id"), downloader_id=task.get("downloader_id"),
                media_kind=getattr(event.media, "kind", None), media_item_id=event.media.id,
                media_title=event.media.title, torrent_name=task.get("name"), source=task.get("source"),
            )
            await save_state(key, state)
            touched.append(key)

        # 再从条目关系补齐任务快照中没有的历史种子；只接受明确属于这条订阅的关系，
        # 且种子唯一或季集 units 与事件重合，避免把同一作品的其他版本误关联。
        libraries = items_of(await ops.call("library.list", {}))
        event_units = {(as_int(getattr(u, "season", None)), as_int(getattr(u, "episode", None))) for u in (event.units or ())}
        event_units.discard((None, None))
        for library in libraries:
            library_id = as_int(library.get("library_id"))
            if library_id is None:
                continue
            try:
                view = await ops.call("library.items.relations", {"library_id": library_id, "media_item_id": event.media.id})
            except OpsError as exc:
                if exc.status == 404:
                    continue
                raise
            if not isinstance(view, dict) or as_int(view.get("subscription_id")) != event.subscription_id:
                continue
            torrents = [x for x in (view.get("torrents") or []) if isinstance(x, dict) and norm_hash(x.get("info_hash"))]
            if len(torrents) == 1:
                selected = torrents
            elif event_units:
                selected = []
                for torrent in torrents:
                    units = set()
                    for unit in torrent.get("units") or []:
                        if isinstance(unit, (list, tuple)) and len(unit) >= 2:
                            units.add((as_int(unit[0]), as_int(unit[1])))
                    if units & event_units:
                        selected.append(torrent)
            else:
                selected = []
            for torrent in selected:
                key = norm_hash(torrent.get("info_hash"))
                if key in touched:
                    continue
                state = merge_state(
                    await get_state(key), key, now_ts(), event_name="subscription.fulfilled",
                    site_id=torrent.get("site_id"), downloader_id=torrent.get("downloader_id"),
                    media_kind=getattr(event.media, "kind", None), media_item_id=event.media.id,
                    media_title=event.media.title, torrent_name=torrent.get("title"), source=torrent.get("source"),
                    library_id=library_id,
                )
                await save_state(key, state)
                touched.append(key)
        await mark_event_seen(event_id)
        await evaluate_all(reason="subscription.fulfilled", only_hashes=touched)

    ctx.on(SUBSCRIPTION_DOWNLOAD_STARTED, event_download_started, id="subscription-download-started", durable=True)
    ctx.on(DOWNLOAD_COMPLETED, event_download_completed, id="download-completed", durable=True)
    ctx.on(LIBRARY_INGEST_COMPLETED, event_ingest_completed, id="library-ingest-completed", durable=True)
    ctx.on(LIBRARY_FILES_ADDED, event_files_added, id="library-files-added", durable=True)
    ctx.on(DOWNLOAD_IMPORTED, event_download_imported, id="download-imported", durable=True)
    ctx.on(LIBRARY_ITEMS_ARRIVED, event_items_arrived, id="library-items-arrived", durable=True)
    ctx.on(SUBSCRIPTION_FULFILLED, event_subscription_fulfilled, id="subscription-fulfilled", durable=True)

    async def emit_action(rule: dict, state: dict, site_name: str, action_label: str, result: str) -> bool:
        try:
            payload = RuleActionEvent(
                rule_name=str(rule.get("name") or "未命名规则"),
                action_label=action_label,
                torrent_name=str(state.get("torrent_name") or state.get("media_title") or state.get("info_hash") or "未知任务"),
                site_name=site_name or str(state.get("site_id") or "未知站点"),
                result=result,
                info_hash=norm_hash(state.get("info_hash")),
            )
            await publisher.publish(RULE_ACTION_EVENT, payload)
            return True
        except Exception:
            log.warning("发布策略联动通知失败", exc_info=True)
            return False

    async def execute_one(rule: dict, state: dict, task: dict | None, site_name: str, settings: dict) -> tuple[str, str]:
        action_type = rule["action"]["type"]
        info_hash = norm_hash(state.get("info_hash"))
        name = str(state.get("torrent_name") or state.get("media_title") or info_hash)
        rule_fp = rule_fingerprint(rule)
        latest_state = await get_state(info_hash)
        fired = dict((latest_state or {}).get("fired") or state.get("fired") or {})
        downloader_id_for_key = as_int((task or {}).get("downloader_id") or state.get("downloader_id"))
        daily_time = rule.get("daily_time") or ""
        schedule_date = datetime.fromtimestamp(now_ts()).astimezone().date().isoformat() if daily_time else None
        fired_key = f"{rule['id']}@{downloader_id_for_key or 0}@daily" if daily_time else f"{rule['id']}@{downloader_id_for_key or 0}"
        previous = fired.get(fired_key) if daily_time else (fired.get(fired_key) or fired.get(rule["id"]))
        if (isinstance(previous, dict) and previous.get("fingerprint") == rule_fp
                and (not daily_time or previous.get("schedule_date") == schedule_date)):
            return "already_done", "该规则今天已对这颗任务执行过" if daily_time else "该规则已对这颗种子执行过"

        if action_type == "notify":
            if settings["dry_run"]:
                await add_history({"rule_id": rule["id"], "rule_name": rule["name"], "info_hash": info_hash,
                                   "torrent_name": name, "site_id": state.get("site_id"), "action": action_type,
                                   "status": "dry_run", "message": "模拟：条件满足，将发送通知"})
                return "dry_run", "模拟：条件满足，将发送通知"
            published = await emit_action(rule, state, site_name, "规则命中", "条件已满足，已发送通知")
            if not published:
                message = "通知事件发布失败"
                await add_history({"rule_id": rule["id"], "rule_name": rule["name"], "info_hash": info_hash,
                                   "torrent_name": name, "site_id": state.get("site_id"), "action": action_type,
                                   "status": "error", "message": message})
                return "error", message
            fired[fired_key] = {"fingerprint": rule_fp, "at": now_ts(), "action": action_type, "schedule_date": schedule_date}
            state["fired"] = fired
            await save_state(info_hash, state)
            await add_history({"rule_id": rule["id"], "rule_name": rule["name"], "info_hash": info_hash,
                               "torrent_name": name, "site_id": state.get("site_id"), "action": action_type,
                               "status": "success", "message": "已发送通知"})
            return "success", "已发送通知"

        if action_type in GLOBAL_ACTIONS:
            downloader_id = as_int((task or {}).get("downloader_id") or state.get("downloader_id"))
            if downloader_id is None:
                message = "无法确定下载器 ID，不能修改下载器全局设置"
                await add_history({"rule_id": rule["id"], "rule_name": rule["name"], "info_hash": info_hash,
                                   "torrent_name": name, "site_id": state.get("site_id"), "action": action_type,
                                   "status": "blocked", "message": message})
                return "blocked", message
            try:
                current_limits = await ops.call("dl.limits", {"downloader_id": downloader_id})
                if not isinstance(current_limits, dict):
                    raise ValueError("宿主没有返回可用的下载器限制快照")
                update, description = build_global_limits_update(action_type, rule["action"], current_limits)
            except OpsError as exc:
                message = "读取下载器全局设置失败：" + exc.message
                await add_history({"rule_id": rule["id"], "rule_name": rule["name"], "info_hash": info_hash,
                                   "torrent_name": name, "site_id": state.get("site_id"), "action": action_type,
                                   "status": "error", "message": message})
                return "error", message
            except (ValueError, TypeError) as exc:
                message = "无法生成下载器全局设置计划：" + str(exc)
                await add_history({"rule_id": rule["id"], "rule_name": rule["name"], "info_hash": info_hash,
                                   "torrent_name": name, "site_id": state.get("site_id"), "action": action_type,
                                   "status": "error", "message": message})
                return "error", message
            message = "模拟：" + description
            if settings["dry_run"]:
                await add_history({"rule_id": rule["id"], "rule_name": rule["name"], "info_hash": info_hash,
                                   "torrent_name": name, "site_id": state.get("site_id"), "action": action_type,
                                   "status": "dry_run", "message": message})
                return "dry_run", message
            global_key = f"global-action:{rule['id']}:{downloader_id}"
            global_previous = await data(global_key, None)
            if (isinstance(global_previous, dict) and global_previous.get("fingerprint") == rule_fp
                    and (not daily_time or global_previous.get("schedule_date") == schedule_date)):
                return "already_done", "该规则今天已为此下载器应用过全局设置" if daily_time else "该规则已为此下载器应用过相同的全局设置"
            try:
                # 先读取当前完整快照，再只覆盖本动作涉及的字段，避免无意清除其他限速/队列设置。
                await ops.call("dl.limits.set", {"downloader_id": downloader_id, **update})
            except OpsError as exc:
                message = "修改下载器全局设置失败：" + exc.message
                await add_history({"rule_id": rule["id"], "rule_name": rule["name"], "info_hash": info_hash,
                                   "torrent_name": name, "site_id": state.get("site_id"), "action": action_type,
                                   "status": "error", "message": message})
                return "error", message
            await put(global_key, {"fingerprint": rule_fp, "at": now_ts(), "action": action_type, "update": update, "schedule_date": schedule_date})
            fired[fired_key] = {"fingerprint": rule_fp, "at": now_ts(), "action": action_type, "schedule_date": schedule_date}
            state["fired"] = fired
            await save_state(info_hash, state)
            message = "已应用下载器全局设置：" + description
            await add_history({"rule_id": rule["id"], "rule_name": rule["name"], "info_hash": info_hash,
                               "torrent_name": name, "site_id": state.get("site_id"), "action": action_type,
                               "status": "success", "message": message})
            await emit_action(rule, state, site_name, "已修改下载器全局设置", message)
            return "success", message

        if action_type == "replace_stalled_task":
            downloader_id = as_int((task or {}).get("downloader_id") or state.get("downloader_id"))
            source = norm_source((task or {}).get("source") or state.get("source"))
            task_state = str((task or {}).get("state") or "unknown").lower()
            if source != "subscription":
                message = "仅支持 MovieClaw 自己投递的订阅任务；当前来源不是订阅"
                await add_history({"rule_id": rule["id"], "rule_name": rule["name"], "info_hash": info_hash,
                                   "torrent_name": name, "site_id": state.get("site_id"), "action": action_type,
                                   "status": "blocked", "message": message})
                return "blocked", message
            if task_state != "stalled":
                message = f"当前任务状态为 {task_state}，不是可换源的停滞任务"
                await add_history({"rule_id": rule["id"], "rule_name": rule["name"], "info_hash": info_hash,
                                   "torrent_name": name, "site_id": state.get("site_id"), "action": action_type,
                                   "status": "blocked", "message": message})
                return "blocked", message
            if downloader_id is None or not info_hash:
                message = "缺少下载器 ID 或 info_hash，无法请求换源"
                await add_history({"rule_id": rule["id"], "rule_name": rule["name"], "info_hash": info_hash,
                                   "torrent_name": name, "site_id": state.get("site_id"), "action": action_type,
                                   "status": "blocked", "message": message})
                return "blocked", message
            if settings["dry_run"]:
                message = "模拟：会请求 MovieClaw 跨站寻找同规格替代源；旧任务会保留到新源产生真实进度"
                await add_history({"rule_id": rule["id"], "rule_name": rule["name"], "info_hash": info_hash,
                                   "torrent_name": name, "site_id": state.get("site_id"), "action": action_type,
                                   "status": "dry_run", "message": message})
                return "dry_run", message
            try:
                await ops.call("dl.torrent.replace", {"downloader_id": downloader_id, "info_hash": info_hash})
            except OpsError as exc:
                message = "请求订阅任务换源失败：" + exc.message
                await add_history({"rule_id": rule["id"], "rule_name": rule["name"], "info_hash": info_hash,
                                   "torrent_name": name, "site_id": state.get("site_id"), "action": action_type,
                                   "status": "error", "message": message})
                return "error", message
            fired[fired_key] = {"fingerprint": rule_fp, "at": now_ts(), "action": action_type, "schedule_date": schedule_date}
            state["fired"] = fired
            await save_state(info_hash, state)
            message = "已请求跨站寻找同规格替代源；旧任务会保留到新源产生真实进度"
            await add_history({"rule_id": rule["id"], "rule_name": rule["name"], "info_hash": info_hash,
                               "torrent_name": name, "site_id": state.get("site_id"), "action": action_type,
                               "status": "success", "message": message})
            return "success", message

        # 业务条件只由 rule_matches 中用户明确配置的 AND 条件决定。
        # 不隐式追加“必须已完成/已入库”或 H&R 条件；H&R 未选择即不参与判断。
        delete_files = action_type == "delete_torrent_with_files"
        downloader_id = as_int((task or {}).get("downloader_id") or state.get("downloader_id"))
        if downloader_id is None or not info_hash:
            message = "安全保护：无法确定下载器 ID 或 info_hash"
            await add_history({"rule_id": rule["id"], "rule_name": rule["name"], "info_hash": info_hash,
                               "torrent_name": name, "site_id": state.get("site_id"), "action": action_type,
                               "status": "blocked", "message": message})
            return "blocked", message
        try:
            preflight = await ops.call("dl.torrent.delete", {
                "downloader_id": downloader_id, "info_hash": info_hash,
                "delete_files": delete_files, "dry_run": True,
            })
        except OpsError as exc:
            message = "删种预演失败：" + exc.message
            await add_history({"rule_id": rule["id"], "rule_name": rule["name"], "info_hash": info_hash,
                               "torrent_name": name, "site_id": state.get("site_id"), "action": action_type,
                               "status": "error", "message": message})
            return "error", message
        if isinstance(preflight, dict) and preflight.get("exists") is False:
            message = "下载器中不存在对应任务，无需移除"
            await add_history({"rule_id": rule["id"], "rule_name": rule["name"], "info_hash": info_hash,
                               "torrent_name": name, "site_id": state.get("site_id"), "action": action_type,
                               "status": "miss", "message": message})
            return "miss", message
        if not isinstance(preflight, dict) or not preflight.get("exists"):
            message = "安全保护：无法确认下载器中是否存在该任务，未执行移除"
            await add_history({"rule_id": rule["id"], "rule_name": rule["name"], "info_hash": info_hash,
                               "torrent_name": name, "site_id": state.get("site_id"), "action": action_type,
                               "status": "blocked", "message": message})
            return "blocked", message
        requeued = preflight.get("requeued_units") or []
        requeue_ok, requeue_reason = check_requeue_condition(rule, requeued)
        if not requeue_ok:
            message = requeue_reason
            await add_history({"rule_id": rule["id"], "rule_name": rule["name"], "info_hash": info_hash,
                               "torrent_name": name, "site_id": state.get("site_id"), "action": action_type,
                               "status": "blocked", "message": message})
            return "blocked", message
        if settings["dry_run"]:
            message = "模拟：会移除下载器任务并同时删除本地数据文件" if delete_files else "模拟：会移除下载器任务并保留本地文件"
            if requeued:
                message += "；提示：移除后宿主报告可能触发订阅重新找源（本规则未限制此情况）"
            await add_history({"rule_id": rule["id"], "rule_name": rule["name"], "info_hash": info_hash,
                               "torrent_name": name, "site_id": state.get("site_id"), "action": action_type,
                               "status": "dry_run", "message": message})
            return "dry_run", message
        try:
            await ops.call("dl.torrent.delete", {
                "downloader_id": downloader_id, "info_hash": info_hash,
                "delete_files": delete_files, "dry_run": False,
            })
        except OpsError as exc:
            message = "实际移除失败：" + exc.message
            await add_history({"rule_id": rule["id"], "rule_name": rule["name"], "info_hash": info_hash,
                               "torrent_name": name, "site_id": state.get("site_id"), "action": action_type,
                               "status": "error", "message": message})
            return "error", message
        fired[fired_key] = {"fingerprint": rule_fp, "at": now_ts(), "action": action_type, "schedule_date": schedule_date}
        state["fired"] = fired
        await save_state(info_hash, state)
        message = "已移除任务及本地数据文件" if delete_files else "已移除任务，本地文件保留"
        if requeued:
            message += "；提示：宿主报告移除后可能触发订阅重新找源（本规则未限制此情况）"
        await add_history({"rule_id": rule["id"], "rule_name": rule["name"], "info_hash": info_hash,
                           "torrent_name": name, "site_id": state.get("site_id"), "action": action_type,
                           "status": "success", "message": message})
        await emit_action(rule, state, site_name, "已移除种子并删除文件" if delete_files else "已移除种子（保留文件）", message)
        return "success", message

    async def _evaluate_all_unlocked(reason: str = "timer", only_hashes: list[str] | None = None) -> dict:
        rules = [r for r in await rules_now() if r.get("enabled")]
        if not rules:
            result = {"scanned": 0, "matched": 0, "executed": 0, "dry_run": 0,
                      "blocked_or_failed": 0, "reason": reason, "message": "暂无启用规则", "at": now_ts()}
            await put("last_run", result)
            return result
        settings = await settings_now()
        task_index, tasks, task_error = await task_snapshot()
        site_ids, sites, site_error = await configured_sites()
        downloader_ids, downloaders, downloader_error = await configured_downloaders()
        states = await state_rows()
        state_by_hash = {norm_hash(row.get("info_hash")): row for row in states if norm_hash(row.get("info_hash"))}
        allowed = {norm_hash(x) for x in only_hashes} if only_hashes is not None else None
        now = now_ts()
        # 以全量下载器快照为主候选；历史台账里已不在下载器的任务仍可评估通知/状态规则，
        # 但删除动作会由宿主 dry-run 确认任务确实存在。
        candidates: list[tuple[dict, dict | None]] = []
        current_hashes = set()
        for task in tasks:
            key = norm_hash(task.get("info_hash"))
            if not key or (allowed is not None and key not in allowed):
                continue
            current_hashes.add(key)
            old = state_by_hash.get(key)
            state = merge_state(
                old, key, now, site_id=(task.get("site_id") or (old or {}).get("site_id")),
                downloader_id=task.get("downloader_id") or (old or {}).get("downloader_id"),
                media_kind=(task.get("media_kind") if task.get("media_kind") not in (None, "unknown") else (old or {}).get("media_kind")),
                media_item_id=task.get("media_item_id") or (old or {}).get("media_item_id"),
                media_title=task.get("media_title") or (old or {}).get("media_title"),
                torrent_name=task.get("name") or (old or {}).get("torrent_name"), source=task.get("source") or (old or {}).get("source"),
            )
            if old is None or state != old:
                await save_state(key, state)
            candidates.append((state, task))
        for state in states:
            key = norm_hash(state.get("info_hash"))
            if key and key not in current_hashes and (allowed is None or key in allowed):
                candidates.append((state, None))
        matches = []
        scanned = 0
        for state, task in candidates:
            key = norm_hash(state.get("info_hash"))
            if not key:
                continue
            scanned += 1
            for rule in rules:
                matched, why = rule_matches(rule, state, task, site_ids, now, downloader_ids)
                if not matched:
                    continue
                fired = (state.get("fired") or {}).get(f"{rule['id']}@{as_int((task or {}).get('downloader_id') or state.get('downloader_id')) or 0}") or (state.get("fired") or {}).get(rule["id"])
                fp = rule_fingerprint(rule)
                if isinstance(fired, dict) and fired.get("fingerprint") == fp:
                    # OR 策略按配置顺序短路：第一条条件命中的规则拥有该任务。
                    # 即使它已经执行过，也不能悄悄落到后面的规则执行另一种动作。
                    break
                site_id = str((task or {}).get("site_id") or state.get("site_id") or "")
                site_name = (sites.get(site_id) or {}).get("display_name") or site_id or "未知站点"
                downloader_id = as_int((task or {}).get("downloader_id") or state.get("downloader_id"))
                downloader_name = (downloaders.get(downloader_id) or {}).get("name") or str(downloader_id or "未知下载器")
                matches.append({"rule": rule, "state": state, "task": task, "site_name": site_name,
                                "downloader_name": downloader_name, "reason": why})
                break

        # 删除动作按（下载器、info_hash）去重；同一任务以规则配置顺序中的第一条命中规则为准。
        rule_order = {item["id"]: index for index, item in enumerate(rules)}
        delete_groups = {}
        notify_groups = []
        for match in matches:
            if match["rule"]["action"]["type"] in {"delete_torrent_keep_files", "delete_torrent_with_files"}:
                did = as_int((match["task"] or {}).get("downloader_id") or match["state"].get("downloader_id"))
                delete_groups.setdefault((did, norm_hash(match["state"].get("info_hash"))), []).append(match)
            else:
                notify_groups.append(match)
        executed = 0
        blocked = 0
        missed = 0
        dry = 0
        for match in notify_groups:
            status, _ = await execute_one(match["rule"], match["state"], match["task"], match["site_name"], settings)
            executed += status == "success"
            blocked += status in {"blocked", "error"}
            missed += status == "miss"
            dry += status == "dry_run"
        for (group_downloader_id, key), group in delete_groups.items():
            # 按规则配置顺序选择首条命中规则；去重只防止同一任务被重复操作，不改变用户选择的动作。
            primary = min(group, key=lambda item: rule_order.get(item["rule"]["id"], MAX_RULES))
            status, message = await execute_one(primary["rule"], primary["state"], primary["task"], primary["site_name"], settings)
            executed += status == "success"
            blocked += status in {"blocked", "error"}
            missed += status == "miss"
            dry += status == "dry_run"
            if status == "success":
                state = await get_state(key) or primary["state"]
                fired = dict(state.get("fired") or {})
                for other in group:
                    if other is primary:
                        continue
                    other_daily = bool(other["rule"].get("daily_time"))
                    other_key = f"{other['rule']['id']}@{group_downloader_id or 0}@daily" if other_daily else f"{other['rule']['id']}@{group_downloader_id or 0}"
                    other_date = datetime.fromtimestamp(now_ts()).astimezone().date().isoformat() if other_daily else None
                    fired[other_key] = {
                        "fingerprint": rule_fingerprint(other["rule"]), "at": now_ts(),
                        "action": primary["rule"]["action"]["type"], "deduped_with": primary["rule"]["id"],
                        "schedule_date": other_date,
                    }
                state["fired"] = fired
                await save_state(key, state)
                if len(group) > 1:
                    await add_history({"rule_id": primary["rule"]["id"], "rule_name": "、".join(x["rule"]["name"] for x in group),
                                       "info_hash": key, "torrent_name": state.get("torrent_name"), "site_id": state.get("site_id"),
                                       "action": primary["rule"]["action"]["type"], "status": "deduplicated",
                                       "message": f"{len(group)} 条规则命中同一任务；删种动作只执行一次"})
        matched_count = len(notify_groups) + len(delete_groups)
        result = {"scanned": scanned, "matched": max(0, matched_count - int(missed)), "missed": int(missed),
                  "executed": int(executed), "dry_run": int(dry),
                  "blocked_or_failed": int(blocked), "reason": reason, "task_snapshot_error": task_error,
                  "site_lookup_error": site_error, "downloader_lookup_error": downloader_error, "at": now_ts()}
        await put("last_run", result)
        return result

    # Event callbacks, the periodic task, and the manual endpoint can all request
    # an evaluation. Serialize them so a real action cannot race its own dedupe state.
    evaluation_lock = asyncio.Lock()

    async def evaluate_all(reason: str = "timer", only_hashes: list[str] | None = None) -> dict:
        async with evaluation_lock:
            return await _evaluate_all_unlocked(reason=reason, only_hashes=only_hashes)

    async def periodic_loop() -> None:
        while True:
            try:
                await evaluate_all(reason="timer")
                await prune_states()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("策略联动定时评估失败，下一轮会重试", exc_info=True)
            settings = await settings_now()
            try:
                await asyncio.sleep(settings["scan_interval_seconds"])
            except asyncio.CancelledError:
                raise

    ctx.task(periodic_loop(), name="strategy-linkage-evaluator")

    router = APIRouter()

    @router.get("/status", operation_id=f"plugins.{PLUGIN_ID}.status", summary="策略联动运行状态")
    async def status() -> dict:
        rules = await rules_now()
        states = await state_rows()
        return {"settings": await settings_now(), "rules_count": len(rules), "enabled_rules": sum(bool(r.get("enabled")) for r in rules),
                "tracked_torrents": len(states), "last_run": await data("last_run", {}),
                "history": (await data(HISTORY_KEY, []) or [])[:30]}

    @router.get("/rules", operation_id=f"plugins.{PLUGIN_ID}.rules.list", summary="读取策略联动规则")
    async def get_rules() -> dict:
        return {"rules": await rules_now(), "settings": await settings_now(), "events": EVENT_CHOICES,
                "elapsed_choices": ELAPSED_CHOICES, "media_kinds": sorted(MEDIA_KINDS),
                "sources": sorted(SOURCES), "task_states": sorted(TASK_STATES), "actions": sorted(ACTIONS)}

    @router.put("/rules", operation_id=f"plugins.{PLUGIN_ID}.rules.save", summary="保存策略联动规则")
    async def save_rules(payload: dict) -> dict:
        raw_rules = payload.get("rules") if isinstance(payload, dict) else None
        if not isinstance(raw_rules, list):
            raise HTTPException(status_code=400, detail="rules 必须是数组")
        if len(raw_rules) > MAX_RULES:
            raise HTTPException(status_code=400, detail=f"最多支持 {MAX_RULES} 条规则")
        try:
            rules = [validate_rule(item) for item in raw_rules]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        ids = [r["id"] for r in rules]
        if len(ids) != len(set(ids)):
            raise HTTPException(status_code=400, detail="规则 ID 重复")
        if len(json.dumps(rules, ensure_ascii=False).encode("utf-8")) > 56000:
            raise HTTPException(status_code=400, detail="规则配置过大，请减少规则数量或每条规则的站点数")
        await put(RULES_KEY, rules)
        log.info("策略联动规则已保存：%d 条（启用 %d 条）", len(rules), sum(bool(r["enabled"]) for r in rules))
        return {"ok": True, "rules": rules, "message": "规则已保存；规则之间 OR，单条规则内部 AND"}

    @router.get("/sites", operation_id=f"plugins.{PLUGIN_ID}.sites", summary="读取 MovieClaw 已配置的 PT 站点")
    async def get_sites() -> dict:
        ids, sites, error = await configured_sites()
        if error:
            raise HTTPException(status_code=503, detail=f"读取已配置站点失败：{error}")
        return {"sites": list(sites.values()), "count": len(ids)}

    @router.get("/downloaders", operation_id=f"plugins.{PLUGIN_ID}.downloaders", summary="读取 MovieClaw 已配置的下载器")
    async def get_downloaders() -> dict:
        ids, downloaders, error = await configured_downloaders()
        if error:
            raise HTTPException(status_code=503, detail=f"读取已配置下载器失败：{error}")
        return {"downloaders": list(downloaders.values()), "count": len(ids)}

    @router.put("/settings", operation_id=f"plugins.{PLUGIN_ID}.settings.save", summary="保存策略联动全局设置")
    async def save_settings(payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="设置格式不正确")
        current = await settings_now()
        if "dry_run" in payload:
            current["dry_run"] = bool(payload["dry_run"])
        if "scan_interval_seconds" in payload:
            interval = as_int(payload["scan_interval_seconds"])
            if interval is None or not 30 <= interval <= 3600:
                raise HTTPException(status_code=400, detail="扫描间隔必须在 30～3600 秒之间")
            current["scan_interval_seconds"] = interval
        await put(SETTINGS_KEY, current)
        return {"ok": True, "settings": current, "message": "全局设置已保存"}

    @router.get("/preview", operation_id=f"plugins.{PLUGIN_ID}.preview", summary="模拟评估当前任务并说明命中与未命中原因；不执行动作")
    async def preview() -> dict:
        # 预览必须直接读取当前下载器任务，而不能只看已经有事件台账的任务。
        # 对尚未跟踪的存量任务只创建临时快照，不伪造下载完成/入库等历史事件，也不写入台账。
        all_rules = await rules_now()
        rules = [r for r in all_rules if r.get("enabled")]
        task_index, task_items, task_error = await task_snapshot()
        site_ids, sites, site_error = await configured_sites()
        downloader_ids, downloaders, downloader_error = await configured_downloaders()
        states = await state_rows()
        state_by_hash = {norm_hash(s.get("info_hash")): s for s in states if norm_hash(s.get("info_hash"))}
        now = now_ts()
        task_items = task_items[:MAX_STATES]
        candidates = []
        for task in task_items:
            key = norm_hash(task.get("info_hash"))
            if not key:
                continue
            old = state_by_hash.get(key)
            state = merge_state(
                old, key, now,
                site_id=task.get("site_id") or (old or {}).get("site_id"),
                downloader_id=task.get("downloader_id") or (old or {}).get("downloader_id"),
                media_kind=task.get("media_kind") if task.get("media_kind") not in (None, "unknown") else (old or {}).get("media_kind"),
                media_item_id=task.get("media_item_id") or (old or {}).get("media_item_id"),
                media_title=task.get("media_title") or (old or {}).get("media_title"),
                torrent_name=task.get("name") or (old or {}).get("torrent_name"),
                source=task.get("source") or (old or {}).get("source"),
            )
            candidates.append((state, task))

        matches = []
        rule_diagnostics = []
        selected_match_tasks = set()
        for rule in rules:
            reasons: dict[str, int] = {}
            matched_count = 0
            for state, task in candidates:
                key = norm_hash(state.get("info_hash"))
                matched, why = rule_matches(rule, state, task, site_ids, now, downloader_ids)
                if not matched:
                    reasons[why] = reasons.get(why, 0) + 1
                    continue
                matched_count += 1
                # 诊断统计每条规则；匹配任务列表只展示配置顺序中的第一条命中规则，
                # 与真实执行的 OR 短路保持一致。
                candidate_identity = (as_int((task or {}).get("downloader_id") or state.get("downloader_id")) or 0, key)
                if candidate_identity in selected_match_tasks:
                    continue
                selected_match_tasks.add(candidate_identity)
                action = rule["action"]["type"]
                blocked_reason = None
                miss_reason = None
                site_id = str(state.get("site_id") or (task or {}).get("site_id") or "")
                plan_message = ""
                downloader_id = as_int((task or {}).get("downloader_id") or state.get("downloader_id"))
                if action in GLOBAL_ACTIONS:
                    if downloader_id is None:
                        blocked_reason = "无法确定下载器 ID，不能修改下载器全局设置"
                    else:
                        try:
                            current_limits = await ops.call("dl.limits", {"downloader_id": downloader_id})
                            if not isinstance(current_limits, dict):
                                blocked_reason = "宿主没有返回可用的下载器限制快照"
                            else:
                                _, description = build_global_limits_update(action, rule.get("action", {}), current_limits)
                                plan_message = "预检计划：" + description
                                current_settings = await settings_now()
                                if current_settings.get("dry_run", True):
                                    plan_message += "。当前仍是模拟模式"
                        except OpsError as exc:
                            blocked_reason = "读取下载器全局设置失败：" + exc.message
                        except (ValueError, TypeError) as exc:
                            blocked_reason = "无法生成下载器全局设置计划：" + str(exc)
                elif action == "replace_stalled_task":
                    source = norm_source((task or {}).get("source") or state.get("source"))
                    task_state = str((task or {}).get("state") or "unknown").lower()
                    if source != "subscription":
                        blocked_reason = "仅支持 MovieClaw 自己投递的订阅任务"
                    elif task_state != "stalled":
                        blocked_reason = f"当前任务状态为 {task_state}，不是可换源的停滞任务"
                    elif downloader_id is None or not key:
                        blocked_reason = "缺少下载器 ID 或 info_hash，无法请求换源"
                    else:
                        plan_message = "预检计划：会请求跨站寻找同规格替代源；旧任务保留到新源产生真实进度"
                elif action in {"delete_torrent_keep_files", "delete_torrent_with_files"}:
                    if downloader_id is None:
                        blocked_reason = "无法确定下载器 ID"
                    elif not norm_hash(state.get("info_hash")):
                        blocked_reason = "缺少 info_hash，无法定位下载器任务"
                    else:
                        try:
                            delete_files = action == "delete_torrent_with_files"
                            preflight = await ops.call("dl.torrent.delete", {
                                "downloader_id": downloader_id, "info_hash": norm_hash(state.get("info_hash")),
                                "delete_files": delete_files, "dry_run": True,
                            })
                            if isinstance(preflight, dict) and preflight.get("exists") is False:
                                miss_reason = "下载器中不存在对应任务，无需移除"
                            elif not isinstance(preflight, dict) or not preflight.get("exists"):
                                blocked_reason = "安全校验无法确认下载器中是否存在该任务"
                            else:
                                requeued = preflight.get("requeued_units") or []
                                requeue_ok, requeue_reason = check_requeue_condition(rule, requeued)
                                if not requeue_ok:
                                    blocked_reason = requeue_reason
                                else:
                                    plan_message = "宿主预检通过：任务存在；移除种子并删除本地数据文件" if delete_files else "宿主预检通过：任务存在；移除种子并保留本地文件"
                                    if requeued:
                                        plan_message += "；提示：移除后可能触发订阅重新找源（本规则未限制此情况）"
                                    current_settings = await settings_now()
                                    if current_settings.get("dry_run", True):
                                        plan_message += "。当前仍是模拟模式"
                        except OpsError as exc:
                            blocked_reason = "宿主删种预演失败：" + exc.message
                        except Exception as exc:
                            blocked_reason = "宿主删种预演失败：" + str(exc)
                matches.append({
                    "rule_id": rule["id"], "rule_name": rule["name"], "info_hash": key,
                    "torrent_name": state.get("torrent_name") or state.get("media_title") or key,
                    "site_id": site_id,
                    "site_name": (sites.get(site_id) or {}).get("display_name") or site_id or "未知站点",
                    "downloader_id": downloader_id,
                    "downloader_name": (downloaders.get(downloader_id) or {}).get("name") or (task or {}).get("downloader_name") or str(downloader_id or "未知下载器"),
                    "media_kind": norm_kind(state.get("media_kind") or (task or {}).get("media_kind")), "action": action,
                    "action_config": dict(rule.get("action") or {}),
                    "conditions": why, "blocked_reason": blocked_reason, "miss_reason": miss_reason,
                    "plan_message": plan_message,
                })
            rule_diagnostics.append({
                "rule_id": rule["id"], "rule_name": rule["name"], "match_count": matched_count,
                "candidate_count": len(candidates),
                "reasons": [{"reason": reason, "count": count} for reason, count in sorted(reasons.items(), key=lambda x: (-x[1], x[0]))[:5]],
            })

        event_stats = []
        for event_name, label in EVENT_CHOICES.items():
            times = []
            for state in states:
                event_times = state.get("event_times") if isinstance(state.get("event_times"), dict) else {}
                stamp = as_float(event_times.get(event_name))
                if stamp is not None:
                    times.append(stamp)
            event_stats.append({"event": event_name, "label": label, "count": len(times), "last_at": max(times) if times else None})

        notes = []
        if not rules:
            notes.append("当前没有启用规则。请先启用至少一条规则，再运行模拟。")
        if task_error:
            notes.append("读取下载器任务失败：" + task_error)
        if site_error:
            notes.append("读取 PT 站点失败：" + site_error)
        if downloader_error:
            notes.append("读取下载器配置失败：" + downloader_error)
        if not task_error and not task_items:
            notes.append("MovieClaw 当前没有返回可评估的下载器任务。请检查下载器连接与任务列表。")
        if not states:
            notes.append("还没有事件台账。当前下载器任务仍会参与临时模拟，但临时快照不会伪造过去发生的下载完成或入库事件。")
        if rules and candidates and not matches:
            notes.append("没有规则命中时，请看下面每条规则的“未命中原因”。必需事件只认插件实际采集到的事件；安装插件之前发生的事件不会从当前状态猜测补造。")
        needs_completed_event = any(
            "download.completed" in (rule.get("required_events") or []) for rule in rules
        )
        has_completed_snapshot_without_event = any(
            (task or {}).get("state") == "completed"
            and not (state.get("event_times") or {}).get("download.completed")
            for state, task in candidates
        )
        if needs_completed_event and has_completed_snapshot_without_event:
            notes.append("注意：下载完成条件也会匹配当前下载器状态为“已完成”的存量任务；这类匹配不会计入历史事件采集数量，也不会伪造下载完成时间戳。")
        if len(task_index) > MAX_STATES:
            notes.append(f"当前任务超过 {MAX_STATES} 项，本次预览最多评估前 {MAX_STATES} 项。")

        return {
            "matches": matches[:500], "match_count": len(matches), "tracked_count": len(states),
            "tasks_count": len(task_index), "evaluated_tasks_count": len(candidates),
            "enabled_rules_count": len(rules), "rule_diagnostics": rule_diagnostics,
            "event_stats": event_stats, "notes": notes,
            "task_snapshot_error": task_error, "site_lookup_error": site_error,
            "downloader_lookup_error": downloader_error,
            "dry_run": True, "at": now,
        }

    @router.post("/run", operation_id=f"plugins.{PLUGIN_ID}.run", summary="立即评估一次全部策略")
    async def run_now() -> dict:
        return await evaluate_all(reason="manual")

    @router.get("/history", operation_id=f"plugins.{PLUGIN_ID}.history", summary="读取策略联动执行记录")
    async def history() -> dict:
        return {"history": await data(HISTORY_KEY, []) or [], "last_run": await data("last_run", {}) or {}}

    @router.post("/history/clear", operation_id=f"plugins.{PLUGIN_ID}.history.clear", summary="清空策略联动执行日志")
    async def clear_history() -> dict:
        old_history = await data(HISTORY_KEY, [])
        cleared = len(old_history) if isinstance(old_history, list) else 0
        await put(HISTORY_KEY, [])
        await put("last_run", {})
        return {"ok": True, "cleared": cleared}

    ctx.use(PLUGIN_ROUTES).mount(ctx, router)
    log.info("策略联动已启动；规则间 OR、单条规则内 AND；默认模拟执行，关闭模拟即执行规则动作")
