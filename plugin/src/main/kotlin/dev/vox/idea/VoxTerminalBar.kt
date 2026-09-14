package dev.vox.idea

import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.diagnostic.logger
import com.intellij.openapi.project.Project
import com.intellij.openapi.wm.ToolWindow
import com.intellij.openapi.wm.ex.ToolWindowManagerListener
import com.intellij.ui.components.JBLabel
import com.intellij.ui.content.Content
import com.intellij.ui.content.ContentManagerEvent
import com.intellij.ui.content.ContentManagerListener
import com.intellij.util.ui.JBUI
import java.awt.FlowLayout
import javax.swing.JButton
import javax.swing.JComponent
import javax.swing.JPanel

/**
 * A dictate button inside each terminal tab, next to the name of the session it will talk to.
 *
 * The button exists for what it removes rather than what it adds: a tab cannot hold two Claude
 * Code sessions, so a control that lives *in* a tab has no ambiguity to resolve about which
 * session a dictation belongs to - it passes its own shell's pid. The keyboard trigger has to
 * infer the same thing from whichever tab is selected, which is right nearly always and silently
 * wrong the rest of the time.
 *
 * The label is the other half, and the more useful one: it names the session Vox resolved, so a
 * wrong answer is visible before the request is sent rather than after it comes back strange.
 *
 * Installed by a project listener on the Terminal tool window. `TerminalView` lives in a plugin
 * content module, so `setTopComponent` is reached by reflection for the same reason
 * [TerminalInserter] reaches the rest of the terminal that way: linking against it would make the
 * terminal plugin a hard requirement of this one.
 */
internal class VoxTerminalBar(private val project: Project) : ToolWindowManagerListener {

    override fun toolWindowShown(toolWindow: ToolWindow) {
        if (toolWindow.id != TerminalTabs.TOOL_WINDOW_ID) return
        val manager = toolWindow.contentManager
        if (!installedIn.add(toolWindow)) return
        manager.addContentManagerListener(
            object : ContentManagerListener {
                override fun contentAdded(event: ContentManagerEvent) = install(event.content)
            }
        )
        manager.contents.forEach(::install)
    }

    private fun install(content: Content) {
        val view = TerminalTabs.terminalView(content) ?: return
        val setTopComponent =
            TerminalTabs.publicMethod(view, "setTopComponent", JComponent::class.java, com.intellij.openapi.Disposable::class.java)
                ?: return
        val bar = Bar(content)
        try {
            setTopComponent.invoke(view, bar, content)
        } catch (t: Throwable) {
            log.debug("cannot add the Vox bar to a terminal tab", t)
            return
        }
        bar.refresh()
    }

    /** The strip itself: a trigger bound to this tab, and what it resolved. */
    private inner class Bar(private val content: Content) :
        JPanel(FlowLayout(FlowLayout.LEFT, JBUI.scale(6), JBUI.scale(2))) {

        private val session = JBLabel(RESOLVING).apply { toolTipText = TOOLTIP }

        init {
            add(
                JButton("Dictate").apply {
                    icon = VoxIcons.Vox
                    toolTipText = "Record, and attach this tab's Claude Code conversation"
                    addActionListener {
                        VoxDictationService.getInstance(project).toggle(TerminalTabs.shellPid(content))
                        refresh()
                    }
                }
            )
            add(session)
        }

        /**
         * Resolves the tab's session and shows it.
         *
         * The pid is read here, on the EDT, because it walks this tab's components; everything
         * after it - process enumeration and reading the registry - is file and OS work that has
         * no business blocking the UI.
         */
        fun refresh() {
            val pid = TerminalTabs.shellPid(content)
            if (pid == null) {
                session.text = NO_SHELL
                return
            }
            ApplicationManager.getApplication().executeOnPooledThread {
                val resolved = ClaudeSessions.forShell(pid)
                val text = when {
                    resolved == null -> NO_SESSION
                    else -> resolved.name ?: resolved.id.take(SHORT_ID)
                }
                ApplicationManager.getApplication().invokeLater({ session.text = text }) { !content.isValid }
            }
        }
    }

    private companion object {
        const val RESOLVING = "…"
        const val NO_SHELL = "no shell"
        const val NO_SESSION = "no Claude Code session"
        const val SHORT_ID = 8
        const val TOOLTIP =
            "The Claude Code session Vox will attach as context. Resolved from this tab's process, " +
                "so several sessions in one project stay apart."
        val installedIn = java.util.Collections.newSetFromMap(java.util.WeakHashMap<ToolWindow, Boolean>())
        val log = logger<VoxTerminalBar>()
    }
}
