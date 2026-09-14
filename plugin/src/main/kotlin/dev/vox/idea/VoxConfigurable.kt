package dev.vox.idea

import com.intellij.openapi.options.BoundConfigurable
import com.intellij.openapi.progress.ProgressManager
import com.intellij.openapi.ui.ComboBox
import com.intellij.openapi.ui.DialogPanel
import com.intellij.openapi.ui.Messages
import com.intellij.ui.SimpleListCellRenderer
import com.intellij.ui.components.JBLabel
import com.intellij.ui.components.JBPasswordField
import com.intellij.ui.components.JBTextField
import com.intellij.ui.dsl.builder.Cell
import com.intellij.ui.dsl.builder.RowLayout
import com.intellij.ui.dsl.builder.TopGap
import com.intellij.ui.dsl.builder.bindIntText
import com.intellij.ui.dsl.builder.bindItem
import com.intellij.ui.dsl.builder.bindSelected
import com.intellij.ui.dsl.builder.bindText
import com.intellij.ui.dsl.builder.columns
import com.intellij.ui.dsl.builder.panel
import com.intellij.ui.dsl.builder.selected
import com.intellij.util.ui.JBUI
import java.time.Duration
import javax.swing.DefaultComboBoxModel
import javax.swing.JCheckBox

/** Settings | Tools | Vox. */
class VoxConfigurable : BoundConfigurable("Vox") {

    private val state = VoxSettings.getInstance().state
    private lateinit var urlField: Cell<JBTextField>

    // The LLM connection belongs to the backend, so these are not bound to VoxSettings and are
    // never stored by the IDE: Load reads them, Apply writes them, and the backend is the only
    // copy. That also keeps the API key out of vox.xml.
    private val llmBaseUrl = JBTextField()
    private val llmModel = JBTextField()
    private val llmApiKey = JBPasswordField()
    private val llmKeyState = JBLabel("")
    private val llmStatus = JBLabel(" ")
    private var llmWarmup: JCheckBox? = null

