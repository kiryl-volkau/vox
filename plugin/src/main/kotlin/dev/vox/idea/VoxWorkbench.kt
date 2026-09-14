package dev.vox.idea

import com.intellij.openapi.actionSystem.ActionUpdateThread
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.project.DumbAware
import com.intellij.openapi.project.DumbAwareAction
import com.intellij.openapi.project.Project
import com.intellij.openapi.wm.ToolWindow
import com.intellij.openapi.wm.ToolWindowFactory
import com.intellij.ui.content.ContentFactory

/** Registers the Vox tool window, which is both the transcript history and the prompt bench. */
class VoxWorkbenchToolWindowFactory : ToolWindowFactory, DumbAware {

    override fun createToolWindowContent(project: Project, toolWindow: ToolWindow) {
        val panel = VoxWorkbenchPanel(project)
        val content = ContentFactory.getInstance().createContent(panel, null, false)
        content.setDisposer(panel)
        toolWindow.contentManager.addContent(content)
    }
}

/** Opens the Vox tool window, which is otherwise only reachable from the tool window bar. */
class OpenWorkbenchAction : DumbAwareAction() {

    override fun getActionUpdateThread(): ActionUpdateThread = ActionUpdateThread.BGT

    override fun update(event: AnActionEvent) {
        event.presentation.isEnabled = event.project != null
    }

    override fun actionPerformed(event: AnActionEvent) {
        val project = event.project ?: return
        VoxTranscriptStore.getInstance(project).show()
    }
}
