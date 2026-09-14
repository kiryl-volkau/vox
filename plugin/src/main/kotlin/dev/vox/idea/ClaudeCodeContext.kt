package dev.vox.idea

import com.google.gson.JsonArray
import com.google.gson.JsonElement
import com.google.gson.JsonObject
import com.google.gson.JsonParser
import com.google.gson.JsonSyntaxException
import com.intellij.openapi.diagnostic.logger
import com.intellij.openapi.project.Project
import java.io.IOException
import java.nio.charset.StandardCharsets
import java.nio.file.Files
import java.nio.file.Path
import kotlin.io.path.isDirectory
import kotlin.io.path.isRegularFile
import kotlin.io.path.listDirectoryEntries

/**
 * The recent conversation from the Claude Code session running in this project.
 *
 * Claude Code appends one JSON object per line to
 * `~/.claude/projects/<project path with separators replaced by "-">/<session id>.jsonl`, and the
 * newest of those files is the session in progress. Vox reads the tail of it so a dictated request
 * can say "this method" or "what we just discussed" and still be understood.
 *
 * Reading only: nothing is ever written back, and a session file that is missing, unreadable or
 * shaped differently yields no context rather than an error - the context is an optimisation and
 * must never cost the user a request.
 */
internal object ClaudeCodeContext {

    /** One captured exchange, already reduced to plain text. */
    private class Exchange(val request: String, val reply: String)

    /** The conversation Vox will send, plus what it was drawn from. */
    class Conversation(
        val text: String,
        val exchanges: Int,
        val sessionId: String,
        /** What the registry calls this session, when it was identified by its terminal. */
        val sessionName: String? = null,
        /** False when the session was guessed from timestamps rather than resolved from the tab. */
        val resolvedFromTerminal: Boolean = false,
    )

    /**
     * Reads the last [exchanges] request/reply pairs of the session this dictation belongs to.
     *
     * [shellPid] is the pid of the shell in the terminal tab being dictated into, and it is what
     * makes the answer right when a project has several sessions open: they all append to the same
     * directory, so picking the newest-modified transcript picks whichever session answered last
     * rather than the one in front of the user. Pass null, or let the resolution fail, and it falls
     * back to that older guess - worse, but what it always did.
     *
     * Returns null when Claude Code has never run here, when the session holds nothing usable, or
     * when either budget is zero. The result is capped at [maxBytes] UTF-8 bytes by dropping whole
     * exchanges from the oldest end, so the newest turn - the one the speech is most likely to
     * refer to - is the last thing dropped.
     *
     * Does no VFS work and blocks on file I/O and process enumeration, so it belongs on a
     * background thread.
     */
    fun read(project: Project, exchanges: Int, maxBytes: Int, shellPid: Long? = null): Conversation? {
        if (exchanges <= 0 || maxBytes <= 0) return null
        val base = project.basePath ?: return null
        val resolved = shellPid?.let { ClaudeSessions.forShell(it) }
        val session = resolved?.let { transcriptOf(it, base) } ?: newestSession(base) ?: return null
        val collected = readExchanges(session, exchanges) ?: return null
        if (collected.isEmpty()) return null
        val kept = fit(collected, maxBytes)
        if (kept.isEmpty()) return null
        val text = kept.joinToString("\n\n") { "user: ${it.request}\nassistant: ${it.reply}" }
        val id = session.fileName.toString().removeSuffix(SESSION_SUFFIX)
        val matched = resolved?.takeIf { it.id == id }
        return Conversation(text, kept.size, id, matched?.name, matched != null)
    }

    /**
     * The transcript of an identified session, or null when it is not where it should be.
     *
     * The directory comes from the session's own working directory rather than from the project
     * root: a session started after a `cd` into a subdirectory writes somewhere else entirely, and
     * the project root would find nothing. [base] covers an entry that does not say.
     */
    private fun transcriptOf(session: ClaudeSessions.Session, base: String): Path? {
        val directory = sessionDirectory(session.workingDirectory ?: base) ?: return null
        val file = directory.resolve(session.id + SESSION_SUFFIX)
        return file.takeIf { it.isRegularFile() && Files.size(it) in 1..MAX_SESSION_BYTES }
    }

    /** True when this project has a Claude Code session directory at all. */
    fun isAvailable(project: Project): Boolean {
        val base = project.basePath ?: return false
        return sessionDirectory(base)?.isDirectory() == true
    }

    /**
     * Returns the directory Claude Code stores this project's sessions in.
     *
     * The name is the absolute project path with every drive colon and path separator replaced by
     * a hyphen, which is how Claude Code mangles a working directory into a single path segment.
     */
    private fun sessionDirectory(base: String): Path? {
        val mangled = base.replace(':', '-').replace('\\', '-').replace('/', '-')
        if (mangled.isBlank()) return null
        val home = System.getProperty("user.home") ?: return null
        return Path.of(home, ".claude", "projects", mangled)
    }

