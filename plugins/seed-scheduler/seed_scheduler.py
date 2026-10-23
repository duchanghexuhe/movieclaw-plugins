"""种子做种定时清理：给下载器里的种子设「做种多久」，到点自动删任务。

流程：定时任务（默认每 10 分钟，可在「设置 → 定时任务」里改）扫描下载器里的全部种子，
按 **qBittorrent 自己记的时间**（已完成的种子只认累计做种秒数与下载完成时刻，`added_on`
只在两者都缺失时兜底）算每个种子已经做种多久。到点、且没有安全线挡着的种子，直接从下载器删掉——
**不依赖 MovieClaw 入库**，只把它当下载器用也照样工作。

安全线（任何一条命中都不删，只提醒）：
- 站点有 H&R 考核、或做种时长没到站点要求的最低时长（H&R 从下载器标签或任务中心取）；
- 还在下载 / 做种没完成，或下载器正在校验；
- 订阅刚投递、还没拿到这个种子里的内容（删了会被打回缺资源重下）——可在设置里关；
- 这个种子还供着媒体库里别的条目 / 没识别的文件（合集、季包，删了连带毁别的片）；
- 刷流（分享率自动提升）引擎登记的种子——由它自己汰换；
- 正在播放、或最近 4 小时内打开过这部片（可在设置里关）；
- 带「免清理 / noreap」标签的种子，永不处理。

「库里有没有这份文件」靠插件自己的入库台账：监听宿主的 ``library.ingest.completed``
（自带 ``info_hashes`` 与 ``files``）记下「哪个种子入库了哪些文件、属于哪个条目」。
宿主的 ``dl.tasks`` 并不返回 ``file_ids``，早前按它取文件的做法永远取不到东西，
等于保护形同失效。

删种走宿主的 ``dl.torrent.delete``：订阅工单、任务中心、订阅状态会跟着同步，
不会留下悬空记录。qBittorrent 自己从 v4.6 起已移除「做种限时」，所以由插件定时对账。

**默认是演练模式**（``dry_run=True``），且默认不删数据文件、不处理「文件已丢失」——
这个插件的删除不可逆，先跑几轮看过 preview 再放开。
"""

import asyncio
from datetime import datetime, timezone
from urllib.parse import urlsplit

import pydantic
from fastapi import APIRouter, HTTPException
from pydantic import Field, SecretStr, field_validator

from movieclaw_api.plugins.keys import PLUGIN_DATA, PLUGIN_ROUTES
from movieclaw_scheduler import SCHEDULED_TASKS, TaskDefinition, TriggerType
from movieclaw_sdk import Context, plugin
from movieclaw_sdk.events import (
    DOWNLOAD_COMPLETED,
    LIBRARY_INGEST_COMPLETED,
    LIBRARY_ITEM_VIEWED,
    PLAYBACK_STARTED,
    PLUGIN_EVENTS,
)
from movieclaw_sdk.events.plugin import plugin_event
from movieclaw_sdk.jobs import JOB_HANDLERS, PLUGIN_JOBS, JobBlocked, RegisteredJobHandler
from movieclaw_sdk.ops import HOST_OPS, OpsError

PLUGIN_ID = "seed-scheduler"
SCAN_KEY = f"{PLUGIN_ID}.scan"
SWEEP_JOB = f"{PLUGIN_ID}:sweep"

SEEN_KEY = "seen"        # {infohash: 插件第一次看到它的时间戳}
REMOVED_KEY = "removed"  # {infohash: 删掉的时间戳}——删了之后不再重记首见时间
HISTORY_KEY = "history"  # 最近的处理记录（给插件接口和网页看）
OPEN_KEY = "opened"      # {media_item_id: {at: 打开时刻, until: 保护到什么时候}}
STATS_KEY = "stats"      # 累计计数
LEDGER_KEY = "ingested"  # {infohash: {files, library_id, media_item_id, units, at}}

# PLUGIN_DATA 单个值有 64 KB 上限。一个 infohash 在 JSON 里约 45 字节，
# 1000 条 ≈ 44 KB，留够余量；超了 store.set 会直接失败，那时 seen 就再也写不进去，
# 所有种子都会被当成「刚见到」重新起算——所以绝不能像原来那样设 20000 条。
SEEN_LIMIT = 1000
# 一条处理记录约 200～250 字节，50 条约 12 KB
HISTORY_SIZE = 50
# 入库台账：一条约 120 字节，400 条 ≈ 48 KB
LEDGER_LIMIT = 400

# qBittorrent 里直接可见的标签：用户可以在下载器里加上它把某个种子排除在外
TAG_KEEP = "免清理"
KEEP_TAGS = {"免清理", "noreap", "seed-keep"}
# H&R 只能靠下载器标签认：dl.tasks 的 subscriptions[] 里并没有 hit_and_run 这个字段，
# 早前从那儿读，结果恒为 False，H&R 保护从来没生效过。
HR_TAGS = {"hit&run", "hitandrun", "hit_and_run", "h&r", "hr", "hr考核", "考核"}

# 订阅工单里「还在等这个种子」的状态：删了会把对应季集打回缺资源重下
_WAITING = {"grabbed", "downloaded", "replacement_pending", "trial"}
# 已经从库里删掉、不该再挡着的库文件状态
_IMPORTED = {"imported", "completed", "done", "succeeded", "fulfilled"}
_UNKNOWN_STATUS_KEEP = True   # 状态看不明白时保守：当成「还要用它」
_GONE_FILE_STATES = {"trashed", "purged", "deleted", "missing", "removed"}

UNFINISHED_GRACE_DAYS = 7   # 首次见到仍未下完的任务，最多赖这么久
# 最近打开过这部片，多久内不清理。原来只有 30 分钟，一部两小时的片看到后半段
# 就已经出了保护窗口，随时可能被删；现在按「一部片 + 一段余量」给到 4 小时。
WEB_IDLE_MINUTES = 240
# 一轮最多删多少个，防止一次改动在几秒内删光上千个任务
MAX_PER_RUN = 20


# ---------------------------------------------------------------------- 设置


