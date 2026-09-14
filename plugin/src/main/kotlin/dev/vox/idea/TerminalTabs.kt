package dev.vox.idea

import com.intellij.ide.DataManager
import com.intellij.openapi.actionSystem.DataKey
import com.intellij.openapi.actionSystem.UiDataProvider
import com.intellij.openapi.diagnostic.logger
import com.intellij.openapi.project.Project
import com.intellij.openapi.wm.ToolWindowManager
import com.intellij.terminal.JBTerminalWidget
import com.intellij.ui.content.Content
import java.awt.Component
import java.awt.Container
import java.lang.reflect.Method

/**
 * Reaching the terminal tab the user is looking at, and the process running inside it.
 *
 * Two engines have to be handled and neither can be linked against directly. The reworked engine -
 * the 2026.2 default - keeps `TerminalView` in a plugin content module, so making the terminal a
 * hard dependency would be the price of importing it; instead it is reached through the
 * "TerminalView" data key and reflection. The classic engine exposes `JBTerminalWidget`, which is
 * in the platform itself.
 *
 * Everything here answers null rather than throwing. A tab's process is a convenience: losing it
 * costs context on one request, and must never cost the request.
 */
internal object TerminalTabs {

    const val TOOL_WINDOW_ID = "Terminal"

    val TERMINAL_VIEW_KEY: DataKey<Any> = DataKey.create("TerminalView")

    /** The selected tab of the Terminal tool window, or null when there is none. */
    fun selectedTab(project: Project): Content? =
        ToolWindowManager.getInstance(project)
            .getToolWindow(TOOL_WINDOW_ID)
            ?.contentManager
            ?.selectedContent

    /** The shell pid of the selected tab. Call on the EDT: it walks the tab's components. */
    fun selectedShellPid(project: Project): Long? = selectedTab(project)?.let { shellPid(it) }

    /**
     * The pid of the shell running in [content], or null when it cannot be determined.
     *
     * The reworked engine publishes it on its startup options, which arrive as a coroutine
     * `Deferred`. Only an already-completed one is read: a tab whose shell has not finished
     * starting has nothing useful to say yet, and blocking a dictation on it would trade a whole
     * request for a detail that only improves the context.
     */
    fun shellPid(content: Content): Long? =
        reworkedShellPid(content) ?: classicShellPid(content)

    /** The [TerminalView] behind [content], for the callers that need the tab object itself. */
    fun terminalView(content: Content): Any? {
        val dataManager = DataManager.getInstance()
        return componentsOf(content.component)
            .filterIsInstance<UiDataProvider>()
            .mapNotNull { TERMINAL_VIEW_KEY.getData(dataManager.getDataContext(it as Component)) }
            .firstOrNull()
    }

    private fun reworkedShellPid(content: Content): Long? =
        try {
            val view = terminalView(content) ?: return null
            val deferred = call(view, "getStartupOptionsDeferred") ?: return null
            val completed = call(deferred, "isCompleted") as? Boolean ?: return null
            if (!completed) return null
            val options = call(deferred, "getCompleted") ?: return null
            call(options, "getPid") as? Long
        } catch (t: Throwable) {
            log.debug("cannot read the reworked terminal's startup options", t)
            null
        }

    private fun classicShellPid(content: Content): Long? =
        try {
            val widget = componentsOf(content.component)
                .filterIsInstance<JBTerminalWidget>()
                .firstOrNull()
            val connector = widget?.ttyConnector
            val process = connector?.let { unwrapProcess(it) }
            process?.pid()
        } catch (t: Throwable) {
            log.debug("cannot read the classic terminal's process", t)
            null
        }

    /**
     * The OS process behind a connector, unwrapping the proxy the terminal wraps it in.
     *
     * `ShellTerminalWidget.getProcessTtyConnector` is the supported way through that proxy; going
     * straight for `getProcess` on the outer object finds nothing.
     */
    private fun unwrapProcess(connector: Any): Process? {
        val widgetClass = runCatching {
            Class.forName("org.jetbrains.plugins.terminal.ShellTerminalWidget", false, connector.javaClass.classLoader)
        }.getOrNull()
        val unwrap = widgetClass?.let { type ->
            runCatching {
                type.getMethod("getProcessTtyConnector", Class.forName("com.jediterm.terminal.TtyConnector", false, type.classLoader))
            }.getOrNull()
        }
        val target = unwrap?.invoke(null, connector) ?: connector
        return call(target, "getProcess") as? Process
    }

    fun componentsOf(root: Component): Sequence<Component> =
        sequence {
            yield(root)
            if (root is Container) root.components.forEach { yieldAll(componentsOf(it)) }
        }

    fun call(target: Any, name: String): Any? = publicMethod(target, name)?.invoke(target)

    /**
     * The method as declared by a public type, so reflection is not blocked by an implementation
     * class the terminal plugin keeps internal.
     */
    fun publicMethod(target: Any, name: String, vararg parameters: Class<*>): Method? {
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

    private val log = logger<TerminalTabs>()
}