    override fun createPanel(): DialogPanel = panel {
        group("Backend") {
            row("Backend URL:") {
                urlField = textField().bindText(state::baseUrl).columns(34)
            }
            row("Request timeout (s):") {
                intTextField(VoxSettings.MIN_TIMEOUT_S..VoxSettings.MAX_TIMEOUT_S)
                    .bindIntText(state::requestTimeoutSeconds)
            }
            row {
                button("Test Connection") { testConnection(urlField.component.text) }
            }
        }

        group("Audio") {
            row("Microphone:") {
                cell(deviceCombo())
                    .bindItem({ state.inputDeviceName }, { state.inputDeviceName = it.orEmpty() })
                    .comment(
                        "Devices are matched by name, so a device that is gone falls back " +
                            "to the system default."
                    )
            }
            row("Max recording (s):") {
                intTextField(VoxSettings.MIN_RECORDING_S..VoxSettings.MAX_RECORDING_S)
                    .bindIntText(state::maxRecordingSeconds)
            }
        }

        group("Output") {
            row("Answer language:") {
                cell(languageCombo())
                    .bindItem(
                        { VoxLanguages.normalise(state.language) ?: VoxLanguages.DEFAULT_CODE },
                        { state.language = it ?: VoxLanguages.DEFAULT_CODE },
                    )
                    .comment(
                        "The language the finished message is written in. What you speak is " +
                            "recognised independently, so dictating in another language keeps working."
                    )
            }
            row {
                checkBox("Type the result into the active IDE terminal")
                    .bindSelected(state::insertIntoTerminal)
                    .comment("When no terminal is open the result goes to the clipboard instead.")
            }
        }

        group("Language model (on the backend)") {
            row("Endpoint:") { cell(llmBaseUrl).columns(34) }
            row("Model:") { cell(llmModel).columns(24) }
            row("API key:") {
                cell(llmApiKey).columns(24)
                cell(llmKeyState)
            }.rowComment(
                "Stored by the backend, never by the IDE. Leave blank to keep the current key; " +
                    "type a space to clear it for a local server that wants none."
            )
            row {
                checkBox("Warm the model up at startup").applyToComponent { llmWarmup = this }
                    .comment("Turn off for a hosted endpoint: there is nothing to load and the call is billed.")
            }
            row {
                button("Load") { loadLlmConfig() }
                button("Apply") { applyLlmConfig() }
                cell(llmStatus)
            }.topGap(TopGap.SMALL)
        }

        group("Context") {
            row("Max ${ProjectContext.FILE_NAME} size (bytes):") {
                intTextField(VoxSettings.MIN_PROJECT_BYTES..VoxSettings.MAX_PROJECT_BYTES)
                    .bindIntText(state::maxProjectBytes)
                    .comment(
                        "${ProjectContext.FILE_NAME} in the project root is sent with every request. " +
                            "Its \"## SYSTEM\" section becomes instructions for the model; the rest is " +
                            "project context. Longer files are truncated on a line boundary."
                    )
            }
            lateinit var claudeContext: Cell<javax.swing.JCheckBox>
            row {
                claudeContext =
                    checkBox("Send the recent Claude Code conversation")
                        .bindSelected(state::sendClaudeContext)
                        .comment(
                            "Lets a dictated request say \"this method\" or \"what we just discussed\". " +
                                "Read from the newest Claude Code session of this project; nothing is " +
                                "ever written back."
                        )
            }
            row("Exchanges to send:") {
                intTextField(VoxSettings.MIN_EXCHANGES..VoxSettings.MAX_EXCHANGES)
                    .bindIntText(state::contextExchanges)
                    .enabledIf(claudeContext.selected)
            }.layout(RowLayout.LABEL_ALIGNED)
            row("Max conversation size (bytes):") {
                intTextField(VoxSettings.MIN_CONTEXT_BYTES..VoxSettings.MAX_CONTEXT_BYTES)
                    .bindIntText(state::maxContextBytes)
                    .enabledIf(claudeContext.selected)
                    .comment("Oldest exchanges are dropped first when the budget is exceeded.")
            }.layout(RowLayout.LABEL_ALIGNED)
        }.topGap(TopGap.SMALL)
    }.apply { border = JBUI.Borders.empty(8) }

    private fun deviceCombo(): ComboBox<String> {
        val devices = AudioDevices.list()
        val labels = devices.associate { it.name to it.label }
        val names = mutableListOf(SYSTEM_DEFAULT)
        devices.mapTo(names) { it.name }
        // A microphone that is unplugged right now must still show as the current choice rather
        // than silently resetting the setting to the default.
        val configured = state.inputDeviceName.trim()
        if (configured.isNotEmpty() && configured !in names) names.add(configured)
        return ComboBox(DefaultComboBoxModel(names.toTypedArray())).apply {
            renderer =
                SimpleListCellRenderer.create("") { value ->
                    when {
                        value.isNullOrEmpty() -> AudioRecorder.DEFAULT_DEVICE_LABEL
                        else -> labels[value] ?: "$value (not connected)"
                    }
                }
        }
    }

    private fun languageCombo(): ComboBox<String> {
        val current = VoxLanguages.normalise(state.language) ?: VoxLanguages.DEFAULT_CODE
        val codes = VoxLanguages.optionsIncluding(current)
        return ComboBox(DefaultComboBoxModel(codes.toTypedArray())).apply {
            renderer = SimpleListCellRenderer.create("") { value -> VoxLanguages.label(value.orEmpty()) }
        }
    }

    /** Reads the backend's LLM connection into the fields. */
    private fun loadLlmConfig() {
        withBackend("Vox: Reading the Backend Configuration") { it.readLlmConfig() }
            ?.let(::showLlmConfig)
    }

