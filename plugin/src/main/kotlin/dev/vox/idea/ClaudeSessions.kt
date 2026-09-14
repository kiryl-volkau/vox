package dev.vox.idea

import com.google.gson.JsonObject
import com.google.gson.JsonParser
import com.google.gson.JsonSyntaxException
import com.intellij.openapi.diagnostic.logger
import java.io.IOException
import java.nio.charset.StandardCharsets
import java.nio.file.Files
import java.nio.file.Path
import java.time.Instant
import kotlin.io.path.isRegularFile

/**
 * Which Claude Code session is running in a given terminal tab.
 *
 * Claude Code registers every running session in `~/.claude/sessions/<pid>.json`, so a session can
 * be identified by the process it runs as rather than by guessing from file timestamps. That
 * matters as soon as a project has more than one session open: they all append to the same
 * directory, and the newest-modified file is whichever session answered last, not the one the
 * person is typing into.
 *
 * The chain is: terminal tab -> its shell pid -> the `claude` process descended from that shell ->
 * the registry entry for that pid -> the session id, which is the name of the transcript file.
 *
 * None of this is a published schema. The registry is documented as a mechanism - sessions
 * register themselves in files on disk so other sessions can find them - but the path and the
 * field names are not contractual, so every field is optional here and every failure returns null.
 * The caller falls back to the old heuristic, which is worse but never wrong in a new way.
 */
internal object ClaudeSessions {

    /** A live session, as the registry describes it. */
    class Session(val id: String, val workingDirectory: String?, val name: String?)

    /**
     * The session running in the terminal whose shell has [shellPid], or null when there is none.
     *
     * Null covers every honest outcome: no `claude` running in that tab, a registry that does not
     * exist, an entry that has gone stale, or a file shaped differently than expected. Blocks on
     * file I/O and process enumeration, so keep it off the EDT.
     */
    fun forShell(shellPid: Long): Session? {
        val claude = claudeUnder(shellPid) ?: return null
        val entry = registryEntry(claude.pid()) ?: return null
        if (!startsMatch(entry, claude)) {
            // The pid was reused: the registry describes a process that has since died and whose
            // number a new one now carries. Reporting its session would be confidently wrong.
            log.debug("registry entry for pid ${claude.pid()} predates the process holding it")
            return null
        }
        val id = entry.stringOrNull("sessionId") ?: return null
        return Session(id, entry.stringOrNull("cwd"), entry.stringOrNull("name"))
    }

    /**
     * The innermost `claude` process running under [shellPid].
     *
     * The newest one wins: a session started inside another - `claude` run from a shell that
     * `claude` itself spawned - is the one in front of the user. A process that exited between
     * listing and inspection simply drops out.
     */
    private fun claudeUnder(shellPid: Long): ProcessHandle? =
        try {
            ProcessHandle.of(shellPid)
                .orElse(null)
                ?.descendants()
                ?.filter { isClaude(it) }
                ?.max(compareBy { it.info().startInstant().orElse(Instant.EPOCH) })
                ?.orElse(null)
        } catch (e: RuntimeException) {
            // Enumerating processes can fail on a locked-down machine; it is never worth a request.
            log.debug("cannot walk the processes under shell $shellPid", e)
            null
        }

    private fun isClaude(handle: ProcessHandle): Boolean {
        val command = handle.info().command().orElse(null) ?: return false
        val name = command.substringAfterLast('\\').substringAfterLast('/').lowercase()
        return name == "claude" || name == "claude.exe"
    }

    private fun registryEntry(pid: Long): JsonObject? {
        val file = registryDirectory()?.resolve("$pid.json") ?: return null
        if (!file.isRegularFile()) return null
        return try {
            val text = Files.readString(file, StandardCharsets.UTF_8)
            JsonParser.parseString(text) as? JsonObject
        } catch (e: IOException) {
            log.debug("cannot read the session registry entry $file", e)
            null
        } catch (e: JsonSyntaxException) {
            log.debug("session registry entry $file is not JSON", e)
            null
        }
    }

    private fun registryDirectory(): Path? {
        val home = System.getProperty("user.home") ?: return null
        return Path.of(home, ".claude", "sessions")
    }

    /**
     * Whether the entry really describes the process now holding that pid.
     *
     * ``procStart`` is a Windows FILETIME of the process start, which matches
     * ``ProcessHandle.info().startInstant()`` to the millisecond. Without the check a recycled pid
     * would resolve to a dead session's transcript. An entry that carries no usable start, or a
     * process that will not report one, is accepted: the guard exists to catch a mismatch, not to
     * reject everything it cannot prove.
     */
    private fun startsMatch(entry: JsonObject, handle: ProcessHandle): Boolean {
        val recorded = entry.get("procStart")?.takeIf { it.isJsonPrimitive }?.asString?.toLongOrNull()
            ?: return true
        val actual = handle.info().startInstant().orElse(null) ?: return true
        val recordedMillis = (recorded - FILETIME_EPOCH_OFFSET) / FILETIME_TICKS_PER_MILLI
        return recordedMillis == actual.toEpochMilli()
    }

    private fun JsonObject.stringOrNull(name: String): String? =
        get(name)?.takeIf { it.isJsonPrimitive }?.asString?.takeIf { it.isNotBlank() }

    // 100-nanosecond ticks between 1601-01-01 and 1970-01-01, and per millisecond.
    private const val FILETIME_EPOCH_OFFSET = 116_444_736_000_000_000L
    private const val FILETIME_TICKS_PER_MILLI = 10_000L
    private val log = logger<ClaudeSessions>()
}
