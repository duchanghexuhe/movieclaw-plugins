"""保活轮：site.list 取启用站点 → 逐站适配 → 代请求 → 三态判定 → 状态机 → 事件。

站点请求全部经宿主 ``site.proxy.request`` 代发（借宿主已认证会话，插件接触不到
凭据）；请求节奏由宿主每站限流兜底，这里逐站串行即可。
"""

from __future__ import annotations

import logging
from typing import Any

from movieclaw_sdk.ops import HOST_OPS, OpsError

from pt_keepalive import state as st
from pt_keepalive.events import (
    AUTH_EXPIRED,
    DAILY_REPORT,
    FAILURE_REPORT,
    AuthExpired,
    DailyReport,
)
from pt_keepalive.result import Outcome, SiteResult
from pt_keepalive.settings import Settings
from pt_keepalive.sites import SiteInfo, Step, adapter_for, step_response

logger = logging.getLogger("movieclaw_plugins.pt_keepalive")

#: 宿主 409 且消息含这些特征 → 本轮没访问（站点未配置 / 停用 / 还没验证通过）
_HOST_SKIP_MARKS = ("未配置", "已停用", "尚未验证通过")

#: 宿主 409 且消息含这些特征 → 已认证会话失效
_HOST_AUTH_MARKS = ("认证失败",)

#: 状态机单键（全部站点汇总 JSON）
STATE_KEY = st.STATE_KEY


