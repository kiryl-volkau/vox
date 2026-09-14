package dev.vox.idea

import com.intellij.openapi.diagnostic.logger
import javax.sound.sampled.AudioFormat
import javax.sound.sampled.AudioSystem
import javax.sound.sampled.DataLine
import javax.sound.sampled.Mixer
import javax.sound.sampled.TargetDataLine

/**
 * The microphones this IDE can actually record from.
 *
 * A device is identified by its name rather than by an index, because an index means nothing
 * across restarts or across sound stacks - the Windows companion configures the same microphone
 * through PortAudio in `config/client.yaml`, whose indices do not match Java Sound's at all. A
 * name survives both, so the same string in either configuration picks the same microphone.
 */
internal object AudioDevices {

    /** One selectable input, in the form the settings combo shows and stores. */
    class InputDevice(val name: String, val label: String)

    /**
     * Returns every mixer that can open a recording line, in the order the platform reports them.
     *
     * Never throws: a sound stack that refuses to be enumerated yields an empty list, which the
     * caller renders as "system default only". A device is listed once even when several of the
     * candidate sample rates work.
     */
    fun list(): List<InputDevice> {
        val mixers =
            try {
                AudioSystem.getMixerInfo()
            } catch (e: SecurityException) {
                log.warn("cannot enumerate audio devices", e)
                return emptyList()
            }
        val found = LinkedHashMap<String, InputDevice>()
        for (info in mixers) {
            val name = info.name?.trim().orEmpty()
            if (name.isEmpty() || !canRecord(info)) continue
            val vendor = info.vendor?.trim().orEmpty()
            val label = if (vendor.isEmpty() || vendor in name) name else "$name ($vendor)"
            found.putIfAbsent(name, InputDevice(name, label))
        }
        return found.values.toList()
    }

    /**
     * Resolves a configured device name to a mixer, or null for the system default.
     *
     * A blank name means the default. An exact name match wins; otherwise the first input device
     * whose name contains the configured text, case-insensitively, is used - the same rule the
     * Windows companion applies - so a shortened or partially remembered name still resolves.
     * Returns null when nothing matches, which records from the default device rather than
     * failing: a microphone that was unplugged must not stop dictation.
     */
    fun resolve(name: String): Mixer.Info? {
        val needle = name.trim()
        if (needle.isEmpty()) return null
        val mixers =
            try {
                AudioSystem.getMixerInfo()
            } catch (e: SecurityException) {
                log.warn("cannot enumerate audio devices", e)
                return null
            }
        val recordable = mixers.filter { canRecord(it) }
        val exact = recordable.firstOrNull { it.name?.trim() == needle }
        if (exact != null) return exact
        val partial = recordable.firstOrNull { it.name?.contains(needle, ignoreCase = true) == true }
        if (partial == null) log.warn("no input device matching \"$needle\"; using the default")
        return partial
    }

    /** Opens a recording line on [mixer], or on the default device when it is null. */
    fun openLine(mixer: Mixer.Info?, format: AudioFormat): TargetDataLine {
        val info = DataLine.Info(TargetDataLine::class.java, format)
        val line = if (mixer == null) AudioSystem.getLine(info) else AudioSystem.getMixer(mixer).getLine(info)
        return line as TargetDataLine
    }

    /** True when [mixer] supports a recording line in any of the formats Vox captures. */
    fun supports(mixer: Mixer.Info?, format: AudioFormat): Boolean =
        try {
            val info = DataLine.Info(TargetDataLine::class.java, format)
            if (mixer == null) AudioSystem.isLineSupported(info) else AudioSystem.getMixer(mixer).isLineSupported(info)
        } catch (e: IllegalArgumentException) {
            log.debug("mixer ${mixer?.name} rejected a format probe", e)
            false
        } catch (e: SecurityException) {
            log.debug("mixer ${mixer?.name} rejected a format probe", e)
            false
        }

    private fun canRecord(mixer: Mixer.Info): Boolean =
        AudioRecorder.SAMPLE_RATES.any { supports(mixer, AudioRecorder.formatFor(it)) }

    private val log = logger<AudioDevices>()
}
