"""PT 站点保活：对已配置站点定时维持登录态、尝试签到、刷新最后活跃时间。

宿主凭据体系是唯一真源：插件经宿主操作 ``site.proxy.request`` 借已认证会话发请求
（凭据不出宿主），经 ``site.list`` 发现启用站点；失效只告警、不自动重登
（PT 站普遍禁止脚本登录）。定时任务经 ``SCHEDULED_TASKS`` 贡献，用户可在
「设置 → 定时任务」改周期。风险告知：程序化签到/访问未必被站方计为「用户活跃」，
插件显示成功只代表请求与判据成立，不承诺免于封号。

注意：本模块挂了路由，**不能**启用延迟注解（future annotations）——
端点参数与返回值的类型必须在模块顶层真实导入，进程外运行时才能解析。
"""

import logging
from typing import Any

from fastapi import APIRouter
from movieclaw_api.plugins.keys import PLUGIN_DATA, PLUGIN_ROUTES
from movieclaw_scheduler import SCHEDULED_TASKS, TaskDefinition, TriggerType
from movieclaw_sdk import Context, plugin
from movieclaw_sdk.events import PLUGIN_EVENTS
from movieclaw_sdk.ops import HOST_OPS

from pt_keepalive import events as plugin_events
from pt_keepalive.runner import KeepaliveRunner
from pt_keepalive.settings import Settings

logger = logging.getLogger("movieclaw_plugins.pt_keepalive")

PLUGIN_ID = "pt-keepalive"
TASK_KEY = f"{PLUGIN_ID}.run"


@plugin(
    PLUGIN_ID,
    title="PT 站点保活",
    inject=(HOST_OPS, PLUGIN_DATA, PLUGIN_EVENTS, PLUGIN_ROUTES),
    permissions=("site.list", "site.catalog", "site.proxy.request"),
    config=Settings,
)
async def apply(ctx: Context) -> None:
    # 自检：宿主必须支持 site.proxy.request（主程序先行开放的能力）。
    # 旧宿主上照常加载、不贡献任务、不发请求——用户看到的是空闲插件而不是报错。
    try:
        ops_client = await ctx.use(HOST_OPS).client(ctx)
        supported = all(
            ops_client.allowed(op) for op in ("site.list", "site.proxy.request")
        )
    except Exception:  # noqa: BLE001
        logger.exception("获取宿主操作目录失败，视为不支持")
        supported = False
    if not supported:
        logger.warning(
            "宿主不支持 site.proxy.request / site.list 操作（需更新主程序）；"
            "保活任务不注册，插件保持空闲"
        )
        return

    events = await ctx.use(PLUGIN_EVENTS).register(
        ctx,
        plugin_events.AUTH_EXPIRED,
        plugin_events.DAILY_REPORT,
        plugin_events.FAILURE_REPORT,
    )
    data = ctx.use(PLUGIN_DATA).scoped(ctx)
    runner = KeepaliveRunner(ctx, data=data, events=events)

    # 状态查询接口：装好后成为 mclaw plugins pt-keepalive status
    routes = ctx.use(PLUGIN_ROUTES)
    router = APIRouter()

    @router.get(
        "/status",
        operation_id=f"plugins.{ctx.entry_id}.status",
        summary="各站点保活状态",
    )
    async def status() -> dict[str, Any]:
        return await runner.status()

    routes.mount(ctx, router)

    # 贡献定时任务：调度器 watch 到注册表新增后自动排上；卸载/停用随注册表撤下。
    # 处理器闭包持有 runner，不落模块全局——多插件实例不会串。
    async def _round() -> None:
        await runner.run_round()

    ctx.contribute(
        SCHEDULED_TASKS,
        TASK_KEY,
        TaskDefinition(
            key=TASK_KEY,
            title="PT 站点保活轮",
            handler=_round,
            default_trigger_type=TriggerType.CRON,
            default_cron="17 9 * * *",
            description=(
                "对已配置的 PT 站点执行保活：访问首页确认登录态、尝试签到（可关）、"
                "刷新最后活跃时间、凭据失效告警"
            ),
        ),
    )
    logger.info("PT 站点保活已加载：定时任务 %s（每天 09:17，可在 设置→定时任务 调整）", TASK_KEY)