    private fun newestSession(base: String): Path? {
        val directory = sessionDirectory(base) ?: return null
        if (!directory.isDirectory()) return null
        return try {
            directory
                .listDirectoryEntries("*$SESSION_SUFFIX")
                .filter { it.isRegularFile() && Files.size(it) in 1..MAX_SESSION_BYTES }
                .maxByOrNull { Files.getLastModifiedTime(it).toMillis() }
        } catch (e: IOException) {
            log.debug("cannot list Claude Code sessions in $directory", e)
            null
        }
    }

    private fun readExchanges(session: Path, wanted: Int): List<Exchange>? {
        val lines =
            try {
                Files.readAllLines(session, StandardCharsets.UTF_8)
            } catch (e: IOException) {
                log.debug("cannot read $session", e)
                return null
            } catch (e: OutOfMemoryError) {
                log.warn("Claude Code session $session is too large to read", e)
                return null
            }

        // Walked newest first so only the tail is ever parsed: these files reach megabytes, and
        // everything before the last few exchanges is thrown away regardless.
        val found = ArrayDeque<Exchange>()
        // One assistant turn spans several records, one per block. Collecting them and prepending
        // keeps the turn in the order it was written, so the reply is the whole answer rather than
        // whichever block the walk happened to see last - which is the opening line, not the point.
        val reply = ArrayDeque<String>()
        for (index in lines.indices.reversed()) {
            val record = parse(lines[index]) ?: continue
            if (record.get("isSidechain")?.asBooleanOrNull() == true) continue
            when (record.stringOrNull("type")) {
                "assistant" -> textOf(record).takeIf { it.isNotBlank() }?.let { reply.addFirst(it) }
                "user" -> {
                    // A tool result is also stored as a "user" record, carrying result blocks and
                    // no text. It is part of the turn, not the end of it: treating it as a boundary
                    // would cut every reply off at its first tool call and keep only the preamble.
                    val request = textOf(record)
                    if (request.isBlank()) continue
                    val answer = clean(reply.joinToString("\n"))
                    if (answer.isNotBlank()) {
                        found.addFirst(Exchange(request, answer))
                        if (found.size >= wanted) return found.toList()
                    }
                    reply.clear()
                }
            }
        }
        return found.toList()
    }

    private fun fit(exchanges: List<Exchange>, maxBytes: Int): List<Exchange> {
        var kept = exchanges
        while (kept.isNotEmpty() && byteLength(kept) > maxBytes) kept = kept.drop(1)
        return kept
    }

    // Cyrillic costs two bytes per character, so a character count would let a Russian
    // conversation through at twice the budget the user configured.
    private fun byteLength(exchanges: List<Exchange>): Int =
        exchanges.sumOf {
            it.request.toByteArray(StandardCharsets.UTF_8).size +
                it.reply.toByteArray(StandardCharsets.UTF_8).size +
                SEPARATOR_BYTES
        }

    private fun parse(line: String): JsonObject? {
        if (line.isBlank()) return null
        return try {
            JsonParser.parseString(line) as? JsonObject
        } catch (e: JsonSyntaxException) {
            log.debug("skipping an unparseable session line", e)
            null
        }
    }

    /**
     * Extracts the human-readable text of one record.
     *
     * `message.content` is either a plain string or a list of typed blocks. Only `text` blocks are
     * taken: tool calls, tool results and images are noise here, and a hook or command injection
     * (marked `isMeta`) is not something the user said. Anything wrapped in a system reminder is
     * dropped for the same reason.
     */
    private fun textOf(record: JsonObject): String {
        if (record.get("isMeta")?.asBooleanOrNull() == true) return ""
        val message = record.getAsJsonObject("message") ?: return ""
        val content = message.get("content") ?: return ""
        val text =
            when {
                content.isJsonPrimitive -> content.asString
                content is JsonArray -> content.mapNotNull { blockText(it) }.joinToString("\n")
                else -> ""
            }
        return clean(text)
    }

    private fun blockText(block: JsonElement): String? {
        val obj = block as? JsonObject ?: return null
        if (obj.stringOrNull("type") != "text") return null
        return obj.stringOrNull("text")
    }

    private fun clean(text: String): String {
        val withoutReminders = SYSTEM_REMINDER.replace(text, "")
        val collapsed = withoutReminders.trim()
        if (collapsed.length <= MAX_MESSAGE_CHARS) return collapsed
        return collapsed.take(MAX_MESSAGE_CHARS).substringBeforeLast(' ') + "…"
    }

    private fun JsonObject.stringOrNull(name: String): String? =
        get(name)?.takeIf { it.isJsonPrimitive }?.asString

    private fun JsonElement.asBooleanOrNull(): Boolean? =
        if (isJsonPrimitive && asJsonPrimitive.isBoolean) asBoolean else null

    private const val SESSION_SUFFIX = ".jsonl"
    private const val MAX_SESSION_BYTES = 256L * 1024 * 1024
    private const val MAX_MESSAGE_CHARS = 2000
    private const val SEPARATOR_BYTES = 24
    private val SYSTEM_REMINDER = Regex("<system-reminder>.*?</system-reminder>", RegexOption.DOT_MATCHES_ALL)
    private val log = logger<ClaudeCodeContext>()
}
