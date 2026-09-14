package dev.vox.idea

import com.intellij.icons.AllIcons
import com.intellij.openapi.Disposable
import com.intellij.openapi.actionSystem.ActionManager
import com.intellij.openapi.actionSystem.ActionPlaces
import com.intellij.openapi.actionSystem.ActionUpdateThread
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.actionSystem.DefaultActionGroup
import com.intellij.openapi.ide.CopyPasteManager
import com.intellij.openapi.progress.ProgressIndicator
import com.intellij.openapi.progress.ProgressManager
import com.intellij.openapi.progress.Task
import com.intellij.openapi.project.DumbAwareAction
import com.intellij.openapi.project.Project
import com.intellij.openapi.ui.ComboBox
import com.intellij.ui.JBSplitter
import com.intellij.ui.SimpleListCellRenderer
import com.intellij.ui.components.JBCheckBox
import com.intellij.ui.components.JBLabel
import com.intellij.ui.components.JBList
import com.intellij.ui.components.JBScrollPane
import com.intellij.ui.components.JBTextArea
import com.intellij.util.ui.JBUI
import java.awt.BorderLayout
import java.awt.FlowLayout
import java.awt.Font
import java.awt.datatransfer.StringSelection
import javax.swing.BorderFactory
import javax.swing.DefaultComboBoxModel
import javax.swing.DefaultListModel
import javax.swing.JButton
import javax.swing.JPanel
import javax.swing.ListSelectionModel

/**
 * The Vox tool window: the transcripts this project has produced, and a bench for replaying one.
 *
 * The point of the bench is that a prompt can be tried without dictating again. Picking a stored
 * transcript and pressing Run sends it to `POST /v1/transform`, which runs the same prompts, the
 * same glossary and the same model a real dictation would - the prompts stay on the backend and
 * the plugin only chooses which of them to apply.
 */
