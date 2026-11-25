package care.ira.watch.ui

import java.io.InputStream

/**
 * The watch's third-party notices: assets/THIRD_PARTY_NOTICES.txt, which each flavour carries
 * from its own source set (src/wearos/assets, src/aosp/assets), written from
 * docs/legal/notices/watch-<flavour>.txt by `scripts/supply_chain.py render`. Shown under
 * About, Open-source licences. Pure Kotlin, tested on the JVM.
 */
object Notices {
    const val ASSET = "THIRD_PARTY_NOTICES.txt"
    const val HEADING = "THIRD-PARTY NOTICES: "

    /** The notices from [open] (the app's assets). A file that is not a notices file is an
     *  error, never an empty screen that looks as if nothing needed attributing. */
    fun read(open: (String) -> InputStream): String {
        val text = open(ASSET).use { it.readBytes().toString(Charsets.UTF_8) }
        require(text.startsWith(HEADING)) { "$ASSET is not a third-party notices file" }
        return text
    }

    /** The build the notices describe: their first line, without the heading. */
    fun title(text: String): String = text.substringBefore('\n').removePrefix(HEADING).trim()

    /** Paragraphs, so a long file scrolls as a list on a small round screen. */
    fun paragraphs(text: String): List<String> =
        text.split(Regex("\n{2,}")).map { it.trimEnd() }.filter { it.isNotEmpty() }
}
