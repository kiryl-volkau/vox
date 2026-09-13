package dev.vox.idea

import com.intellij.ide.DataManager
import com.intellij.openapi.actionSystem.DataKey
import com.intellij.openapi.actionSystem.UiDataProvider
import com.intellij.openapi.diagnostic.logger
import com.intellij.openapi.progress.ProcessCanceledException
import com.intellij.openapi.project.Project
import com.intellij.openapi.wm.ToolWindowManager
import com.intellij.terminal.JBTerminalWidget
import com.intellij.ui.content.Content
import java.awt.Component
import java.awt.Container
import java.lang.reflect.Method

/**
 * Types text into the terminal tab the user is currently looking at, as if it had been typed.
 *
 * Nothing here ever sends a newline or asks the terminal to execute: the user reviews the text and
 * presses Enter. Every entry point returns false instead of throwing, so the caller can fall back
 * to the clipboard when the terminal is closed, absent, or exposes a different API than expected.
 *
 * Two terminal engines have to be handled. The reworked engine (the 2026.2 default) keeps its
 * `TerminalView` in a plugin content module that this plugin cannot link against without making
 * the terminal plugin a hard requirement, so that path goes through the "TerminalView" data key and
 * reflection. The classic engine exposes `JBTerminalWidget`, which lives in the platform itself.
 */
internal object TerminalInserter {

    private const val TOOL_WINDOW_ID = "Terminal"
    private val TERMINAL_VIEW_KEY: DataKey<Any> = DataKey.create("TerminalView")
    private val log = logger<TerminalInserter>()

    /** Call on the EDT. Returns true only when the text really reached a terminal. */
    fun insert(project: Project, text: String): Boolean =
        try {
            insertIntoSelectedTab(project, text)
        } catch (e: ProcessCanceledException) {
            throw e
        } catch (t: Throwable) {
            log.warn("terminal insertion failed, falling back to the clipboard", t)
            false
        }

    private fun insertIntoSelectedTab(project: Project, text: String): Boolean {
        val toolWindow = ToolWindowManager.getInstance(project).getToolWindow(TOOL_WINDOW_ID) ?: return false
        val content = toolWindow.contentManager.selectedContent ?: return false
        val sent = sendToReworkedTerminal(content, text) || sendToClassicTerminal(content, text)
        if (sent && !toolWindow.isVisible) toolWindow.show(null)
        return sent
    }

    private fun sendToReworkedTerminal(content: Content, text: String): Boolean {
        val view = findTerminalView(content) ?: return false
        val builder = call(view, "createSendTextBuilder") ?: return callSend(view, "sendText", text)
        // Multi-line text must arrive as a paste, or the shell runs everything up to the last line.
        val mode = if (text.contains('\n')) "requireBracketedPasteMode" else "useBracketedPasteMode"
        val configured = call(builder, mode) ?: builder
        return callSend(configured, "trySend", text)
    }

    private fun sendToClassicTerminal(content: Content, text: String): Boolean {
        if (text.contains('\n')) return false
        val widget = componentsOf(content.component).filterIsInstance<JBTerminalWidget>().firstOrNull() ?: return false
        val connector = widget.ttyConnector ?: return false
        if (!connector.isConnected) return false
        connector.write(text)
        return true
    }

    private fun findTerminalView(content: Content): Any? {
        val dataManager = DataManager.getInstance()
        return componentsOf(content.component)
            .filterIsInstance<UiDataProvider>()
            .mapNotNull { TERMINAL_VIEW_KEY.getData(dataManager.getDataContext(it as Component)) }
            .firstOrNull()
    }

    private fun componentsOf(root: Component): Sequence<Component> =
        sequence {
            yield(root)
            if (root is Container) root.components.forEach { yieldAll(componentsOf(it)) }
        }

    private fun call(target: Any, name: String): Any? = publicMethod(target, name)?.invoke(target)

    private fun callSend(target: Any, name: String, text: String): Boolean {
        val method = publicMethod(target, name, String::class.java) ?: return false
        val result = method.invoke(target, text)
        return result !is Boolean || result
    }

    /**
     * The method as declared by a public type, so reflection is not blocked by an implementation
     * class the terminal plugin keeps internal.
     */
    private fun publicMethod(target: Any, name: String, vararg parameters: Class<*>): Method? {
        for (type in publicTypesOf(target.javaClass)) {
            val method = runCatching { type.getMethod(name, *parameters) }.getOrNull()
            if (method != null) return method
        }
        return null
    }

    private fun publicTypesOf(type: Class<*>): Sequence<Class<*>> =
        generateSequence(type) { it.superclass }
            .flatMap { sequenceOf(it) + it.interfaces.asSequence() }
            .filter { java.lang.reflect.Modifier.isPublic(it.modifiers) }
}
