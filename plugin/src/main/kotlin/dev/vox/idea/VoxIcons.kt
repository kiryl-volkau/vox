package dev.vox.idea

import com.intellij.openapi.util.IconLoader
import javax.swing.Icon

/**
 * The plugin's own icons.
 *
 * Each name resolves to `/icons/<name>.svg`, and the IDE picks the `_dark` variant next to it on a
 * dark theme by itself - the `_dark` files must stay beside the light ones for that to work.
 */
object VoxIcons {

    /** Microphone, idle. */
    @JvmField val Vox: Icon = load("/icons/vox.svg")

    /** Microphone in the recording colour, for the action and the status bar while capturing. */
    @JvmField val Recording: Icon = load("/icons/voxRecording.svg")

    private fun load(path: String): Icon = IconLoader.getIcon(path, VoxIcons::class.java)
}
