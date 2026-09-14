package dev.vox.idea

import com.intellij.openapi.diagnostic.logger
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import javax.sound.sampled.AudioFileFormat
import javax.sound.sampled.AudioFormat
import javax.sound.sampled.AudioInputStream
import javax.sound.sampled.AudioSystem
import javax.sound.sampled.LineUnavailableException
import javax.sound.sampled.Mixer
import javax.sound.sampled.TargetDataLine

/** The microphone could not be opened, or it produced nothing usable. */
class AudioCaptureException(message: String, cause: Throwable? = null) : RuntimeException(message, cause)

/** One finished capture: a complete RIFF/WAVE file held in memory, plus its duration. */
class RecordedAudio(val wav: ByteArray, val seconds: Double)

/**
 * Records mono 16-bit little-endian PCM into memory from [deviceName], or from the system default
 * when that is blank.
 *
 * The sample rate is whatever the mixer actually offers, tried in [SAMPLE_RATES] order, because a
 * device that refuses 16 kHz is common; the backend resamples anyway. Audio never reaches the disk.
 *
 * [start] and [stop]/[cancel] are expected to be called from the same thread (the EDT); the capture
 * itself runs on a daemon thread. [onLimitReached] fires on that capture thread once
 * [maxSeconds] of audio has been buffered, and further audio is dropped until the caller stops.
 */
class AudioRecorder(
    private val maxSeconds: Int,
    private val deviceName: String = "",
    private val onLimitReached: () -> Unit,
) {

    private val captured = ByteArrayOutputStream()
    private var line: TargetDataLine? = null
    private var format: AudioFormat? = null
    private var captureThread: Thread? = null

    @Volatile
    private var running = false

    /** The device this recorder actually opened, for the log and the status tooltip. */
    var openedDevice: String = ""
        private set

    fun start() {
        val mixer = AudioDevices.resolve(deviceName)
        val chosen =
            SAMPLE_RATES.map { formatFor(it) }.firstOrNull { AudioDevices.supports(mixer, it) }
                ?: fallbackFormat(mixer)
        val opened =
            try {
                AudioDevices.openLine(mixer, chosen).apply {
                    open(chosen)
                    start()
                }
            } catch (e: LineUnavailableException) {
                throw AudioCaptureException("the microphone is unavailable or already in use", e)
            } catch (e: IllegalArgumentException) {
                throw AudioCaptureException("no microphone is available", e)
            } catch (e: SecurityException) {
                throw AudioCaptureException("the IDE is not allowed to use the microphone", e)
            }

        synchronized(captured) { captured.reset() }
        line = opened
        format = chosen
        running = true
        openedDevice = mixer?.name?.trim() ?: DEFAULT_DEVICE_LABEL
        captureThread = Thread({ capture(opened, chosen) }, "vox-audio-capture").apply {
            isDaemon = true
            start()
        }
        log.debug("recording at ${chosen.sampleRate.toInt()} Hz from $openedDevice")
    }

    /** Stops the line and returns the captured audio, or null when nothing was recorded. */
    fun stop(): RecordedAudio? {
        val chosen = format ?: return null
        shutdown()
        val pcm = synchronized(captured) { captured.toByteArray() }
        val frames = pcm.size.toLong() / chosen.frameSize
        if (frames == 0L) return null
        val wav = ByteArrayOutputStream(pcm.size + WAV_HEADER_BYTES)
        AudioInputStream(ByteArrayInputStream(pcm), chosen, frames).use {
            AudioSystem.write(it, AudioFileFormat.Type.WAVE, wav)
        }
        return RecordedAudio(wav.toByteArray(), frames.toDouble() / chosen.frameRate)
    }

    fun cancel() {
        shutdown()
        synchronized(captured) { captured.reset() }
    }

    /**
     * The format to use when the chosen device advertises none of [SAMPLE_RATES].
     *
     * Java Sound under-reports what a mixer supports often enough that refusing here would make a
     * working microphone unusable; opening the line is the real test, and it throws on its own.
     */
    private fun fallbackFormat(mixer: Mixer.Info?): AudioFormat {
        if (mixer == null) throw AudioCaptureException("no input device offers 16-bit mono PCM")
        log.debug("${mixer.name} advertises no supported rate; trying ${SAMPLE_RATES.first()} Hz anyway")
        return formatFor(SAMPLE_RATES.first())
    }

    private fun capture(source: TargetDataLine, chosen: AudioFormat) {
        val maxBytes = maxSeconds.toLong() * chosen.frameRate.toLong() * chosen.frameSize
        val chunk = ByteArray(chosen.frameSize * (chosen.sampleRate.toInt() / CHUNKS_PER_SECOND))
        try {
            while (running) {
                val read = source.read(chunk, 0, chunk.size)
                if (read <= 0) break
                val full = synchronized(captured) {
                    captured.write(chunk, 0, read)
                    captured.size() >= maxBytes
                }
                if (full) {
                    running = false
                    onLimitReached()
                }
            }
        } catch (e: RuntimeException) {
            running = false
            log.warn("microphone capture stopped", e)
        }
    }

    private fun shutdown() {
        running = false
        line?.let {
            it.stop()
            it.close()
        }
        line = null
        captureThread?.takeIf { it !== Thread.currentThread() }?.join(THREAD_JOIN_MS)
        captureThread = null
    }

    companion object {
        const val DEFAULT_DEVICE_LABEL = "System default"

        val SAMPLE_RATES = floatArrayOf(16_000f, 48_000f, 44_100f).toList()

        /** The one capture format, at [rate]: mono, 16-bit signed, little-endian PCM. */
        fun formatFor(rate: Float): AudioFormat = AudioFormat(rate, BITS_PER_SAMPLE, 1, true, false)

        private val log = logger<AudioRecorder>()
        private const val BITS_PER_SAMPLE = 16
        private const val CHUNKS_PER_SECOND = 10
        private const val WAV_HEADER_BYTES = 64
        private const val THREAD_JOIN_MS = 2000L
    }
}
