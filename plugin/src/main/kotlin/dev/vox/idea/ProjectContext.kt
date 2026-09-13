package dev.vox.idea

import com.intellij.openapi.diagnostic.logger
import com.intellij.openapi.project.Project
import java.io.IOException
import java.nio.charset.StandardCharsets
import java.nio.file.Path

/** The per-project `.vox.md` whose text steers the model for one repository. */
internal object ProjectContext {

    const val FILE_NAME = ".vox.md"

    class ProjectFile(val name: String, val text: String)

    /**
     * Reads `<project base>/.vox.md`, truncated to [maxBytes] UTF-8 bytes on a line boundary where
     * one exists, because the backend charges a full prompt prefill for anything oversized.
     *
     * Returns null when the project has no base path, the file is absent, empty, unreadable, or
     * [maxBytes] is zero: a project file is an optimisation and must never cost the user a request.
     * Does no VFS work, so it is safe to call from a background thread without a read action.
     */
    fun read(project: Project, maxBytes: Int): ProjectFile? {
        if (maxBytes <= 0) return null
        val base = project.basePath ?: return null
        val path = Path.of(base, FILE_NAME)
        val text =
            try {
                path.toFile().readText()
            } catch (e: IOException) {
                log.debug("cannot read $path", e)
                return null
            }
        if (text.isBlank()) return null
        return ProjectFile(project.name, truncate(text, maxBytes, path))
    }

    private fun truncate(text: String, maxBytes: Int, path: Path): String {
        val encoded = text.toByteArray(StandardCharsets.UTF_8)
        if (encoded.size <= maxBytes) return text
        log.warn("$path is ${encoded.size} bytes, sending only the first $maxBytes")
        // A byte cut can split a character; the decoder leaves U+FFFD behind, so drop it.
        val clipped = String(encoded, 0, maxBytes, StandardCharsets.UTF_8).trimEnd('\uFFFD')
        val boundary = clipped.lastIndexOf('\n')
        return if (boundary > 0) clipped.substring(0, boundary) else clipped
    }

    private val log = logger<ProjectContext>()
}
