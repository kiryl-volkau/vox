package dev.vox.idea

import com.intellij.openapi.components.PersistentStateComponent
import com.intellij.openapi.components.Service
import com.intellij.openapi.components.State
import com.intellij.openapi.components.Storage
import com.intellij.openapi.components.service
import com.intellij.util.xmlb.XmlSerializerUtil
import java.time.Duration

const val VOX_PLUGIN_ID = "dev.vox.idea"

/** Application-wide Vox configuration, edited in Settings | Tools | Vox. */
@Service(Service.Level.APP)
@State(name = "VoxSettings", storages = [Storage("vox.xml")])
class VoxSettings : PersistentStateComponent<VoxSettings.State> {

    class State {
        var baseUrl: String = "http://127.0.0.1:8765"
        var requestTimeoutSeconds: Int = 180
        var maxRecordingSeconds: Int = 120
        var maxProjectBytes: Int = 8000
        var insertIntoTerminal: Boolean = true

        /** Microphone name; blank records from the system default. */
        var inputDeviceName: String = ""

        /** The language the answer is written in, as an ISO 639-1 code. */
        var language: String = VoxLanguages.DEFAULT_CODE

        var sendClaudeContext: Boolean = true
        var contextExchanges: Int = 3
        var maxContextBytes: Int = 6000
    }

    private val state = State()

    override fun getState(): State = state

    override fun loadState(state: State) {
        XmlSerializerUtil.copyBean(state, this.state)
    }

    /** The backend root without a trailing slash; falls back to the default when blank. */
    fun baseUrl(): String {
        val trimmed = state.baseUrl.trim().trimEnd('/')
        return trimmed.ifEmpty { State().baseUrl }
    }

    fun requestTimeout(): Duration =
        Duration.ofSeconds(state.requestTimeoutSeconds.coerceIn(MIN_TIMEOUT_S, MAX_TIMEOUT_S).toLong())

    fun maxRecordingSeconds(): Int = state.maxRecordingSeconds.coerceIn(MIN_RECORDING_S, MAX_RECORDING_S)

    fun maxProjectBytes(): Int = state.maxProjectBytes.coerceIn(MIN_PROJECT_BYTES, MAX_PROJECT_BYTES)

    fun insertIntoTerminal(): Boolean = state.insertIntoTerminal

    /** The configured microphone name, or "" for the system default. */
    fun inputDeviceName(): String = state.inputDeviceName.trim()

    /** The output language code; falls back to the default when the stored value is unusable. */
    fun language(): String = VoxLanguages.normalise(state.language) ?: VoxLanguages.DEFAULT_CODE

    fun sendClaudeContext(): Boolean = state.sendClaudeContext

    fun contextExchanges(): Int = state.contextExchanges.coerceIn(MIN_EXCHANGES, MAX_EXCHANGES)

    fun maxContextBytes(): Int = state.maxContextBytes.coerceIn(MIN_CONTEXT_BYTES, MAX_CONTEXT_BYTES)

    companion object {
        const val MIN_TIMEOUT_S = 5
        const val MAX_TIMEOUT_S = 3600
        const val MIN_RECORDING_S = 5
        const val MAX_RECORDING_S = 3600
        const val MIN_PROJECT_BYTES = 0
        const val MAX_PROJECT_BYTES = 200_000
        const val MIN_EXCHANGES = 1
        const val MAX_EXCHANGES = 20
        const val MIN_CONTEXT_BYTES = 0
        const val MAX_CONTEXT_BYTES = 200_000

        fun getInstance(): VoxSettings = service()
    }
}
