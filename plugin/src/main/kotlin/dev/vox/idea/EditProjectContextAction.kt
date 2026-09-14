package dev.vox.idea

import com.intellij.openapi.actionSystem.ActionUpdateThread
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.diagnostic.logger
import com.intellij.openapi.fileEditor.FileEditorManager
import com.intellij.openapi.project.DumbAwareAction
import com.intellij.openapi.vfs.LocalFileSystem
import java.io.IOException
import java.nio.file.Files
import java.nio.file.Path

/**
 * Opens this project's `.vox.md`, creating it from a template when it does not exist yet.
 *
 * The file is the one piece of the repository Vox ever sends: its `## SYSTEM` section becomes
 * instructions layered on top of the built-in prompt, and everything else becomes project context.
 * Having it one menu item away is what makes that steerable without leaving the IDE.
 */
class EditProjectContextAction : DumbAwareAction() {

    override fun getActionUpdateThread(): ActionUpdateThread = ActionUpdateThread.BGT

    override fun update(event: AnActionEvent) {
        event.presentation.isEnabled = event.project?.basePath != null
    }

    override fun actionPerformed(event: AnActionEvent) {
        val project = event.project ?: return
        val base = project.basePath ?: return
        val path = Path.of(base, ProjectContext.FILE_NAME)
        if (!Files.exists(path)) {
            try {
                Files.writeString(path, TEMPLATE)
            } catch (e: IOException) {
                log.warn("cannot create $path", e)
                VoxNotifications.error(project, "could not create ${ProjectContext.FILE_NAME}: ${e.message}")
                return
            }
        }
        val file = LocalFileSystem.getInstance().refreshAndFindFileByNioFile(path)
        if (file == null) {
            VoxNotifications.error(project, "could not open ${ProjectContext.FILE_NAME}")
            return
        }
        FileEditorManager.getInstance(project).openFile(file, true)
    }

    private companion object {
        val log = logger<EditProjectContextAction>()

        val TEMPLATE =
            """
            # Vox project context

            Vox sends this file with every recording. Keep it short and factual: the model gets
            chattier the more context it is given, and output length is what costs time.

            ## SYSTEM

            Instructions layered on top of the built-in prompt. Say how the answer should be
            shaped - for example "answer in at most two sentences" or "always name the module the
            change belongs to". They refine the format; they cannot make the model invent content
            that was not spoken.

            ## Vocabulary

            Terms, class and table names of this repository, so they are spelled correctly when
            they are dictated out loud.

            ## Constraints

            Things that are always true here and that the model should not contradict.
            """.trimIndent() + "\n"
    }
}
