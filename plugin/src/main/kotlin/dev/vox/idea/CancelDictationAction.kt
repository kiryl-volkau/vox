package dev.vox.idea

import com.intellij.openapi.actionSystem.ActionUpdateThread
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.project.DumbAwareAction

/** Discards a running dictation instead of sending it. */
class CancelDictationAction : DumbAwareAction() {

    override fun getActionUpdateThread(): ActionUpdateThread = ActionUpdateThread.BGT

    override fun update(event: AnActionEvent) {
        val project = event.project
        event.presentation.isEnabled =
            project != null && VoxDictationService.getInstance(project).status == VoxDictationService.Status.RECORDING
    }

    override fun actionPerformed(event: AnActionEvent) {
        val project = event.project ?: return
        VoxDictationService.getInstance(project).cancel()
    }
}
