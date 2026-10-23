package io.movieclaw.androidtv.home

import android.app.Activity
import android.app.ActivityOptions
import android.content.ActivityNotFoundException
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.view.View
import android.window.OnBackInvokedDispatcher
import androidx.recyclerview.widget.GridLayoutManager
import androidx.recyclerview.widget.RecyclerView
import java.util.concurrent.Executors

private const val COLUMNS = 5
private const val LAST_OPEN = "last_open"
private const val LOOP_GUARD_MS = 10_000L

/**
 * MovieClaw 桌面（主仓库 docs/design/androidtv-home.md §5、§6）：收到主页意图就打开 MovieClaw，其余时候显示「全部应用」。
 *
 * 只在两种时候自动打开（§6.2，两种模拟器上逐条实测）：带主页意图的全新创建（没有保存的状态，即开机），以及收到新的主页意图
 * （onNewIntent；进程被杀后按主页键，系统重建后也会补发）。不能只看 getIntent()：进程被杀后带着保存的状态重建时，
 * 它仍是原来的主页意图，会把「从别的应用返回」误判成开机。
 */
class HomeActivity : Activity() {
    private val loader = Executors.newSingleThreadExecutor()
    private val list = AppList(this)
    private val apps = AppsAdapter(::launch)
    private lateinit var grid: RecyclerView
    private lateinit var cover: View

    /** 刚打开了 MovieClaw：它盖上来之前只露黑底，不画应用页 */
    private var opening = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_home)
        cover = findViewById(R.id.cover)
        grid = findViewById<RecyclerView>(R.id.apps).apply {
            layoutManager = GridLayoutManager(context, COLUMNS).apply { spanSizeLookup = apps.spans(COLUMNS) }
            adapter = apps
        }
        findViewById<View>(R.id.settings).setOnClickListener { start(Intent(Settings.ACTION_SETTINGS)) }
        // 「全部应用」按返回：不动（§5）。Android 13 起走这个回调，更早的走 onBackPressed
        if (Build.VERSION.SDK_INT >= 33) {
            onBackInvokedDispatcher.registerOnBackInvokedCallback(OnBackInvokedDispatcher.PRIORITY_DEFAULT) {}
        }
        if (savedInstanceState == null && intent.isHome() && !openedRecently()) openMovieClaw()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        if (intent.isHome()) openMovieClaw()
    }

    override fun onResume() {
        super.onResume()
        // onNewIntent 之后一定有 onResume。之后再回到前台（从别的应用退回、MovieClaw 退出或一启动就崩）都显示「全部应用」
        cover.visibility = if (opening) View.VISIBLE else View.GONE
        if (!opening) refresh()
        opening = false
    }

    override fun onDestroy() {
        loader.shutdownNow()
        super.onDestroy()
    }

    @Deprecated("Android 12 及以下的返回键")
    override fun onBackPressed() = Unit

    /** 打开 MovieClaw 首页（§6.2）。老版本不认这条深链，效果是把 MovieClaw 调到前台、停在原来的页面 */
    private fun openMovieClaw() {
        val home = Intent(Intent.ACTION_VIEW, Uri.parse("movieclaw://home")).setPackage(MOVIECLAW)
        opening = start(home, ActivityOptions.makeCustomAnimation(this, 0, 0).toBundle())
        if (opening) prefs().edit().putLong(LAST_OPEN, System.currentTimeMillis()).apply()
    }

    /** 防连环拉起（§6.2）：开机时距上次自动打开不到 10 秒，多半是 MovieClaw 一启动就崩、桌面也跟着被重建 */
    private fun openedRecently() = System.currentTimeMillis() - prefs().getLong(LAST_OPEN, 0) in 0 until LOOP_GUARD_MS

    private fun prefs() = getSharedPreferences("home", MODE_PRIVATE)

    /** 应用列表在后台线程读；和现在显示的一样就不动，焦点留在上次的位置 */
    private fun refresh() = loader.execute {
        val loaded = list.reload() ?: return@execute
        runOnUiThread {
            val first = apps.itemCount == 0
            apps.submit(loaded)
            // 第一次落在 MovieClaw（排第一格）
            if (first) grid.post { grid.requestFocus() }
        }
    }

    private fun launch(app: App) {
        start(app.intent)
    }

    /** 打开的应用都在自己的任务里；没装、被停用、不让打开的，停在原地 */
    private fun start(intent: Intent, options: Bundle? = null) = try {
        startActivity(intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK), options)
        true
    } catch (_: ActivityNotFoundException) {
        false
    } catch (_: SecurityException) {
        false
    }

    private fun Intent.isHome() = action == Intent.ACTION_MAIN && hasCategory(Intent.CATEGORY_HOME)
}
