package dev.vox.idea

import com.intellij.openapi.options.BoundConfigurable
import com.intellij.openapi.progress.ProgressManager
import com.intellij.openapi.ui.DialogPanel
import com.intellij.openapi.ui.Messages
import com.intellij.ui.components.JBTextField
import com.intellij.ui.dsl.builder.Cell
import com.intellij.ui.dsl.builder.bindIntText
import com.intellij.ui.dsl.builder.bindItem
import com.intellij.ui.dsl.builder.bindSelected
import com.intellij.ui.dsl.builder.bindText
import com.intellij.ui.dsl.builder.columns
import com.intellij.ui.dsl.builder.panel
import com.intellij.ui.dsl.builder.toNullableProperty
import com.intellij.util.ui.JBUI
import java.time.Duration

/** Settings | Tools | Vox. */
class VoxConfigurable : BoundConfigurable("Vox") {

    private val state = VoxSettings.getInstance().state
    private lateinit var urlField: Cell<JBTextField>

    override fun createPanel(): DialogPanel = panel {
        row("Backend URL:") {
            urlField = textField().bindText(state::baseUrl).columns(34)
        }
        row("Mode:") {
            comboBox(VOX_MODES).bindItem(state::mode.toNullableProperty())
        }
        row("Request timeout (s):") {
            intTextField(VoxSettings.MIN_TIMEOUT_S..VoxSettings.MAX_TIMEOUT_S)
                .bindIntText(state::requestTimeoutSeconds)
        }
        row("Max recording (s):") {
            intTextField(VoxSettings.MIN_RECORDING_S..VoxSettings.MAX_RECORDING_S)
                .bindIntText(state::maxRecordingSeconds)
        }
        row("Max ${ProjectContext.FILE_NAME} size (bytes):") {
            intTextField(VoxSettings.MIN_PROJECT_BYTES..VoxSettings.MAX_PROJECT_BYTES)
                .bindIntText(state::maxProjectBytes)
                .comment("Longer project files are truncated on a line boundary before they are sent.")
        }
        row {
            checkBox("Type the result into the active IDE terminal")
                .bindSelected(state::insertIntoTerminal)
                .comment("When no terminal is open the result goes to the clipboard instead.")
        }
        row {
            button("Test Connection") { testConnection(urlField.component.text) }
        }.topGap(com.intellij.ui.dsl.builder.TopGap.SMALL)
    }.apply { border = JBUI.Borders.empty(8) }

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
        Messages.showInfoMessage(
            "Status: ${health.status}\nSpeech: ${health.sttModel} on ${health.sttDevice}\nModel: ${health.llmModel}",
            TITLE,
        )
    }

    private companion object {
        const val TITLE = "Vox"
        val HEALTH_TIMEOUT: Duration = Duration.ofSeconds(15)
    }
}
