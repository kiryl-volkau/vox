package dev.vox.idea

import com.google.gson.JsonObject
import com.google.gson.JsonParser
import com.google.gson.JsonSyntaxException
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.net.URI
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import java.net.http.HttpTimeoutException
import java.nio.charset.StandardCharsets
import java.time.Duration
import java.util.UUID

const val VOX_CLIENT_ID = "vox-idea"

/** A backend call failed; [message] is already short enough for a balloon. */
class VoxException(message: String, cause: Throwable? = null) : RuntimeException(message, cause)

class ProcessRequest(
    val wav: ByteArray,
    val audioSeconds: Double,
    val project: String?,
    val projectName: String?,
    val context: String?,
    val language: String,
    val clientVersion: String,
)

/**
 * How the backend's second pass read one request.
 *
 * Reporting only: it never carries the message, and every field can legitimately be empty when
 * the pass ran but found nothing to say. The whole object is null when the pass could not answer
 * at all, which the backend treats as normal rather than as a failure, so the UI has to as well.
 *
 * [action] and [target] are the verb the model heard and what it aimed it at, [constraints] the
 * limits it kept, [uncertainty] what it read as hedged. All of them quote the speech, so they are
 * in the spoken language whatever language the message came back in. [tool] is one of "none",
 * "subagent", "review" or "plan"; anything but "none" means a line was appended to the message.
 */
class VoxAnalysis(
    val action: String,
    val target: String,
    val constraints: List<String>,
    val uncertainty: List<String>,
    val tool: String,
    val why: String,
) {
    fun namesATool(): Boolean = tool.isNotEmpty() && tool != "none"

    fun isEmpty(): Boolean =
        action.isEmpty() &&
            target.isEmpty() &&
            constraints.isEmpty() &&
            uncertainty.isEmpty() &&
            !namesATool()
}

class ProcessResult(
    val requestId: String,
    val transcript: String,
    val output: String,
    val totalMs: Int,
    val analysis: VoxAnalysis?,
)

/** Replaying one already-transcribed text through a prompt, with no microphone involved. */
class TransformRequest(
    val text: String,
    val dictation: Boolean,
    val language: String,
    val project: String?,
    val context: String?,
)

class TransformResult(
    val requestId: String,
    val output: String,
    val totalMs: Int,
    val analysis: VoxAnalysis?,
)

/** The backend's LLM connection. The API key is reported as set or not, never returned. */
class LlmConfig(
    val baseUrl: String,
    val model: String,
    val apiKeySet: Boolean,
    val warmup: Boolean,
    val overridden: Boolean,
    val ready: Boolean,
    val error: String,
)

/** A partial change. A null field is left alone; an empty [apiKey] clears the key. */
class LlmConfigUpdate(
    val baseUrl: String? = null,
    val model: String? = null,
    val apiKey: String? = null,
    val warmup: Boolean? = null,
)

class HealthResult(
    val status: String,
    val sttReady: Boolean,
    val sttModel: String,
    val sttDevice: String,
    val sttError: String,
    val llmReady: Boolean,
    val llmModel: String,
    val llmBaseUrl: String,
    val llmError: String,
) {
    /** True when the backend can serve a dictation end to end right now. */
    val usable: Boolean
        get() = status == "ready" && sttReady && llmReady
}

/**
 * The Vox HTTP contract: `POST /v1/process` (multipart) and `GET /health`.
 *
 * Every failure - unreachable host, timeout, backend error body - surfaces as [VoxException] with a
 * message meant for the user. Calls block, so they belong on a background thread.
 */
class VoxClient(private val baseUrl: String, private val requestTimeout: Duration) {

    private val http: HttpClient =
        HttpClient.newBuilder()
            .version(HttpClient.Version.HTTP_1_1)
            .connectTimeout(CONNECT_TIMEOUT)
            .build()

    fun process(request: ProcessRequest): ProcessResult {
        val boundary = "VoxBoundary" + UUID.randomUUID().toString().replace("-", "")
        val httpRequest =
            HttpRequest.newBuilder(uri("/v1/process"))
                .timeout(requestTimeout)
                .header("Content-Type", "multipart/form-data; boundary=$boundary")
                .POST(HttpRequest.BodyPublishers.ofByteArray(multipartBody(boundary, request)))
                .build()
        val body = send(httpRequest)
        return ProcessResult(
            requestId = body.stringOrEmpty("request_id"),
            transcript = body.stringOrEmpty("transcript"),
            output = body.stringOrEmpty("output"),
            totalMs = body.getAsJsonObject("timings_ms")?.get("total")?.takeIf { it.isJsonPrimitive }?.asInt ?: 0,
            analysis = parseAnalysis(body),
        )
    }

