package dev.vox.idea

import com.intellij.notification.NotificationGroupManager
import com.intellij.notification.NotificationType
import com.intellij.openapi.project.Project

/** Balloons for the states the user cannot see in the status bar: failures and fallbacks. */
internal object VoxNotifications {
    private const val GROUP_ID = "Vox"

    fun info(project: Project?, content: String) = notify(project, content, NotificationType.INFORMATION)

    fun warn(project: Project?, content: String) = notify(project, content, NotificationType.WARNING)

    fun error(project: Project?, content: String) = notify(project, content, NotificationType.ERROR)

    private fun notify(project: Project?, content: String, type: NotificationType) {
        NotificationGroupManager.getInstance()
            .getNotificationGroup(GROUP_ID)
            .createNotification("Vox", content, type)
            .notify(project)
    }
}
