"""电视桌面：把厂商桌面换成「MovieClaw 桌面」。

规格在主仓库 ``docs/design/androidtv-home.md``（§8 是本插件）。``apply`` 只做登记：
挂管理员区路由；设备判断都在 operations.py，文件与 HTTP 在 routes.py。
"""

from movieclaw_api.plugins.keys import PLUGIN_FILES, PLUGIN_ROUTES
from movieclaw_sdk import Context, plugin

from .routes import TvStore, build_router

PLUGIN_ID = "tv-home"


@plugin(PLUGIN_ID, title="电视桌面", inject=(PLUGIN_FILES, PLUGIN_ROUTES), permissions=())
async def apply(ctx: Context) -> None:
    files = ctx.use(PLUGIN_FILES).scoped(ctx)
    store = TvStore(files)
    ctx.use(PLUGIN_ROUTES).mount(ctx, build_router(store))
