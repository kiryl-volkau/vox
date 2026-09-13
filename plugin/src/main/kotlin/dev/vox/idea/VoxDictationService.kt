package dev.vox.idea

import com.intellij.ide.plugins.PluginManagerCore
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.components.Service
import com.intellij.openapi.components.service
import com.intellij.openapi.diagnostic.logger
import com.intellij.openapi.extensions.PluginId
import com.intellij.openapi.ide.CopyPasteManager
import com.intellij.openapi.progress.ProgressIndicator
import com.intellij.openapi.progress.ProgressManager
import com.intellij.openapi.progress.Task
import com.intellij.openapi.project.Project
import com.intellij.openapi.wm.WindowManager
import java.awt.datatransfer.StringSelection

/**
 * Owns the dictation state machine for one project: idle -> recording -> sending -> idle.
 *
 * Every method here runs on the EDT; only the HTTP round trip and the microphone loop leave it.
 * A failure always returns the state to idle, so a broken backend can never leave the trigger stuck.
 */
@Service(Service.Level.PROJECT)
class VoxDictationService(private val project: Project) {

    enum class Status {
        IDLE,
        RECORDING,
        SENDING,
    }

    @Volatile
    var status: Status = Status.IDLE
        private set

    private var recorder: AudioRecorder? = null

    /** Starts recording, or stops the running recording and sends it to the backend. */
    fun toggle() {
        when (status) {
            Status.IDLE -> start()
            Status.RECORDING -> stopAndSend()
            Status.SENDING -> VoxNotifications.info(project, "still working on the previous recording")
        }
    }

    /** Throws a running recording away. A request already sent to the backend runs to its end. */
    fun cancel() {
        if (status != Status.RECORDING) return
        recorder?.cancel()
        recorder = null
        setStatus(Status.IDLE)
        VoxNotifications.info(project, "dictation cancelled")
    }

    private fun start() {
        val settings = VoxSettings.getInstance()
        val started = AudioRecorder(settings.maxRecordingSeconds(), ::onLimitReached)
        try {
            started.start()
        } catch (e: AudioCaptureException) {
            VoxNotifications.error(project, e.message ?: "the microphone could not be opened")
            return
        }
        recorder = started
        setStatus(Status.RECORDING)
    }

    private fun onLimitReached() {
        ApplicationManager.getApplication().invokeLater(
            {
                if (status != Status.RECORDING) return@invokeLater
                VoxNotifications.warn(project, "recording limit reached, sending what was captured")
                stopAndSend()
            },
            project.disposed,
        )
    }

    private fun stopAndSend() {
        val active = recorder ?: return
        recorder = null
        val audio =
            try {
                active.stop()
            } catch (e: RuntimeException) {
                log.warn("could not finish the recording", e)
                null
            }
        if (audio == null) {
            setStatus(Status.IDLE)
            VoxNotifications.warn(project, "nothing was recorded")
            return
        }
        setStatus(Status.SENDING)
        send(audio, VoxSettings.getInstance())
    }

    private fun send(audio: RecordedAudio, settings: VoxSettings) {
        val client = VoxClient(settings.baseUrl(), settings.requestTimeout())
        val mode = settings.mode()
        val maxProjectBytes = settings.maxProjectBytes()
        val clientVersion = pluginVersion()
        ProgressManager.getInstance()
            .run(
                object : Task.Backgroundable(project, "Vox: Transcribing", false) {
                    private var result: ProcessResult? = null
                    private var failure: String? = null

                    override fun run(indicator: ProgressIndicator) {
                        indicator.isIndeterminate = true
                        val context = ProjectContext.read(project, maxProjectBytes)
                        val request =
                            ProcessRequest(
                                wav = audio.wav,
                                audioSeconds = audio.seconds,
                                mode = mode,
                                project = context?.text,
                                projectName = context?.name,
                                clientVersion = clientVersion,
                            )
                        result =
                            try {
                                client.process(request)
                            } catch (e: VoxException) {
                                failure = e.message
                                null
                            }
                    }

                    override fun onThrowable(error: Throwable) {
                        log.warn("the vox request failed", error)
                        failure = error.message ?: "the request failed"
                    }

                    override fun onFinished() {
                        setStatus(Status.IDLE)
                        val finished = result
                        if (finished != null) log.info("request ${finished.requestId} took ${finished.totalMs} ms")
                        when {
                            finished != null && finished.output.isNotBlank() -> deliver(finished.output)
                            finished != null -> VoxNotifications.warn(project, "the backend returned no text")
                            else -> VoxNotifications.error(project, failure ?: "the request failed")
                        }
                    }
                }
            )
    }

    private fun deliver(text: String) {
        val wantsTerminal = VoxSettings.getInstance().insertIntoTerminal()
        if (wantsTerminal && TerminalInserter.insert(project, text)) return
        CopyPasteManager.getInstance().setContents(StringSelection(text))
        val reason = if (wantsTerminal) "no terminal tab to type into - the result is " else "the result is "
        VoxNotifications.info(project, reason + "on the clipboard")
    }

    private fun setStatus(next: Status) {
        status = next
        WindowManager.getInstance().getStatusBar(project)?.updateWidget(VoxStatusWidget.ID)
    }

    private fun pluginVersion(): String =
        PluginManagerCore.getPlugin(PluginId.getId(VOX_PLUGIN_ID))?.version ?: "0.0.0"

    companion object {
        private val log = logger<VoxDictationService>()

        fun getInstance(project: Project): VoxDictationService = project.service()
    }
}