class Settings(pydantic.BaseModel):
    """插件「设置」表单。"""

    seed_hours: float = Field(
        72,
        ge=1,
        le=8760,
        title="单种做种时长（小时）",
        description="从 qBittorrent 记录的做种起点算起。到点、且没有安全线挡着的，自动从下载器删除",
    )
    overdue_days: float = Field(
        3,
        ge=0,
        le=90,
        title="没入库的种子再等几天后提醒（天）",
        description="手动下载、一直没入库的种子不删除；超过「做种时长 + 这么多天」时提醒你处理",
    )
    protect_unwatched: bool = Field(
        False,
        title="库里的还没看完就先保留",
        description="打开后：种子挂着的订阅季集里还有没看完的集，就先不清理（默认关：纯当下载器用时不看观看进度）",
    )
    protect_recent_open: bool = Field(
        True,
        title="最近打开过的先保留",
        description=f"最近 {WEB_IDLE_MINUTES // 60} 小时有人在网页上打开过、或正在播放这部片，就先保留",
    )
    respect_hr: bool = Field(
        True,
        title="带 H&R 考核的种子多保一段",
        description="有 H&R 的种子至少做种到下面这个时长才删（站点具体考核时长 MovieClaw 拿不到）",
    )
    hr_min_hours: float = Field(
        72,
        ge=1,
        le=8760,
        title="H&R 种子最短做种时长（小时）",
        description="站点有 H&R 考核时按它兜底：常见站点的考核是 72 小时、分享率 1.0 以上",
    )
    respect_boost: bool = Field(
        True,
        title="不碰刷流引擎的种子",
        description="分享率自动提升（刷流）引擎登记的种子由它自己汰换，本插件不删",
    )
    dry_run: bool = Field(
        True,
        title="只记录不删除（演练）",
        description=(
            "默认开：只把「本来会删什么」写进记录，不动下载器。"
            "先跑几轮、看过 preview 的结果再关掉——这个插件删东西是不可逆的"
        ),
    )
    delete_files: bool = Field(
        False,
        title="删除时连数据文件一起删",
        description=(
            "默认关：只从下载器移除任务、磁盘文件留着（内容还在，随时能重新做种）。"
            "打开后删除会**真的抹掉数据**，不可恢复"
        ),
    )

    qb_url: str = Field(
        "",
        title="qBittorrent WebUI 地址",
        description=(
            "插件要扫「下载器里的全部种子」，而任务中心只列还在跑的任务，所以需要直连一次 QB。"
            "填 http://10.10.10.100:8085 这样；留空则只用任务中心的口径（只看到还在跑的任务）"
        ),
    )
    qb_username: str = Field("", title="qBittorrent 用户名", description="未开鉴权可留空")
    qb_password: SecretStr | None = Field(None, title="qBittorrent 密码", description="加密保存，界面不回显")

    handle_seeding: bool = Field(
        True,
        title="处理·正在做种（上传中 / 已停做种）",
        description="做种满设定时长的种子里，这一类要不要处理。关掉则它们永不自动删除",
    )
    handle_missing_files: bool = Field(
        False,
        title="处理·文件已丢失（missingFiles）",
        description=(
            "QB 找不到文件、做不了种的种子。默认关：挂载一抖全部种子都会变成 missingFiles，"
            "这时批量删除就把好好的任务全清了。确认是残留再打开"
        ),
    )
    handle_downloading: bool = Field(
        False,
        title="处理·仍在下载（下载中 / 排队 / 停滞）",
        description="到点还没下完的种子。默认不碰——还在下的种子删了等于放弃这次下载",
    )
    handle_errored: bool = Field(
        False,
        title="处理·出错（error）",
        description="QB 报错的种子。默认不碰，等你人工看一眼",
    )
    handle_checking: bool = Field(
        False,
        title="处理·校验中（checking / 分配空间）",
        description="正在校验文件的种子。默认不碰，校验完下一轮自然归到做种 / 出错",
    )
    handle_unknown: bool = Field(
        False,
        title="处理·其他未知状态",
        description="QB 新旧版本状态词对不上时用。默认不碰，宁可漏删也不误删",
    )

    library_required: bool = Field(
        False,
        title="只清理媒体库里已有这份文件的种子",
        description=(
            "默认关：做种满设定的时长，不论有没有入库都删（适合只把 MovieClaw 当下载器用）。"
            "打开后更保守：没入库的种子一律不删，只提醒"
        ),
    )
    keep_waiting_subscriptions: bool = Field(
        True,
        title="订阅还等着用的种子先保留",
        description=(
            "MovieClaw 订阅刚投递、还没拿到文件的种子（正在下的），到点也先留着，"
            "免得订阅被打回缺资源反复重下。用不到订阅可以关掉"
        ),
    )

    @field_validator("seed_hours")
    @classmethod
    def _at_least_one_hour(cls, value: float) -> float:
        if value < 1:
            raise ValueError("做种时长至少 1 小时")
        return value


# ---------------------------------------------------------------------- 纯函数（可单独自测）


def items_of(payload) -> list:
    """``dl.tasks`` 的返回可能被包成 {items: [...]} 或直接是列表。"""
    if isinstance(payload, dict):
        return list(payload.get("items") or [])
    if isinstance(payload, list):
        return list(payload)
    return []


def num(value, default=0.0) -> float:
    """稳妥地把接口里的数字转成 float；None、字符串、布尔都不炸。"""
    if value is None or isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_time(value) -> datetime | None:
    """把接口返回的时间解析成 UTC datetime；解析不了返回 None。"""
    if not value:
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def hash_key(value) -> str:
    """info_hash 是唯一键，统一小写（工具与下载器的大小写不一定一致）。"""
    return str(value or "").strip().lower()


