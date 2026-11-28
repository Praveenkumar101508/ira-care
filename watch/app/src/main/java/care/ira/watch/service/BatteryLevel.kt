package care.ira.watch.service

/** The battery percentage the watch reports. Pure Kotlin: tested without a watch. */
object BatteryLevel {
    /**
     * `property`: BatteryManager.BATTERY_PROPERTY_CAPACITY (Int.MIN_VALUE or 0 when unsupported on some
     * devices); `level` and `scale`: the last ACTION_BATTERY_CHANGED broadcast. The broadcast is preferred
     * when present, since it is what the system itself shows; the property is the fallback. Null when
     * neither gives a value in 0..100: the server then shows the battery as unknown, never as a guess.
     */
    fun percent(property: Int?, level: Int?, scale: Int?): Int? {
        if (level != null && scale != null && level >= 0 && scale > 0) {
            val p = Math.round(level * 100f / scale)
            if (p in 0..100) return p
        }
        return property?.takeIf { it in 1..100 }
    }
}
