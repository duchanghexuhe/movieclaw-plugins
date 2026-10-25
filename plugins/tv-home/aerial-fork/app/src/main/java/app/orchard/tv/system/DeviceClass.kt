package app.orchard.tv.system

import android.app.ActivityManager
import android.content.Context
import android.os.Build

/**
 * Whether this TV is one of the slow ones, for the Effects setting's Auto (AerialSettings.lite).
 * Read from what the system says about itself, not from a list of models:
 * - the firmware's own low-RAM flag (`ro.config.low_ram`): Xiaomi MiTV-MFTP0, Konka U86V9, Coocaa
 *   C10 and OPPO K9 set it; the system already trims itself there;
 * - under 1.8 GB of memory: the 1.5-1.7 GB Xiaomi MiTV4 / ASTP0 boards and old boxes;
 * - Android 6 and older: these are all several years old (Xiaomi MiTV4, Letv, Konka, Whaley);
 * - two cores or fewer.
 * TVs with 2-4 GB on Android 9-14 (Xiaomi MFTR0, TCL MT5879, Hisense) are not counted.
 */
object DeviceClass {
    @Volatile private var known: Boolean? = null

    fun lowEnd(context: Context): Boolean = known ?: run {
        val am = context.getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager
        val info = ActivityManager.MemoryInfo().also(am::getMemoryInfo)
        val mb = info.totalMem / (1 shl 20)
        (am.isLowRamDevice || mb < 1800 || Build.VERSION.SDK_INT < 24 || Runtime.getRuntime().availableProcessors() <= 2)
            .also { known = it }
    }

    /** One line for diagnostics: what the check saw. */
    fun describe(context: Context): String {
        val am = context.getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager
        val info = ActivityManager.MemoryInfo().also(am::getMemoryInfo)
        return "low-end=${lowEnd(context)} (low-RAM flag ${am.isLowRamDevice}, ${info.totalMem / (1 shl 20)} MB, " +
            "${Runtime.getRuntime().availableProcessors()} cores, memory class ${am.memoryClass} MB)"
    }
}
