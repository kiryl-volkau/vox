package dev.vox.idea

/**
 * The output languages the settings offer, mirroring `vox_server.languages`.
 *
 * The list only drives the picker; the backend accepts any code it is given, so a language missing
 * here can still be configured by hand in `vox.xml`.
 */
internal object VoxLanguages {

    class Language(val code: String, val englishName: String, val nativeName: String) {
        /** "English" for English, "Russian (Русский)" for everything else. */
        val label: String
            get() = if (nativeName == englishName) englishName else "$englishName ($nativeName)"
    }

    const val DEFAULT_CODE = "en"

    val ALL =
        listOf(
            Language("en", "English", "English"),
            Language("ru", "Russian", "Русский"),
            Language("de", "German", "Deutsch"),
            Language("fr", "French", "Français"),
            Language("es", "Spanish", "Español"),
            Language("it", "Italian", "Italiano"),
            Language("pt", "Portuguese", "Português"),
            Language("nl", "Dutch", "Nederlands"),
            Language("pl", "Polish", "Polski"),
            Language("uk", "Ukrainian", "Українська"),
            Language("tr", "Turkish", "Türkçe"),
            Language("zh", "Chinese", "中文"),
            Language("ja", "Japanese", "日本語"),
            Language("ko", "Korean", "한국어"),
        )

    private val byCode = ALL.associateBy { it.code }

    /**
     * Returns the canonical code for [value], or null when it names nothing.
     *
     * Accepts a code ("EN", "en-GB", "ru_RU") case- and separator-insensitively, and keeps an
     * unlisted two- or three-letter code rather than rejecting it, because the backend's Whisper
     * knows far more languages than [ALL] lists.
     */
    fun normalise(value: String?): String? {
        val cleaned = value?.trim().orEmpty()
        if (cleaned.isEmpty()) return null
        val base = cleaned.replace('_', '-').substringBefore('-').lowercase()
        if (base in byCode) return base
        return base.takeIf { it.length in 2..3 && it.all(Char::isLetter) }
    }

    /** The label for [code], falling back to the bare code for a language not in [ALL]. */
    fun label(code: String): String = byCode[code]?.label ?: code

    /** [ALL] plus [code] itself when it was configured by hand and is not on the list. */
    fun optionsIncluding(code: String): List<String> {
        val codes = ALL.map { it.code }
        return if (code in codes) codes else codes + code
    }
}
