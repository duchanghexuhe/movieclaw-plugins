"""站点适配：把「对一个站点做一轮保活」翻译成对宿主 site.proxy.request 的调用。

分发表：专用站（API 触碰类）按 site_id 命中；其余页面型站点走 NexusPHP 通用模板。
**没适配的 API 站直接判跳过**——拿 HTML 判定器去读 JSON 站的响应只会产出假失败。
站点有没有、凭据可不可用由宿主 ``site.list`` 说了算，这里只关心「对这个站发什么
请求、怎么判结果」。

M-Team 不在分发表里：它的保活接口只认网页会话，必须用账号密码 + TOTP 自己登录
（见 ``pt_keepalive.mteam``），不走这条代请求通道。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pt_keepalive.result import (
    Outcome,
    SiteResult,
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
# 分发表
# ---------------------------------------------------------------------------


def adapter_for(site: SiteInfo) -> Adapter | None:
    """站点 → 适配器；``None`` 表示这个站没有可用的适配（本轮跳过）。

    用 API Key 认证的站点是纯 API 站（如 sunnypt），没有可判定的页面，不套 NexusPHP
    模板。M-Team 也是 API 站，但它由插件用账号密码 + TOTP 自己登录（见
    ``pt_keepalive.mteam``），在 ``runner.SELF_LOGIN_SITES`` 就被分流，走不到这里。
    """
    # 宿主 `site.list` 回的是枚举值（`APIKEY`），这里按大小写不敏感比对，
    # 免得换个宿主版本把 API 站误判成 NexusPHP 站、每轮假失败。
    if site.auth_type.strip().lower() == "apikey":
        return None
    return nexusphp_adapter
