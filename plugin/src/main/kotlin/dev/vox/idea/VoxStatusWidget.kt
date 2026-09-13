package dev.vox.idea

import com.intellij.openapi.project.DumbAware
import com.intellij.openapi.project.Project
import com.intellij.openapi.wm.StatusBarWidget
import com.intellij.openapi.wm.StatusBarWidgetFactory
import com.intellij.util.Consumer
import java.awt.Component
import java.awt.event.MouseEvent

/** Status bar entry showing what Vox is doing; clicking it is the same as the Dictate action. */
class VoxStatusWidget(private val project: Project) : StatusBarWidget, StatusBarWidget.TextPresentation {

    override fun ID(): String = ID

    override fun getPresentation(): StatusBarWidget.WidgetPresentation = this

    override fun getText(): String =
        when (VoxDictationService.getInstance(project).status) {
            VoxDictationService.Status.IDLE -> "Vox"
            VoxDictationService.Status.RECORDING -> "Vox \u25CF REC"
            VoxDictationService.Status.SENDING -> "Vox \u2026"
        }

    override fun getAlignment(): Float = Component.CENTER_ALIGNMENT

    override fun getTooltipText(): String =
        when (VoxDictationService.getInstance(project).status) {
            VoxDictationService.Status.IDLE -> "Vox: click to start dictating"
            VoxDictationService.Status.RECORDING -> "Vox is recording: click to stop and send"
            VoxDictationService.Status.SENDING -> "Vox is waiting for the backend"
        }

    override fun getClickConsumer(): Consumer<MouseEvent> = Consumer {
        VoxDictationService.getInstance(project).toggle()
    }

    override fun dispose() = Unit

    companion object {
        const val ID = "dev.vox.idea.StatusWidget"
    }
}

class VoxStatusWidgetFactory : StatusBarWidgetFactory, DumbAware {

    override fun getId(): String = VoxStatusWidget.ID

    override fun getDisplayName(): String = "Vox"

    // Overriding the Project-only overload also serves the CoroutineScope overload, which delegates to it.
    override fun createWidget(project: Project): StatusBarWidget = VoxStatusWidget(project)
}
