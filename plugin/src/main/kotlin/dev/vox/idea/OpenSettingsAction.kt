package dev.vox.idea

import com.intellij.openapi.actionSystem.ActionUpdateThread
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.options.ShowSettingsUtil
import com.intellij.openapi.project.DumbAwareAction

/** Jumps straight to Settings | Tools | Vox instead of making the user find it in the tree. */
class OpenSettingsAction : DumbAwareAction() {

    override fun getActionUpdateThread(): ActionUpdateThread = ActionUpdateThread.BGT

    override fun actionPerformed(event: AnActionEvent) {
        ShowSettingsUtil.getInstance().showSettingsDialog(event.project, VoxConfigurable::class.java)
    }
}
