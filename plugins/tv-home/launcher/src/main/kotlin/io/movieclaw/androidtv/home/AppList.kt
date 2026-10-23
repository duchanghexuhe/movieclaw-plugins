package io.movieclaw.androidtv.home

import android.content.Context
import android.content.Intent
import android.content.pm.ActivityInfo
import android.graphics.drawable.Drawable
import java.text.Collator

/** MovieClaw 主 App 的包名 */
const val MOVIECLAW = "io.movieclaw.androidtv"

/** 「全部应用」里的一格 */
class App(val intent: Intent, val label: String, val image: Drawable, val banner: Boolean) {
    val featured = intent.component?.packageName == MOVIECLAW

    /** 按组件取的稳定 id：应用升级、列表重排后，焦点仍认得它 */
    val id = intent.component.hashCode().toLong()
}

/** 可启动的应用（§6.3）。只在后台线程用 */
class AppList(private val context: Context) {
    private var shown: Set<String>? = null

    /** 有应用装上、卸掉、升级、停用才返回新列表：MovieClaw 排第一，其余按名称排序；有横幅用横幅，没有用图标 */
    fun reload(): List<App>? {
        val entries = launchables()
        val keys = entries.mapTo(HashSet()) { (info, _) -> "${info.packageName}/${info.name}@${info.applicationInfo.sourceDir}" }
        if (keys == shown) return null
        shown = keys
        val pm = context.packageManager
        return entries.map { (info, category) ->
            val banner = info.loadBanner(pm)
            App(
                Intent(Intent.ACTION_MAIN).addCategory(category).setClassName(info.packageName, info.name)
                    .addFlags(Intent.FLAG_ACTIVITY_RESET_TASK_IF_NEEDED),
                info.loadLabel(pm).toString(),
                banner ?: info.loadIcon(pm),
                banner != null,
            )
        }.sortedWith(compareBy<App> { !it.featured }.thenBy(Collator.getInstance()) { it.label })
    }

    /** 每个包一格：电视入口（LEANBACK_LAUNCHER）优先，没有的用普通入口（LAUNCHER）；不含桌面自己 */
    private fun launchables(): List<Pair<ActivityInfo, String>> {
        val seen = hashSetOf(context.packageName)
        return listOf(Intent.CATEGORY_LEANBACK_LAUNCHER, Intent.CATEGORY_LAUNCHER).flatMap { category ->
            context.packageManager.queryIntentActivities(Intent(Intent.ACTION_MAIN).addCategory(category), 0)
                .map { it.activityInfo }
                .filter { seen.add(it.packageName) }
                .map { it to category }
        }
    }
}
