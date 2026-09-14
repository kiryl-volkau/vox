package dev.vox.idea

import com.intellij.openapi.options.BoundConfigurable
import com.intellij.openapi.progress.ProgressManager
import com.intellij.openapi.ui.ComboBox
import com.intellij.openapi.ui.DialogPanel
import com.intellij.openapi.ui.Messages
import com.intellij.ui.SimpleListCellRenderer
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

/** Settings | Tools | Vox. */
class VoxConfigurable : BoundConfigurable("Vox") {

    private val state = VoxSettings.getInstance().state
    private lateinit var urlField: Cell<JBTextField>

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
                        "Devices are matched by name, so the same name works here and in " +
                            "config/client.yaml. A device that is gone falls back to the system default."
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