    /**
     * Runs ``text`` through a prompt again, skipping speech recognition entirely.
     *
     * This is what the workbench drives: the prompts, the glossary and the model all stay on
     * the backend, so replaying a transcript exercises exactly the pipeline a real dictation
     * would, minus the microphone.
     */
    fun transform(request: TransformRequest): TransformResult {
        val body = JsonObject().apply {
            addProperty("text", request.text)
            addProperty("dictation", request.dictation)
            addProperty("language", request.language)
            request.project?.let { addProperty("project", it) }
            request.context?.let { addProperty("context", it) }
        }
        val httpRequest =
            HttpRequest.newBuilder(uri("/v1/transform"))
                .timeout(requestTimeout)
                .header("Content-Type", "application/json; charset=utf-8")
                .POST(HttpRequest.BodyPublishers.ofString(body.toString(), StandardCharsets.UTF_8))
                .build()
        val response = send(httpRequest)
        return TransformResult(
            requestId = response.stringOrEmpty("request_id"),
            output = response.stringOrEmpty("output"),
            totalMs = response.getAsJsonObject("timings_ms")?.get("total")?.takeIf { it.isJsonPrimitive }?.asInt ?: 0,
            analysis = parseAnalysis(response),
        )
    }

    /** Reads the backend's LLM connection. */
    fun readLlmConfig(): LlmConfig =
        parseLlmConfig(
            send(HttpRequest.newBuilder(uri("/v1/config")).timeout(CONFIG_TIMEOUT).GET().build())
        )

    /**
     * Changes the backend's LLM connection and returns what it became.
     *
     * The backend re-probes the new endpoint before answering, so the returned [LlmConfig.ready]
     * and [LlmConfig.error] already say whether it works - the caller does not need a second
     * health call to find out.
     */
    fun writeLlmConfig(update: LlmConfigUpdate): LlmConfig {
        val body = JsonObject().apply {
            update.baseUrl?.let { addProperty("base_url", it) }
            update.model?.let { addProperty("model", it) }
            update.apiKey?.let { addProperty("api_key", it) }
            update.warmup?.let { addProperty("warmup", it) }
        }
        val request =
            HttpRequest.newBuilder(uri("/v1/config"))
                .timeout(CONFIG_TIMEOUT)
                .header("Content-Type", "application/json; charset=utf-8")
                .PUT(HttpRequest.BodyPublishers.ofString(body.toString(), StandardCharsets.UTF_8))
                .build()
        return parseLlmConfig(send(request))
    }

    private fun parseLlmConfig(body: JsonObject): LlmConfig =
        LlmConfig(
            baseUrl = body.stringOrEmpty("base_url"),
            model = body.stringOrEmpty("model"),
            apiKeySet = body.booleanOrFalse("api_key_set"),
            warmup = body.booleanOrFalse("warmup"),
            overridden = body.booleanOrFalse("overridden"),
            ready = body.booleanOrFalse("ready"),
            error = body.stringOrEmpty("error"),
        )

    fun health(): HealthResult {
        val httpRequest =
            HttpRequest.newBuilder(uri("/health")).timeout(HEALTH_TIMEOUT).GET().build()
        val body = send(httpRequest)
        val stt = body.getAsJsonObject("stt")
        val llm = body.getAsJsonObject("llm")
        return HealthResult(
            status = body.stringOrEmpty("status"),
            sttReady = stt?.booleanOrFalse("ready") ?: false,
            sttModel = stt?.stringOrEmpty("model").orEmpty(),
            sttDevice = stt?.stringOrEmpty("device").orEmpty(),
            sttError = stt?.stringOrEmpty("error").orEmpty(),
            llmReady = llm?.booleanOrFalse("ready") ?: false,
            llmModel = llm?.stringOrEmpty("model").orEmpty(),
            llmBaseUrl = llm?.stringOrEmpty("base_url").orEmpty(),
            llmError = llm?.stringOrEmpty("error").orEmpty(),
        )
    }

    private fun uri(path: String): URI =
        try {
            URI.create(baseUrl + path)
        } catch (e: IllegalArgumentException) {
            throw VoxException("the backend URL \"$baseUrl\" is not a valid URL", e)
        }

    private fun send(request: HttpRequest): JsonObject {
        val response =
            try {
                http.send(request, HttpResponse.BodyHandlers.ofByteArray())
            } catch (e: HttpTimeoutException) {
                throw VoxException("the backend did not answer within ${requestTimeout.toSeconds()} s", e)
            } catch (e: IOException) {
                throw VoxException("cannot reach the backend at $baseUrl", e)
            } catch (e: InterruptedException) {
                Thread.currentThread().interrupt()
                throw VoxException("the request was interrupted", e)
            }
        val text = String(response.body(), StandardCharsets.UTF_8)
        if (response.statusCode() !in 200..299) throw VoxException(errorMessage(response.statusCode(), text))
        return parse(text)
    }

