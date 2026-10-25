package app.orchard.tv.system

import android.os.Build
import android.os.Handler
import android.os.HandlerThread
import android.os.SystemClock
import android.view.FrameMetrics
import android.view.Window
import androidx.annotation.RequiresApi

/**
 * How long Home's last frames took, for the diagnostics report: a report that says "it stutters"
 * then comes with numbers from the reporter's own TV. Android 7+ (FrameMetrics); the system hands
 * over each frame's times after it is drawn, on a thread of its own. Nothing runs while nothing is
 * drawn, so it costs nothing when Home is still.
 */
object FrameStats {
    private const val KEEP = 600
    private const val SKIP_START_MS = 5_000L
    private const val BUDGET_NS = 16_700_000L
    private val total = LongArray(KEEP)
    private var count = 0L
    private var over = 0L
    private var thread: HandlerThread? = null

    fun attach(window: Window) {
        if (Build.VERSION.SDK_INT < 24) return
        listen(window)
    }

    @RequiresApi(24)
    private fun listen(window: Window) {
        val t = thread ?: HandlerThread("AerialFrames").also { it.start(); thread = it }
        val from = SystemClock.uptimeMillis() + SKIP_START_MS
        window.addOnFrameMetricsAvailableListener({ _, metrics, _ ->
            // Home's first seconds are its start (up to a second a frame on TCL MT5879), not the
            // TV's pace while it is used; the start has its own lines in DiagLog.
            if (SystemClock.uptimeMillis() < from || metrics.getMetric(FrameMetrics.FIRST_DRAW_FRAME) == 1L) {
                return@addOnFrameMetricsAvailableListener
            }
            val ns = metrics.getMetric(FrameMetrics.TOTAL_DURATION)
            synchronized(this) {
                total[(count % KEEP).toInt()] = ns
                count++
                if (ns > BUDGET_NS) over++
            }
        }, Handler(t.looper))
    }

    /** One line: how many frames, the median, 90th and 99th percentiles and the slowest of the last [KEEP]. */
    fun summary(): String {
        if (Build.VERSION.SDK_INT < 24) return "not measured before Android 7"
        val (n, all, slow, sorted) = synchronized(this) {
            val n = minOf(count, KEEP.toLong()).toInt()
            Quad(n, count, over, total.copyOf(n).also { it.sort() })
        }
        if (n == 0) return "no frames yet"
        fun ms(p: Double) = "%.1f".format(sorted[((n - 1) * p).toInt()] / 1e6)
        return "last $n frames: median ${ms(0.5)} ms, p90 ${ms(0.9)} ms, p99 ${ms(0.99)} ms, slowest ${ms(1.0)} ms; " +
            "over 16.7 ms: $slow of $all since 5 s after start"
    }

    private data class Quad(val n: Int, val all: Long, val slow: Long, val sorted: LongArray)
}
