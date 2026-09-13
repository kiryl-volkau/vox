package dev.vox.idea

import com.intellij.openapi.components.PersistentStateComponent
import com.intellij.openapi.components.Service
import com.intellij.openapi.components.State
import com.intellij.openapi.components.Storage
import com.intellij.openapi.components.service
import com.intellij.util.xmlb.XmlSerializerUtil
import java.time.Duration

const val VOX_PLUGIN_ID = "dev.vox.idea"

val VOX_MODES = listOf("dictation", "clean", "task", "context")

/** Application-wide Vox configuration, edited in Settings | Tools | Vox. */
@Service(Service.Level.APP)
@State(name = "VoxSettings", storages = [Storage("vox.xml")])
class VoxSettings : PersistentStateComponent<VoxSettings.State> {

    class State {
        var baseUrl: String = "http://127.0.0.1:8765"
        var mode: String = "context"
        var requestTimeoutSeconds: Int = 180
        var maxRecordingSeconds: Int = 120
        var maxProjectBytes: Int = 8000
        var insertIntoTerminal: Boolean = true
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

    fun mode(): String = state.mode.trim().ifEmpty { "context" }

    fun requestTimeout(): Duration =
        Duration.ofSeconds(state.requestTimeoutSeconds.coerceIn(MIN_TIMEOUT_S, MAX_TIMEOUT_S).toLong())

    fun maxRecordingSeconds(): Int = state.maxRecordingSeconds.coerceIn(MIN_RECORDING_S, MAX_RECORDING_S)

    fun maxProjectBytes(): Int = state.maxProjectBytes.coerceIn(MIN_PROJECT_BYTES, MAX_PROJECT_BYTES)

    fun insertIntoTerminal(): Boolean = state.insertIntoTerminal

    companion object {
        const val MIN_TIMEOUT_S = 5
        const val MAX_TIMEOUT_S = 3600
        const val MIN_RECORDING_S = 5
        const val MAX_RECORDING_S = 3600
        const val MIN_PROJECT_BYTES = 0
        const val MAX_PROJECT_BYTES = 200_000

        fun getInstance(): VoxSettings = service()
    }
}