internal class VoxWorkbenchPanel(private val project: Project) :
    JPanel(BorderLayout()), Disposable {

    private val store = VoxTranscriptStore.getInstance(project)

    private val listModel = DefaultListModel<VoxTranscriptStore.Entry>()
    private val entryList =
        JBList(listModel).apply {
            selectionMode = ListSelectionModel.SINGLE_SELECTION
            cellRenderer =
                SimpleListCellRenderer.create<VoxTranscriptStore.Entry>("") { entry ->
                    "${entry.time()}  ${entry.summary()}"
                }
            addListSelectionListener { if (!it.valueIsAdjusting) showSelected() }
        }

    private val transcript = editor(editable = true)
    private val output = editor(editable = false)
    private val status = JBLabel(" ")

    private val promptKind =
        ComboBox(DefaultComboBoxModel(arrayOf(TASK, DICTATION))).apply { toolTipText = PROMPT_HINT }
    private val language = languageCombo()
    private val withProject = JBCheckBox("${ProjectContext.FILE_NAME}", true)
    private val runButton = JButton("Run").apply { addActionListener { run() } }

    init {
        val splitter = JBSplitter(false, 0.32f).apply {
            firstComponent = JBScrollPane(entryList).apply { border = BorderFactory.createEmptyBorder() }
            secondComponent = bench()
        }
        add(toolbar(), BorderLayout.WEST)
        add(splitter, BorderLayout.CENTER)
        store.setListener { refresh() }
        refresh()
    }

    override fun dispose() {
        store.setListener(null)
    }

    private fun bench(): JPanel {
        val controls = JPanel(FlowLayout(FlowLayout.LEFT, JBUI.scale(6), JBUI.scale(4))).apply {
            add(JBLabel("Prompt:"))
            add(promptKind)
            add(JBLabel("Language:"))
            add(language)
            add(withProject)
            add(runButton)
        }
        val top = JPanel(BorderLayout()).apply {
            add(JBLabel("Transcript").apply { border = JBUI.Borders.empty(4, 6, 0, 0) }, BorderLayout.NORTH)
            add(JBScrollPane(transcript), BorderLayout.CENTER)
            add(controls, BorderLayout.SOUTH)
        }
        val bottom = JPanel(BorderLayout()).apply {
            add(JBLabel("Output").apply { border = JBUI.Borders.empty(4, 6, 0, 0) }, BorderLayout.NORTH)
            add(JBScrollPane(output), BorderLayout.CENTER)
            add(status.apply { border = JBUI.Borders.empty(2, 6) }, BorderLayout.SOUTH)
        }
        return JPanel(BorderLayout()).apply {
            add(JBSplitter(true, 0.45f).apply {
                firstComponent = top
                secondComponent = bottom
            }, BorderLayout.CENTER)
        }
    }

    private fun toolbar(): JPanel {
        val actions =
            DefaultActionGroup(
                object : DumbAwareAction("Copy All", "Copy every transcript to the clipboard", AllIcons.Actions.Copy) {
                    override fun getActionUpdateThread(): ActionUpdateThread = ActionUpdateThread.BGT

                    override fun actionPerformed(event: AnActionEvent) {
                        CopyPasteManager.getInstance().setContents(StringSelection(store.render()))
                    }
                },
                object : DumbAwareAction("Delete", "Remove the selected transcript", AllIcons.General.Remove) {
                    override fun getActionUpdateThread(): ActionUpdateThread = ActionUpdateThread.EDT

                    override fun update(event: AnActionEvent) {
                        event.presentation.isEnabled = entryList.selectedValue != null
                    }

                    override fun actionPerformed(event: AnActionEvent) {
                        entryList.selectedValue?.let { store.remove(it.id) }
                    }
                },
                object : DumbAwareAction("Clear", "Remove every stored transcript", AllIcons.Actions.GC) {
                    override fun getActionUpdateThread(): ActionUpdateThread = ActionUpdateThread.BGT

                    override fun actionPerformed(event: AnActionEvent) = store.clear()
                },
            )
        val toolbar = ActionManager.getInstance().createActionToolbar(ActionPlaces.TOOLWINDOW_CONTENT, actions, false)
        toolbar.targetComponent = entryList
        return JPanel(BorderLayout()).apply { add(toolbar.component, BorderLayout.NORTH) }
    }

    /** Reloads the list, keeping the selected entry selected when it still exists. */
    private fun refresh() {
        val selected = entryList.selectedValue?.id
        listModel.clear()
        store.entries().forEach(listModel::addElement)
        val index = (0 until listModel.size()).firstOrNull { listModel.get(it).id == selected }
        when {
            index != null -> entryList.selectedIndex = index
            !listModel.isEmpty -> entryList.selectedIndex = 0
            else -> showSelected()
        }
    }

    private fun showSelected() {
        val entry = entryList.selectedValue
        if (entry == null) {
            transcript.text = ""
            output.text = VoxTranscriptStore.EMPTY_TEXT
            status.text = " "
            runButton.isEnabled = false
            return
        }
        transcript.text = entry.transcript
        transcript.caretPosition = 0
        output.text = if (entry.failed()) entry.error else entry.output
        output.caretPosition = 0
        language.selectedItem = VoxLanguages.normalise(entry.language) ?: VoxLanguages.DEFAULT_CODE
        status.text = describe(entry)
        runButton.isEnabled = true
    }

    private fun describe(entry: VoxTranscriptStore.Entry): String {
        val parts = mutableListOf("dictated ${entry.time()}", entry.language, entry.device)
        if (entry.contextExchanges > 0) parts.add("${entry.contextExchanges} exchanges of context")
        if (entry.delivery.isNotEmpty()) parts.add("→ ${entry.delivery}")
        if (entry.failed()) parts.add("failed")
        return parts.joinToString(" · ")
    }

    private fun run() {
        val text = transcript.text.trim()
        if (text.isEmpty()) {
            status.text = "nothing to send"
            return
        }
        val settings = VoxSettings.getInstance()
        val client = VoxClient(settings.baseUrl(), settings.requestTimeout())
        val dictation = promptKind.selectedItem == DICTATION
        val chosen = (language.selectedItem as? String) ?: VoxLanguages.DEFAULT_CODE
        val maxProjectBytes = if (withProject.isSelected) settings.maxProjectBytes() else 0
        runButton.isEnabled = false
        status.text = "running…"

        ProgressManager.getInstance()
            .run(
                object : Task.Backgroundable(project, "Vox: Applying the Prompt", true) {
                    private var result: TransformResult? = null
                    private var failure: String? = null

                    override fun run(indicator: ProgressIndicator) {
                        indicator.isIndeterminate = true
                        val context = ProjectContext.read(project, maxProjectBytes)
                        result =
                            try {
                                client.transform(
                                    TransformRequest(
                                        text = text,
                                        dictation = dictation,
                                        language = chosen,
                                        project = context?.text,
                                        // The conversation belongs to a live dictation, not to a
                                        // replay: replaying with today's context would grade the
                                        // prompt against something the original never saw.
                                        context = null,
                                    )
                                )
                            } catch (e: VoxException) {
                                failure = e.message
                                null
                            }
                    }

                    override fun onThrowable(error: Throwable) {
                        failure = error.message ?: "the request failed"
                    }

                    override fun onFinished() {
                        runButton.isEnabled = true
                        val finished = result
                        if (finished == null) {
                            status.text = failure ?: "the request failed"
                            return
                        }
                        output.text = finished.output
                        output.caretPosition = 0
                        val kind = if (dictation) "dictation" else "task"
                        status.text = "$kind prompt · $chosen · ${finished.totalMs} ms · not stored"
                    }
                }
            )
    }

    private fun languageCombo(): ComboBox<String> {
        val codes = VoxLanguages.optionsIncluding(VoxSettings.getInstance().language())
        return ComboBox(DefaultComboBoxModel(codes.toTypedArray())).apply {
            renderer = SimpleListCellRenderer.create("") { value -> VoxLanguages.label(value.orEmpty()) }
            selectedItem = VoxSettings.getInstance().language()
        }
    }

    private fun editor(editable: Boolean): JBTextArea =
        JBTextArea().apply {
            isEditable = editable
            lineWrap = true
            wrapStyleWord = true
            font = Font(Font.MONOSPACED, Font.PLAIN, font.size)
            border = JBUI.Borders.empty(6)
        }

    private companion object {
        const val TASK = "Task"
        const val DICTATION = "Dictation"
        const val PROMPT_HINT =
            "Task formalises speech into a request; Dictation only punctuates it and drops filler."
    }
}
