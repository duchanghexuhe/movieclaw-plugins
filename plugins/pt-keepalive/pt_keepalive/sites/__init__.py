"""站点适配：把「对一个站点做一轮保活」翻译成对宿主 site.proxy.request 的调用。

分发表：专用站（API 触碰类）按 site_id 命中；其余页面型站点走 NexusPHP 通用模板。
**没适配的 API 站直接判跳过**——拿 HTML 判定器去读 JSON 站的响应只会产出假失败。
站点有没有、凭据可不可用由宿主 ``site.list`` 说了算，这里只关心「对这个站发什么
请求、怎么判结果」。
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from pt_keepalive.result import (
    Outcome,
    SiteResult,
    api_payload_of,
    judge_api_json,
    judge_nexusphp_page,
)


@dataclass
class SiteInfo:
    """site.list 里一个站点的最少信息（宿主返回脱敏，无凭据字段）。"""

    site_id: str
    display_name: str
    usable: bool
    auth_type: str


@dataclass
class PlannedRequest:
    """一个待发的代请求（与宿主 SiteProxyRequest 字段对齐）。"""

    method: str = "GET"
    path: str = "/"
    base: str = "web"
    headers: dict[str, str] = field(default_factory=dict)
    body: dict[str, Any] | str | None = None


@dataclass
class StepResponse:
    """判定函数看到的响应（宿主 SiteProxyResponse 的字段子集）。"""

    status_code: int = 200
    text: str = ""
    json_body: Any = None


@dataclass
class Step:
    """一轮里的一步：发这个请求，用这个判定函数读响应。"""

    request: PlannedRequest
    judge: Callable[[StepResponse], SiteResult]
    #: 仅「尝试签到」开启时才执行（关掉签到后连请求都不发）
    signin_only: bool = False


#: 适配器：站点信息 → 一轮要执行的步骤
Adapter = Callable[[SiteInfo], list[Step]]


def step_response(response: dict[str, Any]) -> StepResponse:
    """宿主代请求响应字典 → 判定函数用的结构。"""
    try:
        status = int(response.get("status_code") or 200)
    except (TypeError, ValueError):
        status = 200
    return StepResponse(
        status_code=status,
        text=str(response.get("text") or ""),
        json_body=response.get("json_body"),
    )


# ---------------------------------------------------------------------------
# NexusPHP 通用模板
# ---------------------------------------------------------------------------


def nexusphp_adapter(site: SiteInfo) -> list[Step]:
    """先访问首页确认登录态，再访问签到页尝试签到。

    分两步的理由：签到页不是每个 NexusPHP 站都有（没有就是 404），而首页一定有
    登出链接。把「确认会话」与「签到」拆开，缺签到页的站不会每轮都报假失败。
    """

    def judge_home(response: StepResponse) -> SiteResult:
        return judge_nexusphp_page(
            response.text, want_signin=False, status_code=response.status_code
        )

    def judge_attendance(response: StepResponse) -> SiteResult:
        if response.status_code == 404:
            return SiteResult(Outcome.LOGIN_ONLY, "登录在线；站点没有签到页（attendance.php 404）")
        return judge_nexusphp_page(
            response.text,
            want_signin=True,
            status_code=response.status_code,
            login_confirm=False,
        )

    return [
        Step(PlannedRequest(method="GET", path="index.php"), judge_home),
        Step(
            PlannedRequest(method="GET", path="attendance.php"),
            judge_attendance,
            signin_only=True,
        ),
    ]


# ---------------------------------------------------------------------------
# M-Team 专用（触碰最后活跃时间）
# ---------------------------------------------------------------------------

#: updateLastBrowse 签名密钥（来自站点前端 JS，站点改版轮换时只改这里）
_MTEAM_SIGN_SECRET = b"HLkPcWmycL57mfJt"

#: M-Team 2026-10 起的反直觉成功组合 + 传统成功形态
_MTEAM_SUCCESS_MARKS: tuple[dict, ...] = (
    {"code": "1", "message": "FAIL"},
    {"code": 1, "message": "FAIL"},
    {"code": "0"},
    {"code": 0},
    {"message": "SUCCESS"},
    {"success": True},
)


def _mteam_signature(path: str, timestamp_ms: int) -> str:
    """Base64(HMAC-SHA1(secret, "POST&{path}&{ts_ms}"))——站点前端算法的复刻。"""
    import base64
    import hmac

    message = f"POST&{path}&{timestamp_ms}".encode()
    digest = hmac.new(_MTEAM_SIGN_SECRET, message, hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def mteam_adapter(site: SiteInfo) -> list[Step]:
    """调 /api/member/updateLastBrowse 刷新「最后浏览时间」（防长期不活跃删号）。"""
    path = "/api/member/updateLastBrowse"
    ts_ms = int(time.time() * 1000)
    # 设备指纹：从 site_id 稳定派生，避免每轮被站点认作新设备
    digest = hashlib.sha256(f"pt-keepalive:{site.site_id}".encode()).hexdigest()
    plan = PlannedRequest(
        method="POST",
        path=path,
        base="api",
        headers={
            "Content-Type": "application/json",
            # 站点网页域名（宿主配置里 web_base_url 也是它）；referer 只作来源标识，
            # 不参与签名，站点改域名时改这一行即可。
            "referer": "https://tp.m-team.cc/",
            "Did": digest[:32],
            "visitorid": digest[:16],
        },
        body={"_timestamp": ts_ms, "_sgin": _mteam_signature(path, ts_ms)},
    )

    def judge(response: StepResponse) -> SiteResult:
        payload = api_payload_of({"json_body": response.json_body, "text": response.text})
        if payload is not None:
            return judge_api_json(
                payload, success_marks=_MTEAM_SUCCESS_MARKS, status_code=response.status_code
            )
        if response.status_code in (401, 403):
            # 401/403 连 JSON 都没有：M-Team 在凭据失效时偶尔直接回纯文本
            return SiteResult(
                Outcome.FAILED,
                f"站点拒绝访问（HTTP {response.status_code}），凭据可能已失效",
                auth_expired=True,
            )
        # 非 JSON 响应：按未预期失败处理（站点改版或被拦）
        snippet = response.text[:120]
        return SiteResult(
            Outcome.FAILED,
            f"站点返回了非 JSON 响应：{snippet}" if snippet else "站点返回了非 JSON 响应",
        )

    return [Step(plan, judge)]


# ---------------------------------------------------------------------------
# 分发表
# ---------------------------------------------------------------------------

#: 专用站适配器（按 site_id 精确命中）
SPECIAL_ADAPTERS: dict[str, Adapter] = {
    "mteam": mteam_adapter,
}


def adapter_for(site: SiteInfo) -> Adapter | None:
    """站点 → 适配器；``None`` 表示这个站没有可用的适配（本轮跳过）。

    专用站优先。用 API Key 认证的其余站点是纯 API 站（如 sunnypt），没有可判定
    的页面，不套 NexusPHP 模板；等将来写专用适配再启用。
    """
    special = SPECIAL_ADAPTERS.get(site.site_id)
    if special is not None:
        return special
    if site.auth_type == "apikey":
        return None
    return nexusphp_adapter
