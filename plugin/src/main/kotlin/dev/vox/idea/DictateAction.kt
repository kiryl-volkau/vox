package dev.vox.idea

import com.intellij.openapi.actionSystem.ActionUpdateThread
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.project.DumbAwareAction

/** Trigger: the first invocation starts recording, the second stops it and sends the audio. */
class DictateAction : DumbAwareAction() {

    override fun getActionUpdateThread(): ActionUpdateThread = ActionUpdateThread.BGT

    override fun update(event: AnActionEvent) {
        val project = event.project
        event.presentation.isEnabled = project != null
        val recording =
            project != null && VoxDictationService.getInstance(project).status == VoxDictationService.Status.RECORDING
        event.presentation.text = if (recording) "Vox: Stop Dictation" else "Vox: Dictate"
        event.presentation.icon = if (recording) VoxIcons.Recording else VoxIcons.Vox
    }

    override fun actionPerformed(event: AnActionEvent) {
        val project = event.project ?: return
        VoxDictationService.getInstance(project).toggle()
    }
}