class KeepaliveRunner:
    """一轮保活的执行器；由入口构造（注入 data/events 门面）并供任务处理器调用。"""

    def __init__(self, ctx: Any, *, data: Any, events: Any) -> None:
        self.ctx = ctx
        raw = getattr(ctx, "config", None)
        self.config: Settings = raw if isinstance(raw, Settings) else Settings.model_validate(raw or {})
        self.data = data  # PLUGIN_DATA.scoped(ctx)：get(key, default=) / set(key, value)
        self.events = events  # PLUGIN_EVENTS.register() 返回的 Publisher
        self.ops: Any = None

    async def _call(self, operation: str, **arguments: Any) -> Any:
        if self.ops is None:
            self.ops = await self.ctx.use(HOST_OPS).client(self.ctx)
        return await self.ops.call(operation, arguments)

    # ------------------------------------------------------------------ 站点清单

    async def enabled_sites(self) -> list[SiteInfo]:
        """宿主已配置且 usable 的站点，带上能拿到的展示名。"""
        rows = await self._call("site.list")
        names = await self._catalog_names()
        sites: list[SiteInfo] = []
        for row in rows or []:
            if not isinstance(row, dict) or not row.get("usable"):
                continue
            site_id = str(row.get("site_id"))
            sites.append(
                SiteInfo(
                    site_id=site_id,
                    display_name=str(row.get("display_name") or names.get(site_id) or site_id),
                    usable=True,
                    auth_type=str(row.get("auth_type") or ""),
                )
            )
        return sites

    async def _catalog_names(self) -> dict[str, str]:
        """site.catalog 的 site_id → 展示名。

        ``site.list`` 只回已配置站点、没有展示名；通知里用 ``hdsky`` 还是
        ``HDSky`` 是给用户看的差别。拿不到目录就退回 site_id，不影响保活。
        """
        try:
            if self.ops is None:
                self.ops = await self.ctx.use(HOST_OPS).client(self.ctx)
            if not self.ops.allowed("site.catalog"):
                return {}
            rows = await self.ops.call("site.catalog", {})
        except Exception:  # noqa: BLE001 - 展示名是锦上添花，失败不拖垮整轮
            logger.debug("读取站点目录失败，展示名退回 site_id", exc_info=True)
            return {}
        return {
            str(row.get("site_id")): str(row.get("display_name") or "")
            for row in rows or []
            if isinstance(row, dict)
        }

    # ------------------------------------------------------------------ 单站执行

    async def _proxy_request(self, site: SiteInfo, step: Step) -> dict:
        """经宿主代发一个请求；把宿主的 409 语义翻译成统一响应形态。"""
        plan = step.request
        try:
            return await self._call(
                "site.proxy.request",
                site_id=site.site_id,
                method=plan.method,
                path=plan.path,
                base=plan.base,
                headers=plan.headers,
                body=plan.body,
            ) or {}
        except OpsError as exc:
            message = str(exc.message or exc)
            if exc.status == 409 and any(m in message for m in _HOST_SKIP_MARKS):
                # 站点没配置 / 停用 / 还没验证通过：本轮没访问，不是失效
                return {"_skip": message}
            if exc.status == 409 and any(m in message for m in _HOST_AUTH_MARKS):
                return {"_auth_expired": True}
            logger.warning("站点 %s 代请求被宿主拒绝：%s", site.site_id, message)
            return {"_error": message}

    async def run_site(self, site: SiteInfo) -> SiteResult:
        """跑单站：步骤依次执行，最后一步的判定即本轮结果。"""
        adapter = adapter_for(site)
        if adapter is None:
            return SiteResult(
                Outcome.SKIPPED,
                f"未适配的 API 站点（{site.auth_type or '未知认证'}），本轮跳过",
            )
        steps = [
            step
            for step in adapter(site)
            if not (step.signin_only and not self.config.signin_enabled)
        ]
        if not steps:
            return SiteResult(Outcome.SKIPPED, "没有要执行的动作（签到已关闭）")

        last: SiteResult | None = None
        for step in steps:
            response = await self._proxy_request(site, step)
            if response.get("_skip"):
                last = SiteResult(Outcome.SKIPPED, f"宿主判定站点当前不可访问：{response['_skip']}")
                break
            if response.get("_auth_expired"):
                last = SiteResult(
                    Outcome.FAILED,
                    "宿主判定站点凭据不可用（请更新凭据并重新验证）",
                    auth_expired=True,
                )
                break
            if response.get("_error"):
                last = SiteResult(Outcome.FAILED, f"代请求失败：{response['_error']}")
                break
            last = step.judge(step_response(response))
            # 首页没登录证据就不必再试签到页（后续步骤只会更糟）
            if last.outcome in (Outcome.FAILED, Outcome.NEED_HUMAN):
                break
        assert last is not None
        return last

    # ------------------------------------------------------------------ 整轮

    async def run_round(self) -> dict:
        """一轮：全部站点跑完，更新状态机，发事件。返回汇总（供 status 路由/测试）。"""
        summary: dict[str, Any] = {
            "ok": 0,
            "signin": 0,
            "failed": 0,
            "human": 0,
            "skipped": 0,
            "sites": {},
            "ran_at": st.now_iso(),
        }
        if not self.config.enabled:
            summary["skipped_reason"] = "插件已停用（设置里总开关关闭）"
            return summary
        states = st.normalize(await self.data.get(STATE_KEY, default=None))
        sites = await self.enabled_sites()
        for site in sites:
            try:
                result = await self.run_site(site)
            except Exception:  # noqa: BLE001 - 单站异常不拖垮整轮
                logger.exception("站点 %s 保活轮异常", site.site_id)
                result = SiteResult(Outcome.FAILED, "执行异常，见插件日志")

            # 先发告警、再按「告警是否发出去」落状态：发失败下一轮还能重试，
            # 不会因为状态机先置位而永远丢掉这次告警。
            alert = st.should_alert_expired(states, site.site_id, result.auth_expired)
            alerted: bool | None = None
            if alert:
                try:
                    await self.events.publish(
                        AUTH_EXPIRED, AuthExpired(site_id=site.site_id, site_name=site.display_name)
                    )
                    alerted = True
                except Exception:  # noqa: BLE001 - 通知失败不该丢掉站点结果
                    logger.exception("站点 %s 失效告警发送失败", site.site_id)
                    alerted = False

            states = st.record(
                states,
                site.site_id,
                result.outcome.value,
                result.detail,
                auth_expired=result.auth_expired,
                site_name=site.display_name,
                alerted=alerted,
            )
            summary["sites"][site.site_id] = {
                "name": site.display_name,
                "outcome": result.outcome.value,
                "detail": result.detail,
            }
            if result.outcome == Outcome.SIGNIN_OK:
                summary["signin"] += 1
                summary["ok"] += 1
            elif result.outcome == Outcome.LOGIN_ONLY:
                summary["ok"] += 1
            elif result.outcome == Outcome.NEED_HUMAN:
                summary["human"] += 1
            elif result.outcome == Outcome.SKIPPED:
                summary["skipped"] += 1
            else:
                summary["failed"] += 1
        await self.data.set(STATE_KEY, states)

        failures = st.failure_summary(states)
        if self.config.daily_report:
            # 日报是用户主动开的常规汇总，失败明细已经包含在里面
            await self._publish_report(summary, failures, DAILY_REPORT)
        else:
            pending = st.failure_summary(states, pending_only=True)
            if pending:
                # 连续失败是异常，日报关着也得让用户知道；同一段失败只报一次
                await self._publish_report(summary, pending, FAILURE_REPORT)
                states = st.mark_failure_alerted(states)
                await self.data.set(STATE_KEY, states)
        summary["states"] = states
        logger.info(
            "保活轮完成：在线 %d（签到成功 %d）失败 %d 需人工 %d 跳过 %d",
            summary["ok"],
            summary["signin"],
            summary["failed"],
            summary["human"],
            summary["skipped"],
        )
        return summary

    async def _publish_report(
        self, summary: dict[str, Any], failures: list[str], event: Any
    ) -> None:
        """发一条汇总事件；通知失败不影响本轮结果落库。"""
        try:
            await self.events.publish(
                event,
                DailyReport(
                    ok_count=summary["ok"],
                    signin_count=summary["signin"],
                    failed_count=summary["failed"],
                    human_count=summary["human"],
                    skipped_count=summary["skipped"],
                    failures=failures,
                    detail=_report_detail(summary, failures),
                ),
            )
        except Exception:  # noqa: BLE001
            logger.exception("保活汇总事件 %s 发送失败", getattr(event, "name", event))

    # ------------------------------------------------------------------ 状态查询（status 路由用）

    async def status(self) -> dict[str, Any]:
        """各站最近一轮结果（不发起请求）。"""
        states = st.normalize(await self.data.get(STATE_KEY, default=None))
        return {
            "enabled": self.config.enabled,
            "signin_enabled": self.config.signin_enabled,
            "daily_report": self.config.daily_report,
            "sites": {
                sid: {
                    "name": entry.get("site_name") or sid,
                    "outcome": entry.get("outcome"),
                    "detail": entry.get("detail"),
                    "consecutive_failures": entry.get("consecutive_failures", 0),
                    "last_ok_at": entry.get("last_ok_at"),
                    "updated_at": entry.get("updated_at"),
                }
                for sid, entry in sorted(states.items())
            },
        }


def _report_detail(summary: dict[str, Any], failures: list[str]) -> str:
    """日报正文：通知模板只做字段替换，一行汇总在这里拼好。"""
    parts = [f"在线 {summary['ok']} 站（签到成功 {summary['signin']}）"]
    if summary["failed"]:
        parts.append(f"失败 {summary['failed']}")
    if summary["human"]:
        parts.append(f"需人工 {summary['human']}")
    if summary["skipped"]:
        parts.append(f"跳过 {summary['skipped']}")
    line = "，".join(parts) + "。"
    if failures:
        line += "连续失败：" + "；".join(failures) + "。"
    return line