    /**
     * Sends only what was actually changed.
     *
     * A blank key field means "leave the key alone", which is why it is never sent as an empty
     * string by accident: the backend reads "" as "clear the key", and silently unsetting the
     * credential of a hosted endpoint every time somebody opens this page would be its own bug.
     */
    private fun applyLlmConfig() {
        val typedKey = String(llmApiKey.password)
        val update =
            LlmConfigUpdate(
                baseUrl = llmBaseUrl.text.trim().ifEmpty { null },
                model = llmModel.text.trim().ifEmpty { null },
                apiKey = if (typedKey.isEmpty()) null else typedKey.trim(),
                warmup = llmWarmup?.isSelected,
            )
        val applied = withBackend("Vox: Applying the Backend Configuration") {
            it.writeLlmConfig(update)
        } ?: return
        llmApiKey.text = ""
        showLlmConfig(applied)
        if (!applied.ready) {
            Messages.showWarningDialog(
                "The backend accepted the settings but cannot reach the model:\n" +
                    "${applied.error.ifEmpty { "not ready" }}\n\nDictation will fail until this is resolved.",
                TITLE,
            )
        }
    }

    private fun showLlmConfig(config: LlmConfig) {
        llmBaseUrl.text = config.baseUrl
        llmModel.text = config.model
        llmWarmup?.isSelected = config.warmup
        llmKeyState.text = if (config.apiKeySet) "a key is set" else "no key"
        val source = if (config.overridden) "set from here" else "from the backend's .env"
        llmStatus.text =
            if (config.ready) "reachable · $source" else "NOT reachable: ${config.error.ifEmpty { "not ready" }}"
    }

    /** Runs [call] against the configured backend with a progress dialog; null on failure. */
    private fun <T> withBackend(title: String, call: (VoxClient) -> T): T? {
        val url = urlField.component.text.trim().trimEnd('/')
        if (url.isEmpty()) {
            Messages.showErrorDialog("Enter the backend URL first.", TITLE)
            return null
        }
        return try {
            ProgressManager.getInstance()
                .runProcessWithProgressSynchronously<T, Exception>(
                    { call(VoxClient(url, HEALTH_TIMEOUT)) },
                    title,
                    true,
                    null,
                )
        } catch (e: Exception) {
            llmStatus.text = e.message ?: "the backend is unreachable"
            Messages.showErrorDialog(e.message ?: "the backend is unreachable", TITLE)
            null
        }
    }

    private fun testConnection(rawUrl: String) {
        val url = rawUrl.trim().trimEnd('/')
        if (url.isEmpty()) {
            Messages.showErrorDialog("Enter the backend URL first.", TITLE)
            return
        }
        val health =
            try {
                ProgressManager.getInstance()
                    .runProcessWithProgressSynchronously<HealthResult, Exception>(
                        { VoxClient(url, HEALTH_TIMEOUT).health() },
                        "Vox: Checking the Backend",
                        true,
                        null,
                    )
            } catch (e: Exception) {
                Messages.showErrorDialog(e.message ?: "the backend is unreachable", TITLE)
                return
            }
        report(health)
    }

    /**
     * Reports a health snapshot as a success or a warning, never as a bare model list.
     *
     * A backend whose LLM is unreachable still answers /health with the model it is configured
     * for, so printing that alone reads as "everything is fine" and the failure only surfaces
     * as a timeout halfway through a dictation. That matters most with a remote endpoint,
     * where being unreachable is a normal Tuesday rather than a broken install.
     */
    private fun report(health: HealthResult) {
        val speech =
            line(health.sttReady, "Speech", "${health.sttModel} on ${health.sttDevice}", health.sttError)
        val model =
            line(health.llmReady, "Model", "${health.llmModel} at ${health.llmBaseUrl}", health.llmError)
        val body = "Status: ${health.status}\n$speech\n$model"
        if (health.usable) {
            Messages.showInfoMessage(body, TITLE)
        } else {
            Messages.showWarningDialog("$body\n\nDictation will fail until this is resolved.", TITLE)
        }
    }

    private fun line(ready: Boolean, label: String, detail: String, error: String): String {
        val mark = if (ready) "OK" else "FAILED"
        val reason = error.ifEmpty { if (ready) "" else "not ready" }
        return "$label: $mark - $detail" + if (reason.isEmpty()) "" else " ($reason)"
    }

    private companion object {
        const val TITLE = "Vox"
        const val SYSTEM_DEFAULT = ""
        val HEALTH_TIMEOUT: Duration = Duration.ofSeconds(15)
    }
}
