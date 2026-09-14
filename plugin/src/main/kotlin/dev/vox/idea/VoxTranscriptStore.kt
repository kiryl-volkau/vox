package dev.vox.idea

import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.components.PersistentStateComponent
import com.intellij.openapi.components.Service
import com.intellij.openapi.components.State
import com.intellij.openapi.components.Storage
import com.intellij.openapi.components.service
import com.intellij.openapi.project.Project
import com.intellij.openapi.wm.ToolWindowManager
import com.intellij.util.xmlb.annotations.XCollection
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.UUID

/**
 * Every dictation this project has produced, kept across IDE restarts.
 *
 * The terminal only ever receives the finished message, so without this the raw transcript is
 * gone the moment the model rewrote it. Keeping both is what makes a bad rewrite diagnosable,
 * and it is what the workbench replays: a stored transcript can be sent through a prompt again
 * without dictating it again.
 *
 * Persisted per project under the IDE's own state rather than in the repository, because these
 * are transcripts of everything the user said and they have no business in git.
 */
@Service(Service.Level.PROJECT)
@State(name = "VoxTranscripts", storages = [Storage("vox-transcripts.xml")])
class VoxTranscriptStore(private val project: Project) :
    PersistentStateComponent<VoxTranscriptStore.State> {

    /**
     * One dictation, in the shape the XML serialiser can round-trip.
     *
     * Every field is a mutable primitive with a default: the serialiser needs a no-argument
     * constructor, and a state file written by an older build must not fail to load.
     */
    class Entry {
        var id: String = ""
        var at: String = ""
        var transcript: String = ""
        var output: String = ""
        var language: String = ""
        var device: String = ""
        var contextExchanges: Int = 0
        var delivery: String = ""
        var error: String = ""

        /** The time this was dictated, or null when the stored stamp is unreadable. */
        fun instant(): Instant? = runCatching { Instant.parse(at) }.getOrNull()

        /** "HH:mm:ss" in the local zone, or "--:--:--" when the stamp is unreadable. */
        fun time(): String =
            instant()?.atZone(ZoneId.systemDefault())?.format(TIME) ?: "--:--:--"

        /** One line naming this entry in a list: the transcript, clipped. */
        fun summary(): String {
            val text = transcript.replace('\n', ' ').trim().ifEmpty { "(nothing recognised)" }
            return if (text.length <= SUMMARY_CHARS) text else text.take(SUMMARY_CHARS) + "…"
        }

        fun failed(): Boolean = error.isNotEmpty()
    }

    class State {
        @XCollection(style = XCollection.Style.v2)
        var entries: MutableList<Entry> = mutableListOf()
    }

    fun interface Listener {
        fun onChanged()
    }

    private var state = State()
    private var listener: Listener? = null

    override fun getState(): State = state

    override fun loadState(state: State) {
        this.state = state
        trim()
    }

    /** Newest first, which is the order the workbench lists them in. */
    fun entries(): List<Entry> = synchronized(this) { state.entries.reversed() }

    fun find(id: String): Entry? = synchronized(this) { state.entries.firstOrNull { it.id == id } }

    /**
     * Records one dictation and returns it. Safe from any thread.
     *
     * ``transcript`` and ``output`` are stored as given; a blank transcript is kept rather than
     * dropped, because a request that recognised nothing is exactly the one worth looking at.
     */
    fun add(
        transcript: String,
        output: String,
        language: String,
        device: String,
        contextExchanges: Int,
        delivery: String,
        error: String? = null,
    ): Entry {
        val entry = Entry().apply {
            this.id = UUID.randomUUID().toString()
            this.at = Instant.now().toString()
            this.transcript = transcript
            this.output = output
            this.language = language
            this.device = device
            this.contextExchanges = contextExchanges
            this.delivery = delivery
            this.error = error.orEmpty()
        }
        synchronized(this) {
            state.entries.add(entry)
            trim()
        }
        notifyChanged()
        return entry
    }

    fun remove(id: String) {
        synchronized(this) { state.entries.removeIf { it.id == id } }
        notifyChanged()
    }

    fun clear() {
        synchronized(this) { state.entries.clear() }
        notifyChanged()
    }

    internal fun setListener(value: Listener?) {
        listener = value
    }

    /** Opens the Vox tool window without taking focus from the editor or the terminal. */
    fun show() {
        ApplicationManager.getApplication().invokeLater(
            { ToolWindowManager.getInstance(project).getToolWindow(TOOL_WINDOW_ID)?.show(null) },
            project.disposed,
        )
    }

    /** The whole history as plain text, for copying out. */
    fun render(): String {
        val entries = entries()
        if (entries.isEmpty()) return EMPTY_TEXT
        return entries.joinToString("\n\n", transform = ::render)
    }

    private fun render(entry: Entry): String = buildString {
        append(entry.time())
        append("  ")
        append(entry.language)
        append(" · ")
        append(entry.device)
        if (entry.contextExchanges > 0) append(" · context: ${entry.contextExchanges} exchanges")
        appendLine()
        append("  heard  ")
        append(indent(entry.transcript))
        if (entry.failed()) {
            appendLine()
            append("  failed ")
            append(indent(entry.error))
            return@buildString
        }
        appendLine()
        append("  sent   ")
        append(indent(entry.output))
        if (entry.delivery.isNotEmpty()) {
            appendLine()
            append("  → ")
            append(entry.delivery)
        }
    }

    private fun indent(text: String): String = text.trim().replace("\n", "\n         ")

    private fun trim() {
        val excess = state.entries.size - MAX_ENTRIES
        if (excess > 0) repeat(excess) { state.entries.removeAt(0) }
    }

    private fun notifyChanged() {
        val target = listener ?: return
        ApplicationManager.getApplication().invokeLater({ target.onChanged() }, project.disposed)
    }

    companion object {
        const val TOOL_WINDOW_ID = "Vox"
        const val EMPTY_TEXT = "Nothing dictated yet. Press Ctrl+Alt+Shift+D to start."

        private const val MAX_ENTRIES = 200
        private const val SUMMARY_CHARS = 60
        private val TIME: DateTimeFormatter = DateTimeFormatter.ofPattern("HH:mm:ss")

        fun getInstance(project: Project): VoxTranscriptStore = project.service()
    }
}