    private fun parse(text: String): JsonObject =
        try {
            JsonParser.parseString(text).asJsonObject
        } catch (e: JsonSyntaxException) {
            throw VoxException("the backend returned a response Vox could not read", e)
        } catch (e: IllegalStateException) {
            throw VoxException("the backend returned a response Vox could not read", e)
        }

    private fun errorMessage(status: Int, text: String): String {
        val code =
            try {
                JsonParser.parseString(text).asJsonObject.stringOrEmpty("error")
            } catch (e: RuntimeException) {
                ""
            }
        return ERROR_MESSAGES[code] ?: "the backend answered $status"
    }

    private fun multipartBody(boundary: String, request: ProcessRequest): ByteArray {
        val body = ByteArrayOutputStream(request.wav.size + MULTIPART_OVERHEAD_BYTES)
        body.writeAscii("--$boundary\r\n")
        body.writeAscii("Content-Disposition: form-data; name=\"audio\"; filename=\"audio.wav\"\r\n")
        body.writeAscii("Content-Type: audio/wav\r\n\r\n")
        body.write(request.wav)
        body.writeAscii("\r\n")
        val fields =
            linkedMapOf(
                "audio_seconds" to "%.3f".format(java.util.Locale.ROOT, request.audioSeconds),
                "client_id" to VOX_CLIENT_ID,
                "client_version" to request.clientVersion,
                "language" to request.language,
            )
        request.project?.let { fields["project"] = it }
        request.projectName?.let { fields["project_name"] = it }
        request.context?.let { fields["context"] = it }
        for ((name, value) in fields) {
            body.writeAscii("--$boundary\r\n")
            body.writeAscii("Content-Disposition: form-data; name=\"$name\"\r\n\r\n")
            body.write(value.toByteArray(StandardCharsets.UTF_8))
            body.writeAscii("\r\n")
        }
        body.writeAscii("--$boundary--\r\n")
        return body.toByteArray()
    }

    private fun ByteArrayOutputStream.writeAscii(text: String) =
        write(text.toByteArray(StandardCharsets.US_ASCII))

    private fun parseAnalysis(body: JsonObject): VoxAnalysis? {
        val analysis = body.getAsJsonObject("analysis") ?: return null
        val claudeCode = analysis.getAsJsonObject("claude_code")
        return VoxAnalysis(
            action = analysis.stringOrEmpty("action"),
            target = analysis.stringOrEmpty("target"),
            constraints = analysis.strings("constraints"),
            uncertainty = analysis.strings("uncertainty"),
            tool = claudeCode?.stringOrEmpty("tool").orEmpty(),
            why = claudeCode?.stringOrEmpty("why").orEmpty(),
        )
    }

    private fun JsonObject.strings(name: String): List<String> {
        val array = get(name)?.takeIf { it.isJsonArray }?.asJsonArray ?: return emptyList()
        return array.mapNotNull { element ->
            element.takeIf { it.isJsonPrimitive }?.asString?.trim()?.takeIf { it.isNotEmpty() }
        }
    }

    private fun JsonObject.stringOrEmpty(name: String): String {
        val member = get(name) ?: return ""
        return if (member.isJsonPrimitive) member.asString else ""
    }

    private fun JsonObject.booleanOrFalse(name: String): Boolean {
        val member = get(name) ?: return false
        return member.isJsonPrimitive && member.asJsonPrimitive.isBoolean && member.asBoolean
    }

    private companion object {
        val CONNECT_TIMEOUT: Duration = Duration.ofSeconds(3)
        val HEALTH_TIMEOUT: Duration = Duration.ofSeconds(10)
        // Longer than /health: a write re-probes the new endpoint before it answers.
        val CONFIG_TIMEOUT: Duration = Duration.ofSeconds(20)
        const val MULTIPART_OVERHEAD_BYTES = 1024

        val ERROR_MESSAGES =
            mapOf(
                "empty_audio" to "no audio was captured",
                "audio_too_large" to "the recording is too long",
                "empty_transcript" to "nothing was recognised",
                "empty_llm_output" to "the model returned nothing",
                "stt_unavailable" to "speech recognition is unavailable",
                "llm_timeout" to "the model timed out",
                "llm_unavailable" to "the model is unreachable",
                "llm_error" to "the model returned an error",
                "warming" to "the backend is still warming up",
                "internal_error" to "the backend failed",
            )
    }
}