def _url_host(value: str) -> str:
    """``http://10.0.0.2:8080/qb`` → ``10.0.0.2:8080``：只留能认定「同一台机器」的部分。

    故意忽略用户名、路径、协议与末尾斜杠；8080 是 http 的默认端口，省略时按 80 补。
    比对下载器和设置的 QB 地址时用它，避免把一台 QB 的种子当成另一台的任务。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = f"http://{text}"
    try:
        parsed = urlsplit(text)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return ""
    if not host:
        return ""
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return f"{host}:{port}"


def has_keep_tag(tags) -> bool:
    """任务是否被标记为「永不清理」。"""
    wanted = {tag.lower() for tag in KEEP_TAGS}
    return any(str(tag).strip().lower() in wanted for tag in tags or ())


def has_hr_tag(tags) -> bool:
    """任务是否被标记为「站点有 H&R 考核」。

    H&R 只能从下载器标签认：``dl.tasks`` 的 ``subscriptions[]`` 里没有这个字段，
    从这里读恒为 False，等于 H&R 保护从来没生效过。
    """
    wanted = HR_TAGS
    return any(str(tag).strip().lower() in wanted for tag in tags or ())


def file_ref(raw) -> dict:
    """``library.ingest.completed`` 里 ``files[]`` 的一项 → 台账里的一行。

    里层字段名宿主改过几次（``id`` / ``file_id`` / ``media_file_id`` 都出现过），取值
    做兼容；一个 id 都认不出来的条目直接扔掉——宁可台账少一条，也不要记一条永远对不上
    库文件的假账。
    """
    if not isinstance(raw, dict):
        return {}
    raw_id = raw.get("id") or raw.get("file_id") or raw.get("media_file_id")
    try:
        file_id = int(raw_id)
    except (TypeError, ValueError):
        return {}
    if file_id <= 0:
        return {}
    try:
        media_item_id = int(raw.get("media_item_id"))
    except (TypeError, ValueError):
        media_item_id = None
    unit = raw.get("unit") or raw.get("units")
    return {
        "file_id": file_id,
        "media_item_id": media_item_id,
        "season": int(num(raw.get("season"))),
        "episode": int(num(raw.get("episode"))),
        "unit_count": len(unit) if isinstance(unit, (list, tuple)) else 0,
        "state": str(raw.get("status") or raw.get("state") or "").lower(),
        "gone": str(raw.get("status") or raw.get("state") or "").lower() in _GONE_FILE_STATES,
    }


# QB 状态词 → 本插件的归类。qBittorrent 4.x 与 5.x 用词不同（pausedUP / stoppedUP、
# completed / uploading），两边都收；认不出的归 other，由「处理·其他未知状态」决定。
_STATE_GROUPS = {
    "seeding": {
        "uploading", "stalledup", "forcedup", "queuedup", "pausedup", "stoppedup",
        "completed", "seeding", "forcedseeding", "queuedseeding",
    },
    "missing": {"missingfiles"},
    "downloading": {
        "downloading", "metadl", "forceddl", "stalleddl", "queueddl", "allocating",
        "pauseddl", "stoppeddl", "downloadingmetadata",
    },
    "errored": {"error"},
    "checking": {"checkingdl", "checkingup", "checkingresumedata", "moving"},
}
GROUP_TITLES = {
    "seeding": "正在做种",
    "missing": "文件已丢失",
    "downloading": "仍在下载",
    "errored": "出错",
    "checking": "校验中",
    "other": "其他状态",
}


def state_group(brief: dict) -> str:
    """把这个种子归到哪一类（见 ``_STATE_GROUPS``）。

    先看 QB 的原始状态词；状态词认不出时退回「进度是否 100%」——
    下完了却认不出状态，只可能是做种之类，不能当「还在下载」放过。
    """
    raw = str(brief.get("state") or "").strip().lower()
    for group, words in _STATE_GROUPS.items():
        if raw in words:
            return group
    if not raw:
        return "seeding" if brief.get("completed") else "downloading"
    return "other"


def group_allowed(group: str, config: Settings) -> bool:
    """这一类要不要处理（对应设置里的几个勾选）。"""
    return {
        "seeding": config.handle_seeding,
        "missing": config.handle_missing_files,
        "downloading": config.handle_downloading,
        "errored": config.handle_errored,
        "checking": config.handle_checking,
        "other": config.handle_unknown,
    }.get(group, False)


def seeded_hours(brief: dict, first_seen: float, now: float) -> float:
    """这个种子已经做种多久（小时）。**以 QB 自己的记录为准**，不从插件装上的时刻起算。

    已完成（做种中 / 已暂停）的任务只认两个信号：

    - ``seeding_seconds``：QB 记的累计做种秒数（暂停的时间不算，最准）；
    - ``completed_on``：下载完成的时刻，之后一直在做种。

    未完成的任务（下载中 / 出错 / 校验中）才用 ``added_on``：那种情况下"做种"根本
    还没开始，只能按"待在下载器里多久"算宽限期。把 ``added_on`` 也算进已完成的种子
    会高估做种时长——一个下了三天才下完的种子，刚做完就被判成"已做种三天"删掉。

    取最长是为了容错：QB 重启、重新校验会把累计秒数归零，偶尔也会重置完成时间，
    取最长才不会把熬了半个月的老种子判成刚下的、白等一轮。插件自己的首见时间只在
    这些信号全都缺失时兜底。
    """
    candidates: list[float] = []
    seconds = num(brief.get("seeding_seconds"), -1.0)
    if seconds > 0:
        candidates.append(seconds / 3600.0)
    completed_on = num(brief.get("completed_on"), 0.0)
    if completed_on > 0:
        candidates.append(max(0.0, (now - completed_on) / 3600.0))
    if not candidates:
        # 还没下完（或 QB 没给这两个字段）：退回"进了下载器多久"，仅用于宽限期判断
        added_on = num(brief.get("added_on"), 0.0)
        if added_on > 0:
            candidates.append(max(0.0, (now - added_on) / 3600.0))
    if candidates:
        return round(max(candidates), 3)
    if first_seen <= 0:
        return 0.0
    return max(0.0, (now - first_seen) / 3600.0)


def seeded_start(brief: dict, first_seen: float, now: float) -> float:
    """做种起点（秒级时间戳），给「什么时候该删」用；和 :func:`seeded_hours` 同一个口径。"""
    hours = seeded_hours(brief, first_seen, now)
    if hours <= 0:
        return 0.0
    return now - hours * 3600.0


def removal_blocker(
    brief: dict,
    facts: dict,
    config: Settings,
    *,
    first_seen: float,
    now: float,
) -> str | None:
    """这个任务现在**不能删**的原因；能删返回 None。只看种子自己的事实。

    ``facts``：``group``（状态归类）/ ``watching`` / ``boost`` / ``hr``。
    「该不该删」「库里有没有」「订阅还等不等」由调用方判断。
    """
    if has_keep_tag(brief.get("tags")):
        return "带「免清理」标签"
    group = str(facts.get("group") or "other")
    if not group_allowed(group, config):
        return f"「{GROUP_TITLES.get(group, group)}」这一类没勾选处理"
    if facts.get("boost") and config.respect_boost:
        return "刷流引擎在管这个种子"
    if facts.get("watching"):
        return "正在播放，或最近打开过"
    if config.respect_hr and facts.get("hr"):
        elapsed = seeded_hours(brief, first_seen, now)
        # 只有没做够 hr_min_hours 才拦：原来只回一句文案、从不比较，
        # 带 H&R 的种子永远删不掉（哪怕早过了考核时长）
        if elapsed < config.hr_min_hours:
            return (
                f"站点有 H&R 考核（已做种 {elapsed:.0f} 小时，要求不低于 "
                f"{config.hr_min_hours:g} 小时）"
            )
    return None


def library_verdict(relations: dict, config: Settings) -> tuple[str | None, bool]:
    """媒体库这一侧还要不要拦。返回 (不能删的原因, 库里是否已有这份内容)。

    **默认一点不拦**：只当下载器用时，种子到点就删，不用等入库、也不看库里的状态。
    仍会拦的只剩两件「删了会出真事」的情况：

    - 订阅还等着用这个种子里的季集 → 不删（删了会被打回「缺资源」重新下载一遍）；
    - 这个种子还供着别的条目 / 没识别的文件（合集、季包）→ 不删（会连带毁别的片）。

    两个可选开关（默认都关）：``library_required``（没入库不删）、
    ``protect_unwatched``（库里的集没看完不删）。
    """
    waiting = relations.get("waiting_units") or []
    guarded = relations.get("guarded_units") or []
    other = relations.get("other_file_ids") or []
    owned = relations.get("file_ids") or []
    in_library = bool(owned or other)
    if waiting and config.keep_waiting_subscriptions:
        return f"订阅还在等这些季集 {units_text(waiting)}", in_library
    if other:
        return "还供着媒体库里别的条目或没识别的文件", True
    if config.library_required and not in_library:
        return "媒体库还没有这份文件", False
    if config.protect_unwatched and guarded:
        return f"库里的 {units_text(guarded)} 还没看完", True
    return None, in_library


def units_text(units) -> str:
    parts = []
    for unit in units or []:
        try:
            season, episode = int(unit[0]), int(unit[1])
        except (TypeError, ValueError, IndexError):
            continue
        parts.append("电影" if season == 0 and episode == 0 else f"S{season:02d}E{episode:02d}")
    return "、".join(parts[:6]) + ("…" if len(parts) > 6 else "")


def as_relations(raw: dict) -> dict:
    """``library.items.relations`` 的结果 → 删种判断要的事实。"""
    file_ids: list = []
    others: list = []
    waiting: list = []
    guarded: list = []
    for torrent in raw.get("torrents") or []:
        owns = list(torrent.get("file_ids") or []) + list(torrent.get("other_file_ids") or [])
        if not owns:
            continue
        file_ids.extend(torrent.get("file_ids") or [])
        others.extend(torrent.get("other_file_ids") or [])
        status = str(torrent.get("status") or "").lower()
        units = list(torrent.get("units") or [])
        if status in _WAITING:
            # 订阅投递的工单还在等它，或手动下载意图还在等它入库：删了都要重来
            waiting.extend(units)
        elif status in _IMPORTED:
            # 已完成 / 已入库：只剩「库里的集有没有看完」这一条要看
            guarded.extend(units)
        elif _UNKNOWN_STATUS_KEEP:
            # 状态看不明白、又确实认领了库文件：保守当成「完成」，看观看情况
            # （只有空状态，也就是文件来源记录，才直接放行）
            if status:
                guarded.extend(units)
    return {
        "file_ids": uniq(file_ids),
        "other_file_ids": uniq(others),
        "waiting_units": uniq_units(waiting),
        "guarded_units": uniq_units(guarded),
    }


def uniq(values):
    return list(dict.fromkeys(values))


def uniq_units(units):
    out: dict = {}
    for unit in units:
        try:
            key = (int(unit[0]), int(unit[1]))
        except (TypeError, ValueError, IndexError):
            continue
        out[key] = [key[0], key[1]]
    return list(out.values())


def empty_relations() -> dict:
    """「媒体库这一侧什么都没有」的事实。"""
    return {"file_ids": [], "other_file_ids": [], "waiting_units": [], "guarded_units": []}


def ingest_entry(files, library_id, at: float) -> dict:
    """一次入库事件 → 台账条目。

    宿主 ``library.ingest.completed`` 自带 ``info_hashes`` 与 ``files``：这是插件唯一能
    拿到「哪个种子入库了哪些文件」的地方（``dl.tasks`` 不返回 ``file_ids``，宿主也没有
    「按 infohash 查库文件」的操作）。认不出文件 id 就返回空字典，别记假账。
    """
    refs: dict = {}
    for raw in files or ():
        ref = file_ref(raw)
        if ref:
            refs[ref["file_id"]] = ref
    if not refs:
        return {}
    media_item_id = None
    for ref in refs.values():
        if ref["media_item_id"]:
            media_item_id = ref["media_item_id"]
            break
    return {
        "files": [refs[key] for key in sorted(refs)],
        "library_id": library_id,
        "media_item_id": media_item_id,
        "units": uniq_units([[ref["season"], ref["episode"]] for ref in refs.values()]),
        "at": int(num(at)),
    }


def ledger_add(ledger: dict, info_hash: str, entry: dict) -> dict:
    """把一条入库记录并进台账：同一个种子多次入库只合并文件，不重复记。"""
    key = hash_key(info_hash)
    if not key or not entry:
        return dict(ledger or {})
    old = (ledger or {}).get(key) or {}
    refs = {
        ref["file_id"]: ref
        for ref in old.get("files") or []
        if isinstance(ref, dict) and num(ref.get("file_id")) > 0
    }
    for ref in entry.get("files") or []:
        if isinstance(ref, dict) and num(ref.get("file_id")) > 0:
            refs[int(num(ref["file_id"]))] = ref
    merged = {
        "files": [refs[file_id] for file_id in sorted(refs)],
        "library_id": entry.get("library_id") or old.get("library_id"),
        "media_item_id": entry.get("media_item_id") or old.get("media_item_id"),
        "units": uniq_units(list(old.get("units") or []) + list(entry.get("units") or [])),
        "at": int(num(entry.get("at")) or num(old.get("at"))),
    }
    return ledger_trim({**(ledger or {}), key: merged})


def ledger_trim(ledger: dict, limit: int = LEDGER_LIMIT) -> dict:
    """按入库时间留最近的 ``limit`` 条。

    ``PLUGIN_DATA`` 单个值 64 KB 封顶，超了 ``store.set`` 直接失败；台账一旦写不进去，
    「这份内容在不在库里」就又变成永远放行了。
    """
    rows = sorted(
        ((str(key), value) for key, value in (ledger or {}).items() if isinstance(value, dict)),
        key=lambda item: num(item[1].get("at")),
        reverse=True,
    )
    return dict(rows[: max(int(limit), 0)])


def relations_from_ledger(entry: dict) -> dict:
    """台账条目 → 删种判断要的「媒体库这一侧」的事实。

    台账只知道「这个种子往库里送过哪些文件、属于哪个条目」，不知道订阅工单状态；
    等待中的季集要另问 ``library.items.relations``（见插件里的 ``relations_of``）。
    """
    if not isinstance(entry, dict):
        return empty_relations()
    owner = entry.get("media_item_id")
    own: list = []
    others: list = []
    guarded: list = []
    for ref in entry.get("files") or []:
        if not isinstance(ref, dict):
            continue
        # 台账里的行由 file_ref() 生成（已带 gone / state），这里再认一次原始字段：
        # 早前写入的台账没有 gone 这一列，不能因为它就把已清掉的文件当成还在
        state = str(ref.get("state") or ref.get("status") or "").strip().lower()
        if ref.get("gone") or state in _GONE_FILE_STATES:
            continue
        file_id = int(num(ref.get("file_id")))
        if file_id <= 0:
            continue
        if owner is not None and ref.get("media_item_id") not in (None, owner):
            # 合集 / 季包：还有属于别的条目的文件，删了会连带毁别的片
            others.append(file_id)
            continue
        own.append(file_id)
        if state in _IMPORTED:
            guarded.append([int(num(ref.get("season"))), int(num(ref.get("episode")))])
    settled = empty_relations()
    settled["file_ids"] = uniq(own)
    settled["other_file_ids"] = uniq(others)
    settled["guarded_units"] = uniq_units(guarded)
    return settled


def should_remind(brief: dict, first_seen: float, now: float, config: Settings) -> bool:
    """没入库的种子是否已经超过兜底时间，该提醒用户了。

    ``overdue_days`` 的单位是**天**，``seed_hours`` 是**小时**，两者要先化成同一个单位
    再相加——原来把天也乘 3600 当小时算，兜底时间只有设定值的 1/24。
    """
    if first_seen <= 0:
        return False
    grace = (config.seed_hours + config.overdue_days * 24) * 3600
    if now - first_seen < grace:
        return False
    return bool(brief.get("completed"))


def open_until(entry) -> float:
    """一条「打开记录」的保护到什么时候为止（秒级时间戳，0 表示没有保护）。

    新格式是 ``{at: 打开时刻, until: 保护到什么时候}``：播放开始时会按片长把 ``until``
    推到片尾之后，长片的后半段才不会被当成「早就打开过」。老格式只是一个时间戳，
    按打开后 :data:`WEB_IDLE_MINUTES` 分钟算。
    """
    if isinstance(entry, dict):
        at = num(entry.get("at"), 0.0)
        until = num(entry.get("until"), 0.0)
        if until > 0:
            return until
        return at + WEB_IDLE_MINUTES * 60 if at > 0 else 0.0
    stamp = num(entry, 0.0)
    return stamp + WEB_IDLE_MINUTES * 60 if stamp > 0 else 0.0


def recently_opened(opened: dict, media_item_id, now: float) -> bool:
    """这部片是不是还在保护窗口里（网页点进详情 / 正在或刚播放完）。"""
    if media_item_id is None:
        return False
    entry = opened.get(str(media_item_id))
    if entry is None:
        return False
    return now < open_until(entry)


# ---------------------------------------------------------------------- 插件自己的事件


class SeedRemoved(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(frozen=True)

    name: str
    info_hash: str
    title: str = ""
    media_item_id: int | None = None
    seeded_hours: float = 0
    size_bytes: int = 0
    files: str = ""
    deleted_files: bool = True


class SeedOverdue(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(frozen=True)

    name: str
    info_hash: str
    title: str = ""
    media_item_id: int | None = None
    seeded_hours: float = 0
    hours: float = 0


SEED_REMOVED = plugin_event(
    f"{PLUGIN_ID}.seed-removed", SeedRemoved, title="做种定时：种子已到期清理"
)
SEED_OVERDUE = plugin_event(
    f"{PLUGIN_ID}.seed-overdue",
    SeedOverdue,
    title="做种定时：种子到期但被安全线挡着没删",
)


# ---------------------------------------------------------------------- 插件入口


@plugin(
    PLUGIN_ID,
    title="种子做种定时清理",
    config=Settings,
    inject=(HOST_OPS, PLUGIN_DATA, PLUGIN_ROUTES, PLUGIN_JOBS, PLUGIN_EVENTS),
    permissions=(
        "dl.list",
        "dl.tasks",
        "dl.torrent.delete",
        "library.list",
        "library.items.get",
        "library.items.relations",
        "dl.file-sources.list",
    ),
)
async def seed_scheduler(ctx: Context[Settings]) -> None:
    config = ctx.config
    ops = await ctx.use(HOST_OPS).client(ctx)
    store = ctx.use(PLUGIN_DATA).scoped(ctx)
    jobs = ctx.use(PLUGIN_JOBS).scoped(ctx)
    publisher = await ctx.use(PLUGIN_EVENTS).register(ctx, SEED_REMOVED, SEED_OVERDUE)
    log = ctx.logger

    async def data(key: str, default):
        return await store.get(key, default=default)

    async def remember(key: str, value) -> None:
        await store.set(key, value)

    async def add_history(entry: dict) -> None:
        history = await data(HISTORY_KEY, []) or []
        history.insert(0, entry)
        await remember(HISTORY_KEY, history[:HISTORY_SIZE])

    async def media_of(ledger: dict, info_hash: str) -> dict:
        """这个种子在媒体库里的身份：入库台账里记的那条，配上条目的标题与海报。

        台账来自宿主 ``library.ingest.completed``（自带 ``info_hashes`` 与 ``files``）。
        早前走的是「``brief["file_ids"]`` → ``dl.file-sources.list``」：直连 QB 的结果和
        宿主 ``dl.tasks`` 里都没有 ``file_ids``，所以永远查不到文件，媒体库那几条安全线
        也就从来没生效过。
        """
        entry = (ledger or {}).get(hash_key(info_hash)) or {}
        relations = relations_from_ledger(entry)
        owner = entry.get("media_item_id")
        if owner is None:
            return {"relations": relations, "item": {}}
        item = await item_of(entry.get("library_id"), owner)
        return {"relations": relations, "item": item}

    async def item_of(library_id, media_item_id) -> dict:
        """条目详情：标题与海报，给通知卡片用。取不到就返回空——不参与任何判断。

        台账里没存 ``library_id``（宿主事件不一定给）时，按条目 id 在第一个能查通的库
        里试。查不到只是卡片上少个标题，绝不能因此拦住删除或改变结论。
        """
        if media_item_id is None:
            return {}
        guesses = [library_id] if library_id is not None else []
        if library_id is None:
            try:
                for library in await ops.call("library.list", {}) or []:
                    guesses.append(library.get("library_id"))
            except OpsError as exc:
                log.debug("读媒体库列表失败：%s", exc.message)
                return {}
        for guess in guesses[:3]:
            try:
                detail = await ops.call(
                    "library.items.get",
                    {"library_id": guess, "media_item_id": media_item_id},
                )
            except OpsError:
                continue
            if detail:
                return {
                    "library_id": guess,
                    "media_item_id": media_item_id,
                    "title": (detail or {}).get("title") or "",
                    "poster_url": (detail or {}).get("poster_url"),
                }
        return {}

    async def relations_of(info_hash: str, media: dict) -> dict:
        """媒体库这一侧的事实：认领的文件、别的引用、等待中的季集。

        基底是入库台账（文件、条目、别的条目还引用着哪些文件）；再问一次
        「这个条目背后有哪些下载任务」，因为只有 ``library.items.relations`` 带订阅
        工单状态，能判断订阅是不是还在等这几集——删了会被打回缺资源重下。
        """
        settled = dict(media.get("relations") or empty_relations())
        item = media.get("item") or {}
        library_id, media_item_id = item.get("library_id"), item.get("media_item_id")
        if media_item_id is None:
            return settled
        if library_id is None:
            return settled
        try:
            raw = await ops.call(
                "library.items.relations",
                {"library_id": library_id, "media_item_id": media_item_id},
            )
        except OpsError as exc:
            log.debug("查条目关联失败（%s）：%s", info_hash, exc.message)
            return settled
        live = as_relations(raw or {})
        # 订阅工单状态只有这里能拿到；文件、其他引用以更全的一侧为准
        settled["waiting_units"] = uniq_units(
            list(settled.get("waiting_units") or []) + list(live["waiting_units"])
        )
        settled["file_ids"] = uniq(list(settled.get("file_ids") or []) + list(live["file_ids"]))
        settled["other_file_ids"] = uniq(
            list(settled.get("other_file_ids") or []) + list(live["other_file_ids"])
        )
        settled["guarded_units"] = uniq_units(
            list(settled.get("guarded_units") or []) + list(live["guarded_units"])
        )
        return settled

    async def run_scan(dry_run: bool | None = None, only: list[str] | None = None) -> dict:
        """跑一轮：记账 → 判断 → 删除 / 提醒。幂等，随时可以重跑。

        ``only``：这一轮只处理这些状态类（``seeding`` / ``missing`` /
        ``downloading`` / ``errored`` / ``checking`` / ``other``），给人「先只删
        丢失文件的试试」这种用法；不传就按设置里的勾选来。
        """
        dry = config.dry_run if dry_run is None else dry_run
        picked = {str(item).strip().lower() for item in (only or []) if str(item).strip()}
        now = datetime.now(timezone.utc).timestamp()
        seen: dict = await data(SEEN_KEY, {}) or {}
        removed: dict = await data(REMOVED_KEY, {}) or {}
        reminded: dict = await data("reminded", {}) or {}
        opened: dict = await data(OPEN_KEY, {}) or {}
        ledger: dict = await data(LEDGER_KEY, {}) or {}
        report = {
            "at": datetime.now(timezone.utc).isoformat(),
            "dry_run": dry,
            "seeded_hours_limit": config.seed_hours,
            "scanned": 0,
            "removed": 0,
            "reminded": 0,
            "downloader_errors": [],
            "removed_items": [],
            "overdue_items": [],
        }
        index = await task_index()
        try:
            downloaders = await ops.call("dl.list", {}) or []
        except OpsError as exc:
            report["downloader_errors"].append(f"读取下载器列表失败：{exc.message}")
            return report
        usable = [d for d in downloaders if d.get("enabled", True)]
        for downloader in downloaders:
            name = downloader.get("name") or downloader.get("id")
            if not downloader.get("enabled", True):
                continue
            if not downloader.get("usable", True):
                report["downloader_errors"].append(f"下载器「{name}」当前不可用，本轮跳过")
                continue
            # 只有一台下载器时，设置的 QB 地址必然指向它（下载器可以不登记地址）
            one = len(usable) == 1
            did = downloader.get("id")
            briefs = await idle_torrents(downloader, index, only=one)
            for brief in briefs:
                known = index.get(hash_key(brief.get("info_hash"))) or {}
                if known:
                    brief.setdefault("media_item_id", known.get("media_item_id"))
                    brief["source"] = brief.get("source") or known.get("source")
            report["scanned"] += len(briefs)
            handled = 0
            for brief in briefs:
                info_hash = hash_key(brief.get("info_hash"))
                first_seen = num(seen.get(info_hash), 0.0)
                if first_seen <= 0:
                    if info_hash in removed:
                        continue  # 删掉又被加回来的，从下次见到重新起算
                    first_seen = now
                    seen[info_hash] = int(now)
                if seeded_hours(brief, first_seen, now) < config.seed_hours:
                    continue

                media = await media_of(ledger, info_hash)
                relations = await relations_of(info_hash, media)
                item = media.get("item") or {}
                media_item_id = item.get("media_item_id") or brief.get("media_item_id")
                seeded = round(seeded_hours(brief, first_seen, now), 1)
                title = str(brief.get("title") or brief.get("name") or "")
                group = state_group(brief)
                facts = {
                    "group": group,
                    "watching": recently_opened(opened, media_item_id, now)
                    and config.protect_recent_open,
                    "boost": brief.get("source") == "boost",
                    "hr": bool(brief.get("hit_and_run")) or has_hr_tag(brief.get("tags")),
                }
                if picked and group not in picked:
                    continue
                if not group_allowed(group, config):
                    continue
                blocker = removal_blocker(brief, facts, config, first_seen=first_seen, now=now)
                lib_blocker, _in_library = library_verdict(relations, config)
                if blocker is None:
                    blocker = lib_blocker

                if blocker is None:
                    log.info("到期待清理：%s（%s）", title or info_hash, seeded)
                    if handled >= MAX_PER_RUN:
                        continue
                    handled += 1
                    kept = {
                        "name": title or info_hash,
                        "media_item_id": media_item_id,
                        "title": item.get("title") or brief.get("title") or "",
                        "poster_url": item.get("poster_url"),
                    }
                    if dry:
                        report["removed"] += 1
                        report["removed_items"].append(
                            {**kept, "info_hash": info_hash, "seeded_hours": seeded}
                        )
                        continue
                    try:
                        await ops.call(
                            "dl.torrent.delete",
                            {
                                "downloader_id": did,
                                "info_hash": info_hash,
                                "delete_files": config.delete_files,
                            },
                        )
                    except OpsError as exc:
                        report["downloader_errors"].append(f"删除「{title}」失败：{exc.message}")
                        log.warning("删除种子失败（%s）：%s", info_hash, exc.message)
                        continue
                    removed[info_hash] = int(now)
                    seen.pop(info_hash, None)
                    report["removed"] += 1
                    report["removed_items"].append(
                        {**kept, "info_hash": info_hash, "seeded_hours": seeded}
                    )
                    log.info(
                        "已清理做种 %s 小时的种子：%s（%s）",
                        seeded,
                        title or info_hash,
                        "连数据删除" if config.delete_files else "保留数据文件",
                    )
                    try:
                        await publisher.publish(
                            SEED_REMOVED,
                            SeedRemoved(
                                name=title,
                                info_hash=info_hash,
                                title=title,
                                media_item_id=media_item_id,
                                seeded_hours=seeded,
                                size_bytes=int(num(brief.get("size_bytes"))),
                                files="（含数据文件）" if config.delete_files else "（保留数据文件）",
                                deleted_files=config.delete_files,
                            ),
                        )
                    except Exception:  # noqa: BLE001 -- 通知失败不影响删除结论
                        log.warning("发「种子已清理」事件失败", exc_info=True)
                    # 记账放在**删除和通知都做完之后**：原来放在中间，插件数据一超限
                    # store.set 就会抛错，于是「删了、没通知、状态也没落盘」——下一轮
                    # seen 里还有它，会被再判一次、再删一次（第二次失败并刷日志）。
                    await add_history(
                        {
                            "at": datetime.now(timezone.utc).isoformat(),
                            "action": "removed",
                            "name": title,
                            "info_hash": info_hash,
                            "downloader": name,
                            "media_item_id": media_item_id,
                            "title": item.get("title") or "",
                            "seeded_hours": seeded,
                            "size_bytes": int(num(brief.get("size_bytes"))),
                            "delete_files": config.delete_files,
                        }
                    )
                    continue

                # 不删：被安全线挡着。提醒和「开没开入库检查」无关——纯下载器用法下
                # 到不了这里（到点就删了）；能到这里的都是被安全线拦住的，用户本来
                # 就该知道它为什么留着、以及已经拖了多久
                if not should_remind(brief, first_seen, now, config):
                    continue
                if info_hash in reminded:
                    continue
                reminded[info_hash] = int(now)
                report["reminded"] += 1
                report["overdue_items"].append(
                    {"name": title, "info_hash": info_hash, "reason": blocker}
                )
                log.info("「%s」已做种 %s 小时仍保留：%s", title or info_hash, seeded, blocker)
                try:
                    await publisher.publish(
                        SEED_OVERDUE,
                        SeedOverdue(
                            name=title,
                            info_hash=info_hash,
                            title=title,
                            media_item_id=media_item_id,
                            seeded_hours=seeded,
                            hours=round(config.seed_hours + config.overdue_days * 24, 1),
                        ),
                    )
                except Exception:  # noqa: BLE001
                    log.warning("发「到期未入库」事件失败", exc_info=True)
                await add_history(
                    {
                        "at": datetime.now(timezone.utc).isoformat(),
                        "action": "overdue",
                        "name": title,
                        "info_hash": info_hash,
                        "title": title,
                        "seeded_hours": seeded,
                        "reason": blocker,
                    }
                )

        # 收敛状态：60 天没动静的记账清掉，别让插件数据无限长
        cutoff = now - 60 * 86400
        seen = {k: v for k, v in seen.items() if num(v) >= cutoff}
        removed = {k: v for k, v in removed.items() if num(v) >= cutoff}
        await remember(SEEN_KEY, dict(list(seen.items())[-SEEN_LIMIT:]))
        await remember(REMOVED_KEY, dict(list(removed.items())[-SEEN_LIMIT:]))
        stats = await data(STATS_KEY, {}) or {}
        if not dry:
            stats["removed"] = int(stats.get("removed", 0)) + report["removed"]
            stats["reminded"] = int(stats.get("reminded", 0)) + report["reminded"]
        stats["last_run_at"] = report["at"]
        stats["last_scanned"] = report["scanned"]
        await remember(STATS_KEY, stats)
        log.info(
            "%s：扫了 %d 个任务，清理 %d，提醒 %d",
            "演练" if dry else "做种定时",
            report["scanned"],
            report["removed"],
            report["reminded"],
        )
        return report

    # ---- 定时任务（「设置 → 定时任务」里可改周期 / 暂停 / 立即运行）----------
    async def scheduled_scan() -> None:
        try:
            await run_scan()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- 一轮失败不能让定时任务停摆
            log.warning("做种定时扫描失败，下一轮再试", exc_info=True)

    ctx.contribute(
        SCHEDULED_TASKS,
        "scan",
        TaskDefinition(
            key=SCAN_KEY,
            title="做种定时清理扫描",
            handler=scheduled_scan,
            default_trigger_type=TriggerType.INTERVAL,
            default_interval_seconds=600,
            description=(
                "扫一遍下载器里的种子，把做种满设定时长、又没有安全线挡着的种子清理掉。"
                "默认是演练模式：只记「本来会删什么」，不动下载器"
            ),
        ),
    )

    # ---- 后台任务：下载完成 / 入库完成 / 用户点「立即扫描」-------------------
    async def sweep(job_ctx, payload: dict) -> dict:
        await job_ctx.update_progress(
            mode="indeterminate", phase="scan", message="扫描下载器…"
        )
        result = await run_scan(dry_run=bool(payload.get("dry_run", False)))
        if result.get("downloader_errors") and not result.get("scanned"):
            raise JobBlocked("下载器都读不到任务：" + "；".join(result["downloader_errors"][:3]))
        return result

    ctx.contribute(
        JOB_HANDLERS,
        SWEEP_JOB,
        RegisteredJobHandler(sweep, frozenset({1}), title="做种定时清理扫描"),
    )

    def name_of(downloader: dict) -> str:
        return str(downloader.get("name") or downloader.get("id"))

    async def task_index() -> dict:
        """任务中心的快照：这个种子是不是刷流、对应哪个条目。

        **不从这里读 H&R**：``dl.tasks`` 的 ``subscriptions[]`` 里没有这个字段，
        读到的永远是 ``None``。H&R 只能从下载器标签认（见 :func:`has_hr_tag`）。
        """
        try:
            listing = await ops.call("dl.tasks", {}) or {}
        except OpsError as exc:
            log.debug("读任务中心失败：%s", exc.message)
            return {}
        index: dict[str, dict] = {}
        for item in items_of(listing):
            index[hash_key(item.get("info_hash"))] = {
                "source": item.get("source") or "",
                "media_item_id": item.get("media_item_id"),
            }
        return index

    def qb_connection() -> dict:
        """插件设置里填的 QB 连接信息（下载器列表里没有密码，独立进程也拿不到）。"""
        url = (config.qb_url or "").strip().rstrip("/")
        if not url:
            return {}
        password = config.qb_password.get_secret_value() if config.qb_password else ""
        return {
            "url": url,
            "username": config.qb_username or "",
            "password": password,
            "host": _url_host(url),
        }

    def serves(connection: dict, downloader: dict, *, only: bool = False) -> bool:
        """这个连接指向的是不是这台下载器。

        原来把唯一那份 ``qb_url`` 无差别套在 ``dl.list`` 的每台下载器上：有三台下载器
        就会把同一台 QB 里的种子扫三遍，而删除时用的却是**循环里那一台**的
        ``downloader_id``——删的未必是读的那台，多下载器场景下会删错。现在按主机名 +
        端口比对，对不上就直连拿到的任务一律不认，交给任务中心的口径。
        """
        if not connection:
            return False
        url = str(downloader.get("url") or downloader.get("host") or "").strip()
        if not url:
            # 下载器没登记地址：只有一台时才敢认定就是它
            return only
        return _url_host(url) == connection.get("host")

    def local_torrents(connection: dict, index: dict, downloader_id) -> list[dict]:
        """按下载器的连接信息，直接问 qBittorrent 要**全部**种子。

        任务中心只列还在业务管线里的任务，做完使命的种子它不列；而「做种满多久」
        恰恰是这些老种子的事。这里用插件设置里填的连接信息自己连一次——密码不进
        权限系统、也不碰系统里的下载器配置，只在这份插件配置里。

        每条都标上 ``downloader_id``：后面删除时用它，确保删的就是读的那台。
        """
        import qbittorrentapi  # noqa: PLC0415 -- 随主程序带的库

        client = qbittorrentapi.Client(
            host=connection["url"],
            username=connection.get("username") or None,
            password=connection.get("password") or None,
            REQUESTS_ARGS={"timeout": 20},
        )
        out = []
        for torrent in client.torrents_info():
            key = hash_key(torrent.hash)
            known = index.get(key) or {}
            progress = num(getattr(torrent, "progress", 0))
            out.append(
                {
                    "info_hash": key,
                    "name": torrent.name,
                    "title": torrent.name,
                    "progress": progress,
                    "completed": progress >= 1.0 and str(getattr(torrent, "state", "")) not in {"checkingDL", "checkingUP"},
                    "size_bytes": int(num(getattr(torrent, "size", 0))),
                    "state": str(getattr(torrent, "state", "")),
                    # 做种时长以 QB 自己的记录为准（见 seeded_hours）
                    "seeding_seconds": int(num(getattr(torrent, "seeding_time", 0))),
                    "completed_on": int(num(getattr(torrent, "completion_on", 0))),
                    "added_on": int(num(getattr(torrent, "added_on", 0))),
                    "tags": [
                        tag.strip()
                        for tag in str(getattr(torrent, "tags", "") or "").split(",")
                        if tag.strip()
                    ],
                    "media_item_id": known.get("media_item_id"),
                    "source": known.get("source") or "",
                    "downloader_id": downloader_id,
                }
            )
        return out

    async def idle_torrents(downloader: dict, index: dict, *, only: bool = False) -> list[dict]:
        """这台下载器里的种子：优先直连下载器取全量，连不上时退回任务中心口径。

        ``only``：``dl.list`` 里只有这一台可用下载器（设置的 QB 地址必然指向它）。
        """
        connection = qb_connection()
        if serves(connection, downloader, only=only):
            try:
                return await asyncio.to_thread(
                    local_torrents, connection, index, downloader.get("id")
                )
            except Exception as exc:  # noqa: BLE001 -- 连不上就退回任务中心，绝不因此误判
                log.warning(
                    "直连下载器「%s」失败（%s: %s），改用任务中心的口径",
                    name_of(downloader),
                    type(exc).__name__,
                    str(exc)[:200],
                )
        elif connection:
            log.debug(
                "「%s」和设置的 QB 地址不是同一台，按任务中心的口径读", name_of(downloader)
            )
        try:
            listing = await ops.call("dl.tasks", {})
        except OpsError:
            return []
        return [
            item
            for item in items_of(listing)
            if item.get("downloader_id") == downloader.get("id") and item.get("info_hash")
        ]

    async def kick(reason: str) -> None:
        # 节流：事件（下载完成 / 入库完成）可能连着来，5 分钟内只补扫一次，
        # 免得重复盯着同一个种子反复判断
        last = num(await data("last_kick", 0.0))
        now = datetime.now(timezone.utc).timestamp()
        if now - last < 300:
            return
        await remember("last_kick", int(now))
        try:
            await jobs.create(SWEEP_JOB, input={"dry_run": config.dry_run}, dedupe_key="sweep")
        except Exception:  # noqa: BLE001 -- 排不上也无所谓，定时任务稍后还会扫
            log.debug("%s 后的立刻扫描没排上", reason, exc_info=True)

    async def on_download_event(event) -> None:
        # 下载完成：记下这一刻，尽量让「首见时间」贴近真正开始做种的时刻，
        # 再顺手扫一轮（刚下完的种子往往就是该清的那批）
        if getattr(event, "info_hash", None):
            seen = await data(SEEN_KEY, {}) or {}
            key = hash_key(event.info_hash)
            if key not in seen:
                seen[key] = int(datetime.now(timezone.utc).timestamp())
                await remember(SEEN_KEY, seen)
        await kick("下载完成")

    async def on_ingest_event(event) -> None:
        """入库完成：把「哪个种子入库了哪些文件、属于哪个条目」记进台账。

        这是插件唯一能拿到这层关系的时刻——事件自带 ``info_hashes`` 与 ``files``。
        宿主没有「按 infohash 查库文件」的操作，``dl.tasks`` 也不返回 ``file_ids``，
        所以不在这里记，媒体库那几条安全线（还供着别的条目、订阅还在等几集）就全都
        无从判断。
        """
        now = datetime.now(timezone.utc).timestamp()
        hashes = [hash_key(value) for value in getattr(event, "info_hashes", None) or []]
        hashes = [value for value in hashes if value]
        if not hashes:
            single = hash_key(getattr(event, "info_hash", None))
            hashes = [single] if single else []
        files = list(getattr(event, "files", None) or [])
        library_id = getattr(event, "library_id", None)
        if library_id is None:
            media_item = getattr(event, "media_item", None)
            library_id = getattr(media_item, "library_id", None) or getattr(
                event, "target_library_id", None
            )
        entry = ingest_entry(files, library_id, now)
        if hashes and entry:
            ledger = await data(LEDGER_KEY, {}) or {}
            for info_hash in hashes:
                ledger = ledger_add(ledger, info_hash, entry)
            await remember(LEDGER_KEY, ledger_trim(ledger))
            log.debug("入库台账已记：%s 共 %d 个文件", ",".join(hashes[:3]), len(entry["files"]))
        # 首次见到的时间也要记：入库完成的种子往往正是「做完使命」的那批
        if hashes:
            seen = await data(SEEN_KEY, {}) or {}
            touched = False
            for info_hash in hashes:
                if info_hash not in seen:
                    seen[info_hash] = int(now)
                    touched = True
            if touched:
                await remember(SEEN_KEY, seen)
        await kick("入库完成")

    ctx.on(DOWNLOAD_COMPLETED, on_download_event, id="completed")
    ctx.on(LIBRARY_INGEST_COMPLETED, on_ingest_event, id="ingested")

    def open_entry(now: float, seconds: float = 0.0) -> dict:
        """一条「打开记录」：``until`` 至少给到 :data:`WEB_IDLE_MINUTES` 窗口之后。"""
        return {
            "at": int(now),
            "until": int(now + max(WEB_IDLE_MINUTES * 60, num(seconds)) + 60),
        }

    async def remember_open(item_id, now: float, seconds: float = 0.0) -> None:
        opened = await data(OPEN_KEY, {}) or {}
        key = str(item_id)
        previous = open_until(opened.get(key))
        entry = open_entry(now, seconds)
        if previous > entry["until"]:
            entry["until"] = int(previous)  # 别把已有的更长保护改短
        opened[key] = entry
        # 只留还有保护意义的记录：保护窗口最长也就几小时，留 3 天纯属浪费容量
        cutoff = now - WEB_IDLE_MINUTES * 60
        await remember(
            OPEN_KEY,
            {k: v for k, v in opened.items() if open_until(v) > cutoff},
        )

    async def on_viewed(event) -> None:
        """网页上点进详情：从现在起保一段。"""
        item_id = getattr(getattr(event, "media", None), "id", None)
        if item_id is None:
            return
        await remember_open(item_id, datetime.now(timezone.utc).timestamp())

    async def on_playback(event) -> None:
        """开始播放：按片长把保护窗口推到片尾之后。

        原来只保「开始播放后 30 分钟」：一部两小时的片看到后半段就出了窗口，随时
        可能被删——而这恰恰是最不该删的时候。``until`` 要覆盖整段播放，不是固定值。
        """
        media = getattr(event, "media", None)
        item_id = getattr(media, "id", None) or getattr(event, "media_item_id", None)
        if item_id is None:
            return
        seconds = num(getattr(event, "duration_seconds", 0) or getattr(event, "duration", 0))
        if seconds <= 0 and hasattr(event, "duration"):
            seconds = num(event.duration)
        await remember_open(item_id, datetime.now(timezone.utc).timestamp(), seconds)

    ctx.on(LIBRARY_ITEM_VIEWED, on_viewed, id="viewed")
    ctx.on(PLAYBACK_STARTED, on_playback, id="playback")

    # ---- 接口（同时是 mclaw 命令，AI 助手可以直接调）------------------------
    router = APIRouter()

    @router.get(
        "/status",
        operation_id=f"plugins.{PLUGIN_ID}.status",
        summary="做种定时：设置、累计统计与最近的处理记录",
    )
    async def status() -> dict:
        ledger = await data(LEDGER_KEY, {}) or {}
        return {
            "settings": config.model_dump(),
            "stats": await data(STATS_KEY, {}) or {},
            "tracking": len(await data(SEEN_KEY, {}) or {}),
            "ingested": len(ledger),
            "history": (await data(HISTORY_KEY, []) or [])[:50],
        }

    @router.post(
        "/scan",
        operation_id=f"plugins.{PLUGIN_ID}.scan",
        summary="做种定时：立刻扫描一次（dry_run 不传时按设置里的演练开关）",
    )
    async def scan(
        dry_run: bool | None = None,
        only: list[str] | None = None,
    ) -> dict:
        return await run_scan(dry_run=dry_run, only=only)

    @router.get(
        "/preview",
        operation_id=f"plugins.{PLUGIN_ID}.preview",
        summary="做种定时：只读预演，现在哪些种子会到期、各自为什么删 / 为什么留",
    )
    async def preview() -> dict:
        result = await run_scan(dry_run=True)
        return {
            "would_remove": result["removed"],
            "would_remind": result["reminded"],
            "items": result["removed_items"],
            "overdue": result["overdue_items"],
            "downloader_errors": result["downloader_errors"],
        }

    @router.get(
        "/queue",
        operation_id=f"plugins.{PLUGIN_ID}.queue",
        summary="做种定时：每个种子还要做种多久、什么时候清理（只读，按时间排好）",
    )
    async def queue() -> dict:
        try:
            now = datetime.now(timezone.utc).timestamp()
            seen = await data(SEEN_KEY, {}) or {}
            opened = await data(OPEN_KEY, {}) or {}
            ledger: dict = await data(LEDGER_KEY, {}) or {}
            index = await task_index()
            rows: list[dict] = []
            try:
                downloaders = await ops.call("dl.list", {}) or []
            except OpsError as exc:
                raise HTTPException(
                    status_code=502, detail=f"读取下载器失败：{exc.message}"
                ) from exc
            usable = [d for d in downloaders if d.get("enabled", True)]
            for downloader in downloaders:
                if not downloader.get("enabled", True):
                    continue
                if not downloader.get("usable", True):
                    continue
                one = len(usable) == 1
                for brief in await idle_torrents(downloader, index, only=one):
                    info_hash = hash_key(brief.get("info_hash"))
                    first_seen = num(seen.get(info_hash), 0.0)
                    elapsed = round(seeded_hours(brief, first_seen, now), 1)
                    media = await media_of(ledger, info_hash)
                    relations = await relations_of(info_hash, media)
                    item = media.get("item") or {}
                    media_item_id = item.get("media_item_id") or brief.get("media_item_id")
                    group = state_group(brief)
                    facts = {
                        "group": group,
                        "watching": recently_opened(opened, media_item_id, now)
                        and config.protect_recent_open,
                        "boost": brief.get("source") == "boost",
                        "hr": bool(brief.get("hit_and_run")) or has_hr_tag(brief.get("tags")),
                    }
                    # 这里给出的 note 要和真正删的时候一致：两条安全线都要算
                    blocker = removal_blocker(
                        brief, facts, config, first_seen=first_seen, now=now
                    )
                    lib_blocker, in_library = library_verdict(relations, config)
                    if blocker is None:
                        blocker = lib_blocker
                    started = seeded_start(brief, first_seen, now) or first_seen
                    planned = (started + config.seed_hours * 3600) if started else None
                    if blocker:
                        state = "保留"
                    elif elapsed >= config.seed_hours:
                        state = "已到期"
                    else:
                        state = "待到期"
                    rows.append(
                        {
                            "name": brief.get("name"),
                            "info_hash": info_hash,
                            "state": state,
                            "group": group,
                            "group_title": GROUP_TITLES.get(group, group),
                            "seeded_hours": elapsed,
                            "remaining_hours": round(max(0.0, config.seed_hours - elapsed), 1),
                            "in_library": in_library,
                            "media_item_id": media_item_id,
                            "removes_at": (
                                datetime.fromtimestamp(planned, timezone.utc).isoformat()
                                if planned
                                else None
                            ),
                            "note": blocker or "",
                        }
                    )
            rows.sort(key=lambda row: (row["removes_at"] or "9", -num(row.get("seeded_hours"))))
            counts: dict[str, int] = {}
            for row in rows:
                key = row["group"]
                counts[key] = counts.get(key, 0) + 1
            log.info(
                "队列读数：%d 个任务，最早到期 %s",
                len(rows),
                rows[0]["removes_at"] if rows else "-",
            )
            return {
                "seeded_hours_limit": config.seed_hours,
                "dry_run": config.dry_run,
                # 每一类有多少个、这一类现在勾没勾处理
                "groups": [
                    {
                        "group": group,
                        "title": GROUP_TITLES[group],
                        "count": counts.get(group, 0),
                        "handled": group_allowed(group, config),
                    }
                    for group in ["seeding", "missing", "downloading", "errored", "checking", "other"]
                    if counts.get(group, 0)
                ],
                "count": len(rows),
                "items": rows,
            }
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 -- 排查期把真话给出去
            log.warning("队列读数失败：%r", exc, exc_info=True)
            raise HTTPException(status_code=500, detail=f"队列读数失败：{exc!r}") from exc

    routes = ctx.use(PLUGIN_ROUTES)
    routes.mount(ctx, router)

    log.info(
        "%s 已加载：做种满 %s 小时清理（%s）",
        PLUGIN_ID,
        config.seed_hours,
        "演练模式" if config.dry_run else ("连数据删除" if config.delete_files else "保留数据文件"),
    )
