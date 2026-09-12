# voice-code

Push-to-talk voice input for Claude Code on Windows 11.

Hold `Ctrl+Alt+Space`, say what you want in Russian mixed with English engineering terms, release.
A moment later the cleaned-up text is pasted into whatever window had focus - normally the Claude
Code prompt in the IntelliJ IDEA terminal. **Enter is never pressed for you.** You read the text,
edit it if you want, and submit it yourself.

Two pieces:

* a **Docker backend** (Linux, CUDA) running faster-whisper on the GPU plus a local
  OpenAI-compatible LLM that turns spoken speech into a usable prompt;
* a **native Windows companion** that owns the hotkey, the microphone, the on-screen overlay, the
  clipboard and the paste.

Nothing leaves the machine: no telemetry, no cloud API, no database, and audio is never written to
disk.

---

## Architecture

```
  WINDOWS 11 HOST (your interactive session)           DOCKER DESKTOP / WSL2
  =========================================            =====================

  +----------------------------------+
  | IntelliJ IDEA                    |
  |   Terminal                       |
  |     Claude Code   <----+         |
  +------------------------|---------+
                           |  Ctrl+V  (Enter is NEVER sent)
                           |
  +------------------------|---------+           +--------------------------------+
  | voice-code companion (native)    |           | voice-code-backend             |
  |                                  |           |                                |
  |  1. global hotkey   (pynput)     |           |  FastAPI on 0.0.0.0:8765       |
  |         |                        | multipart |    |                           |
  |  2. microphone   (sounddevice)   |   POST    |    +- decode WAV -> 16 kHz mono |
  |         |   WAV kept in RAM      |           |    |                           |
  |  3. HTTP  -----------------------+-----------+->  +- faster-whisper "turbo"   |
  |         |    127.0.0.1:8765      |/v1/process|    |  CTranslate2 + CUDA ------+--> RTX 5070 Ti
  |         |                        |           |    |                           |     16 GB
  |  4. clipboard + SendInput  <-----+-----------+--  +- mode prompt              |
  |         |        output text     |   JSON    |    |  modes/<mode>.md          |
  |  5. overlay (never takes focus)  |           |    |  + config/glossary.yaml   |
  +----------------------------------+           |    |                           |
                                                 |    +- LLM chat completion -----+--+
                                                 +--------------------------------+  |
                                                                                     |
   LLM endpoint - pick one:                                                          |
                                                                                     |
   (a) a model server already running on Windows                                     |
       LLM_BASE_URL=http://host.docker.internal:11434/v1  <--------------------------+
       Ollama on 11434, or LM Studio on 1234. The container reaches the Windows
       host through "host.docker.internal", wired up in compose.yaml via
       extra_hosts: ["host.docker.internal:host-gateway"].

   (b) the bundled compose profile
       docker compose --profile bundled-model up -d
       LLM_BASE_URL=http://ollama:11434/v1  -----------> container "voice-code-ollama"
```

### Why the companion is not in Docker

Everything the companion does is a property of *your Windows desktop session*, and a Linux
container has none of it:

* **Global hotkeys** are a low-level Windows keyboard hook. A container has no Windows message
  queue and cannot see keystrokes on their way to IntelliJ.
* **The microphone** is a Windows WASAPI endpoint. Docker Desktop on Windows does not pass host
  audio devices into WSL2 containers.
* **Focus** - "which window was in front when I started talking" - is `GetForegroundWindow()`, a
  per-session Win32 concept.
* **The clipboard** is per-session too, and pasting means synthesising real keystrokes with
  `SendInput()` into another process's input queue.

So the split is deliberate: the container gets what wants a GPU and a Linux CUDA stack (Whisper,
the LLM), the Windows process gets what wants a Windows desktop. They talk over plain HTTP on
loopback.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Windows 11 | Developed on 11 Home 26200. |
| NVIDIA GPU + current driver | Developed on an RTX 5070 Ti (Blackwell, sm_120), 16 GB, driver 616.64. |
| Docker Desktop, WSL2 backend | Developed with 28.5.2 / Compose v2.40.3, WSL2 kernel 6.6.87.2. |
| Python 3.14 on the host | For the companion and the tests only. |
| `uv` | <https://docs.astral.sh/uv/> - used for the host venv and inside the image build. |
| A local LLM server | Ollama or LM Studio on Windows, **or** the bundled compose profile. |

The backend image installs its own Python 3.14 with `uv`, so the host Python matters only for the
companion.

### Docker Desktop / WSL2 / NVIDIA

Docker Desktop must be on the **WSL2** backend (Settings -> General -> "Use the WSL 2 based
engine"). GPU passthrough then works through the NVIDIA driver already installed on Windows - do
**not** install a Linux NVIDIA driver inside WSL. Confirm Docker exposes the `nvidia` runtime:

```powershell
docker info --format '{{json .Runtimes}}'
```

### Verify Docker GPU support

This is the one command that decides whether the backend can work at all:

```powershell
docker run --rm --gpus all nvidia/cuda:12.8.1-base-ubuntu24.04 nvidia-smi
```

You should get the usual `nvidia-smi` table naming your GPU. If it fails, fix that first: update
the NVIDIA driver, restart Docker Desktop, and check `wsl --status` reports WSL2.

---

## Connecting an LLM

The backend never runs the LLM itself; it calls any OpenAI-compatible endpoint
(`POST {LLM_BASE_URL}/chat/completions`, `GET {LLM_BASE_URL}/models`).

### Option A - a model server already running on Windows (recommended)

The model stays resident across backend restarts and is available to your other tools.

**Ollama** listens on `127.0.0.1:11434` by default, which a container cannot reach. Make it listen
on all interfaces, once:

```powershell
[Environment]::SetEnvironmentVariable("OLLAMA_HOST", "0.0.0.0", "User")
```

Quit Ollama from the tray, start it again, then pull the model:

```powershell
ollama pull qwen2.5:7b-instruct
ollama list
```

In `.env`:

```
LLM_BASE_URL=http://host.docker.internal:11434/v1
LLM_MODEL=qwen2.5:7b-instruct
```

**LM Studio**: load a model, open the Developer tab, enable serving on the local network, press
Start Server (default port 1234).

```
LLM_BASE_URL=http://host.docker.internal:1234/v1
LLM_MODEL=<the model id LM Studio shows>
```

If the Windows firewall prompts about the model server, allow it on private networks.

Once the stack is up you can prove the container reaches the host server:

```powershell
docker compose exec backend python -c "import urllib.request; print(urllib.request.urlopen('http://host.docker.internal:11434/api/tags', timeout=5).status)"
```

`200` means the path works.

### Option B - the bundled Ollama profile

`compose.yaml` ships an optional `ollama` service behind the `bundled-model` profile, with the GPU
reserved for it as well:

```powershell
docker compose --profile bundled-model up -d
docker compose exec ollama ollama pull qwen2.5:7b-instruct
```

and in `.env`:

```
LLM_BASE_URL=http://ollama:11434/v1
LLM_MODEL=qwen2.5:7b-instruct
```

Then restart the backend so it picks up the new `.env`:

```powershell
docker compose up -d backend
```

The weights live in the `ollama-models` volume, so the pull happens only once. Remember that from
now on every `docker compose up` for this stack needs `--profile bundled-model`, or the `ollama`
service will not start.

---

## Installation

All commands run from the repo root in PowerShell.

**1. Backend configuration**

```powershell
Copy-Item .env.example .env
notepad .env
```

The only lines you normally touch are `LLM_BASE_URL` and `LLM_MODEL`. Every other value in
`.env.example` is already the built-in default, so an empty `.env` behaves identically. `.env` is
gitignored.

**2. Build and start the backend**

```powershell
docker compose up -d --build
docker compose logs -f backend
```

The first build pulls the CUDA base image and installs the locked dependencies; the first start
then downloads the Whisper weights into the `hf-cache` volume - which is why the healthcheck has a
180 s start period. Wait for:

```
stt ready: model=turbo device=cuda compute_type=float16
```

**3. Install the Windows companion**

```powershell
.\scripts\install-client.ps1
```

which creates the host virtualenv and installs the client dependencies, equivalent to:

```powershell
uv sync --extra client
```

**4. Client configuration (optional)**

```powershell
Copy-Item config\client.example.yaml config\client.yaml
```

Every value in the example is the default, so you only need this file to change something - a
different microphone, different hotkeys, a longer timeout. `config/client.yaml` is gitignored.

---

## Daily use

```powershell
.\scripts\start.ps1
```

starts the compose stack, waits for `/health` to report `ready`, and launches the companion.
By hand that is:

```powershell
docker compose up -d
uv run voice-code-client
```

To shut everything down:

```powershell
.\scripts\stop.ps1
```

The companion adds a notification-area icon with **Status**, **Open config folder** and **Quit**.
It has no window of its own - only the small overlay that appears while you speak. If the tray
icon cannot be created the app logs it and keeps working.

---

## Hotkeys

| Combo | Mode | What you get |
|---|---|---|
| `Ctrl+Alt+Space` | **context** | Cleaned request wrapped in an instruction that makes Claude Code resolve "this"/"here" against the real repository before implementing. |
| `Ctrl+Alt+D` | **dictation** | Your words, punctuated. Nothing added, nothing rephrased. |
| `Ctrl+Alt+C` | **clean** | A short, explicit, self-contained request. |
| `Ctrl+Alt+T` | **task** | A fuller task statement with steps, constraints and open questions. |
| `Esc` | cancel | While recording: drops the audio, sends nothing. |

Hold the combo while you speak; release it when you are done. Releasing the **trigger** key
(`Space`, `D`, `C`, `T`) is what ends the recording - releasing `Ctrl` or `Alt` first does not, in
any order. Keyboard auto-repeat is ignored, so recording starts exactly once per press.

The modifier set must match exactly: `Ctrl+Alt+Shift+Space` is not `Ctrl+Alt+Space` and does
nothing. A second mode's hotkey pressed while a recording or a request is in flight is ignored -
there is never more than one recording at a time.

**Enter is never pressed automatically.** `paste.auto_submit` defaults to `false`, and leaving it
there is the point: recognition is good, not perfect, and a prompt you have not read is a prompt
you did not write. The text lands in the terminal and waits. Setting `auto_submit: true` in
`config/client.yaml` makes the companion press Enter right after the paste, if you insist.

**Why not `Ctrl+Space`?** IntelliJ binds it to basic code completion and consumes it before
anything else sees it, so a `Ctrl+Space` hotkey would either never fire or fight the IDE.
`Ctrl+Alt+<key>` combos stay clear of the IntelliJ terminal. All of it is configurable - see
[Changing hotkeys](#changing-hotkeys).

While the companion works the overlay shows, in order: `CONTEXT - Recording 00:04` ->
`Transcribing...` -> `Formatting...` -> `Ready`. It never takes focus: the window carries
`WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW`, so your caret stays where it was.

---

## Modes

All four modes share the same hard rules, written into `modes/*.md`: never invent requirements,
file names or acceptance criteria; never turn "посмотри"/"проверь" into "сделай"; never strip the
uncertainty out of "может быть" or "проверь, реально ли"; never propose new layers, patterns,
abstractions or refactorings that were not asked for; never translate identifiers or English
engineering terms. Temperature stays low - modes reformat what you said, they do not invent. The
glossary is injected as vocabulary, never substituted blindly into your sentence.

### dictation - `Ctrl+Alt+D`

Punctuation, capitalisation, filler removal, and fixing mangled English terms. Nothing else. Use
it for comments, commit messages, chat replies - anything where the words are already what you
mean.

```
spoken:  я думаю это лучше оставить здесь потому что оно используется только в этом компоненте
pasted:  Я думаю, это лучше оставить здесь, потому что оно используется только в этом компоненте.
```

### clean - `Ctrl+Alt+C`

Turns rambling speech into one short explicit request. Best when the request is self-contained and
does not depend on what is on your screen.

```
spoken:  посмотри этот сервис тут мембершип почему-то второй раз достается проверь реально ли
         он нужен если нет убери только без новых слоев

pasted:  Проверь, зачем в текущем сервисе membership загружается второй раз. Если второй lookup
         действительно избыточен, убери его и обнови затронутые тесты. Не добавляй новые
         архитектурные слои или абстракции без необходимости.
```

Note what survived: `membership` and `lookup` came back as identifiers (the glossary did that),
"проверь" stayed a check instead of becoming an order, and the "без новых слоёв" constraint was
kept explicitly.

### context - `Ctrl+Alt+Space` (the default)

The same cleanup, except **references to context are deliberately left alone**. "здесь", "этот",
"как мы делали для units" stay exactly as spoken, because the backend cannot see your repository
and must not guess. The normalized request is then wrapped in a meta-prompt that tells Claude Code
to resolve those references itself, against the real code, *before* writing anything.

What gets pasted begins with:

```
VOICE TASK - PROMPT ENRICHMENT MODE

Do NOT implement this. Do not edit, create or delete any file, ...
```

and ends with your normalized request between `---` fences. Claude Code then reads the repository,
resolves "this service" into an actual class, and answers with one precise engineering prompt -
which you read and send on. This is the mode for when you are looking at code and talking about
what you see.

### task - `Ctrl+Alt+T`

For thinking out loud. Produces 1-3 sentences saying what to do, then - only if you actually spoke
them - a short list of concrete steps, affected places and constraints, and finally, only if you
expressed real uncertainty, an `Открытые вопросы` section listing what is unresolved. No
uncertainty in the speech means that section is absent entirely.

`GET /v1/modes` returns the same list at runtime, with each mode's `requires_llm` and
`wrap_for_claude` flags.

---

## Customising

### Changing hotkeys

Edit `hotkeys` in `config/client.yaml`:

```yaml
hotkeys:
  bindings:
    context: "ctrl+alt+space"
    dictation: "ctrl+alt+d"
    clean: "ctrl+alt+c"
    task: "ctrl+alt+t"
  cancel: "esc"
```

A binding is `mod+mod+key`: modifiers are `ctrl`, `alt`, `shift`, `win` (aliases `control`,
`option`, `cmd`/`super`/`meta`/`windows` are accepted), and exactly one trigger key -
`space`, `esc`, `a`-`z`, `0`-`9`, `f1`-`f12`. The key on the left of the colon is the mode name,
which must match a file in `modes/`. Restart the companion to apply. An unparseable combo makes
the companion exit with `hotkey error: ...` instead of starting half-configured.

### Adding glossary words

`config/glossary.yaml` maps a spoken form to the canonical engineering term:

```yaml
terms:
  "джигвард": "Jigward"
  "мембершип": "membership"
  "составной индекс": "composite index"
```

The canonical terms are fed to Whisper as an initial prompt (bias, not substitution) and the whole
mapping is rendered into the LLM prompt as vocabulary. **No blind string replacement is ever done
on the transcript**, so adding an entry cannot corrupt a sentence that happens to contain the same
syllables. Keys are lowercase spoken forms as the recogniser produces them.

The glossary is baked into the image, so apply changes with:

```powershell
docker compose up -d --build backend
```

### Adding a mode

Drop a new `modes/<name>.md` in place. **No Python change is needed** - the registry reads every
`*.md` in the directory at startup, and the client picks the mode up as soon as you bind a hotkey
to its name.

```markdown
---
name: commit
label: Commit
description: "Спонтанная речь превращается в одно сообщение коммита."
requires_llm: true
wrap_for_claude: false
temperature: 0.1
fallback_to_transcript: true
---

## SYSTEM
Ты — редактор сообщений коммитов. На вход приходит ASR-расшифровка устной речи разработчика.
Сформулируй одно сообщение коммита в повелительном наклонении, не длиннее одной строки.
Ничего не выдумывай: только то, что было сказано.
Формат ответа: ТОЛЬКО строка сообщения, без преамбул, кавычек и markdown-ограждений.

## USER
Словарь проекта:
{glossary}

Расшифровка:
{transcript}

## WRAPPER
```

Front-matter defaults if you omit a key: `name` = the filename stem, `label` = `name.title()`,
`description` = empty, `requires_llm` = `true`, `wrap_for_claude` = `false`,
`fallback_to_transcript` = `false`, `temperature` = the server default (`LLM_TEMPERATURE`).

The three sections are split on lines that are exactly `## SYSTEM`, `## USER` and `## WRAPPER`.
Placeholders are literal text replacement, not `str.format`, so braces elsewhere in your prompt are
safe: `{transcript}` and `{glossary}` in the USER section, `{normalized}` in the WRAPPER section.
`## WRAPPER` may be left empty unless `wrap_for_claude: true`, in which case it is required (and a
mode with `requires_llm: true` and an empty `## SYSTEM` is rejected at startup).

Then bind it and restart both sides:

```yaml
hotkeys:
  bindings:
    commit: "ctrl+alt+m"
```

```powershell
docker compose up -d --build backend
```

### Choosing a different microphone

```powershell
uv run voice-code-client --list-devices
```

prints `index  name  (rate Hz)`. Put either the index or a distinctive substring of the name into
`config/client.yaml`:

```yaml
audio:
  input_device: 3          # or: "Yeti"
  max_seconds: 120.0
  sample_rate: null        # null = the device's native rate; the server resamples to 16 kHz
```

A string matches the first input device whose name contains it, case-insensitively. `null` means
the Windows default input device.

### Choosing a different local model

Set `LLM_MODEL` in `.env` to whatever tag your model server reports, then restart the backend:

```powershell
docker compose up -d backend
Invoke-RestMethod http://127.0.0.1:8765/health | Select-Object -ExpandProperty llm
```

**VRAM budget.** Whisper `turbo` in float16 costs roughly 2 GB, and an 8B model at Q4 about 5-6 GB,
so `qwen2.5:7b-instruct` plus Whisper sits comfortably inside the 16 GB on a 5070 Ti with room for the KV
cache and your desktop. A 14B Q4 model still fits but gets tight; a 32B or 70B model does not, and
the symptom is not a clean error - it is Ollama spilling layers to system RAM and the LLM stage
going from under a second to tens of seconds, or the Whisper load failing with a CUDA OOM. If you
want a bigger model, either lower `STT_MODEL` (`medium`, `small`) or accept that they will not be
resident at the same time.

---

## Verifying Whisper is really on the GPU

`auto` silently falls back to CPU when CUDA is not reachable, and CPU Whisper is 10-30x slower -
so check rather than assume.

```powershell
Invoke-RestMethod http://127.0.0.1:8765/health | ConvertTo-Json -Depth 4
```

```json
{
  "status": "ready",
  "version": "0.1.0",
  "uptime_s": 91.4,
  "stt": { "ready": true, "model": "turbo", "device": "cuda", "compute_type": "float16", "error": null },
  "llm": { "ready": true, "model": "qwen2.5:7b-instruct", "base_url": "http://host.docker.internal:11434/v1", "error": null },
  "gpu": { "cuda_available": true, "device_count": 1 }
}
```

`stt.device` must be `cuda` and `stt.compute_type` `float16`. `cpu` / `int8` means the fallback
fired - the reason is in the logs:

```powershell
docker compose logs backend | Select-String -Pattern "stt ready|cuda|falling back|device"
```

Confirm the GPU is visible from inside the container and that VRAM is actually held:

```powershell
docker compose exec backend nvidia-smi
nvidia-smi
```

Inside the container you should see the full GPU table. On the host, watch **Memory-Usage** climb
by a couple of GB once the model has loaded - under WSL2 the host `nvidia-smi` process list often
cannot name processes living in the VM, so used memory, not the process table, is the reliable
signal. A quick timing check: a 5-second recording should come back with `timings_ms.transcription`
in the low hundreds on CUDA, and several seconds on CPU.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Overlay says `Service unavailable` | The backend is not reachable on `127.0.0.1:8765`. The companion retries once automatically and then discards the audio. Check `docker compose ps`, then `docker compose logs backend`. Note the port is published as `127.0.0.1:8765:8765` - loopback only, by design. |
| Overlay says `Backend warming up`, or requests answer `warming` | Whisper is still loading; the first start also downloads the weights. Watch `docker compose logs -f backend` until `stt ready:`. `/health` always returns HTTP 200 - read `status` from the body. |
| `Backend timeout` | The request exceeded the client's `server.timeout_seconds` (60 s) or the backend's `LLM_TIMEOUT_SECONDS` (30 s). Usually a model too large for the GPU, so it is running partly on the CPU. See the VRAM note above. |
| `stt.device` is `cpu` | CUDA was not usable at load time, so it fell back (and `float16` downgraded to `int8`). Re-run the `docker run --gpus all ... nvidia-smi` check, restart Docker Desktop, and make sure the backend image is the `-cudnn-` CUDA flavour - CTranslate2 4.8.2 needs cuDNN 9 at runtime, which the plain runtime image does not ship. |
| `llm.ready` is `false` | The container cannot reach the model server. For a Windows-side Ollama, `OLLAMA_HOST=0.0.0.0` must be set *and Ollama restarted*; `LLM_BASE_URL` must use `host.docker.internal`, not `127.0.0.1` (inside the container that is the container itself). Test with the `urllib` one-liner above, and check the Windows firewall. |
| Hotkey does nothing | (1) The companion is not running - look for the tray icon. (2) Something else owns the combo; try another. (3) **The target app is elevated.** Windows refuses synthetic input from a lower-integrity process, so if IntelliJ runs as Administrator the companion must be elevated too - run it as Administrator, or better, stop running the IDE elevated. (4) A malformed binding: the companion prints `config error:` naming the offending key and exits with code 2. (5) An unsupported trigger key - only the keys listed under [Changing hotkeys](#changing-hotkeys) are recognised. |
| Recording never stops | Only the trigger key's release stops it, and `audio.max_seconds` (120 s) caps it regardless. Press `Esc` to discard. |
| `Microphone unavailable` / `Microphone error` | The device is missing, in use exclusively by another app (Zoom, Teams, OBS), or blocked. Check Settings -> Privacy & security -> Microphone -> "Let desktop apps access your microphone", then `uv run voice-code-client --list-devices` and pin `audio.input_device`. |
| `No audio captured` | The stream opened but produced no frames - almost always the wrong input device, or a muted mic. |
| Text pasted into the wrong window | Should not happen: with `paste.only_if_target_window_unchanged: true` the companion compares the foreground window against the one recorded when you started speaking and, if focus moved, copies instead of pasting and shows `Copied - focus changed` - press `Ctrl+V` yourself. If you disabled that check, this is why. The companion also waits (up to 1 s) for you to release `Ctrl+Alt` before pasting, so the target receives `Ctrl+V` and not `Ctrl+Alt+V`. |
| Clipboard not restored, or `Clipboard busy` | The Windows clipboard is frequently locked by another process; `set_text` retries a few times. Restore happens `paste.restore_delay_ms` (600 ms) after the paste and is unconditional, so anything you copy inside that short window is overwritten by the restored text; lengthen or disable it if that bites. Failures to save or restore are logged and never fail the request. Set `paste.preserve_clipboard: false` to switch the behaviour off. |
| Hotkeys stop working on a Russian layout | Handled: when a `KeyCode` has no ASCII `char` (as with Cyrillic), the key is derived from the virtual-key code, so `Ctrl+Alt+D` fires on the physical `D`/`В` key in either layout. If it still misbehaves, run with `logging.level: DEBUG` and check which key name the listener reports. |
| Wrong words for English terms | Add the spoken form to `config/glossary.yaml` and rebuild the backend. |

---

## API

Base URL `http://127.0.0.1:8765`. Every response carries an `X-Request-ID` header. Errors are
always `{"error": "...", "detail": "..."}` - never a stack trace, never the API key.

| Method | Path | Body | Returns |
|---|---|---|---|
| `GET` | `/health` | - | `HealthResponse`. Always HTTP 200; read `status` (`ready` / `warming` / `degraded`). |
| `GET` | `/v1/modes` | - | `{"modes": [{name, label, description, requires_llm, wrap_for_claude}]}` |
| `POST` | `/v1/process` | multipart: `audio` (WAV), `mode` (default `context`), optional `client_id`, `client_version`, `audio_seconds` | `ProcessResponse` - the full pipeline. |
| `POST` | `/v1/transcribe` | multipart: `audio`, optional `language` | `{request_id, transcript, language, timings_ms}` - STT only, no LLM. |
| `POST` | `/v1/transform` | JSON `{"text": "...", "mode": "clean"}` | `{request_id, mode, normalized_text, output, timings_ms}` - replay a mode on existing text, no STT. Handy for iterating on a prompt. |

`POST /v1/process`:

```json
{
  "request_id": "3f9a1c07b2d4",
  "mode": "clean",
  "transcript": "посмотри этот сервис тут мембершип почему-то второй раз достается",
  "normalized_text": "Проверь, зачем в текущем сервисе membership загружается второй раз.",
  "output": "Проверь, зачем в текущем сервисе membership загружается второй раз.",
  "language": "ru",
  "timings_ms": { "transcription": 380, "llm": 910, "total": 1298 }
}
```

`output` is what the companion pastes. For `wrap_for_claude` modes (`context`) it is
`normalized_text` embedded in that mode's wrapper; otherwise the two are identical.

Error codes: `unknown_mode` (400), `empty_audio` (400), `audio_too_large` (413),
`invalid_request` (422), `empty_transcript` (422), `empty_llm_output` (502), `llm_error` (502),
`stt_unavailable` (503), `llm_unavailable` (503), `warming` (503), `llm_timeout` (504),
`internal_error` (500).

Interactive docs are at <http://127.0.0.1:8765/docs>.

---

## Privacy

* **No telemetry.** Nothing reports usage, crashes or metrics anywhere.
* **No cloud.** STT runs in your container on your GPU. The LLM is whatever local endpoint you
  configured. The only outbound network calls the project ever makes are: the configured
  `LLM_BASE_URL`, and one-time model weight downloads (Whisper weights on first backend start, and
  your `ollama pull`). Point `LLM_BASE_URL` at a hosted provider and that is no longer true - which
  is why `LLM_API_KEY` exists and why `.env` is gitignored.
* **Audio is never persisted.** It is captured into memory, encoded to WAV in memory, posted, and
  dropped. Nothing is written to disk, on either side. `*.wav` is gitignored as a second line of
  defence.
* **No database, no history.** The backend is stateless between requests; the only volumes are
  model caches (`hf-cache`, `ct2-cache`, `ollama-models`).
* **Transcripts and prompts are never logged at INFO**, on either side. `LOG_TEXT=true` (backend)
  and `logging.log_text: true` (client) add them to `DEBUG` output; both default to false, and they
  are debugging switches, not settings.
* **Clipboard contents** are read only to restore what you had, and only when
  `paste.preserve_clipboard` is on.
* The backend port is published as `127.0.0.1:8765:8765` - loopback only, not reachable from the
  LAN.

---

## Autostart (optional)

Not a Windows Service - deliberately. A service runs in session 0, where it has no desktop, no
clipboard, no foreground window and no way to send input to your applications; everything the
companion does would break. It must run as a normal process in your interactive session.

Build the exe (see below), then:

```powershell
$startup  = [Environment]::GetFolderPath("Startup")
$shell    = New-Object -ComObject WScript.Shell
$lnk      = $shell.CreateShortcut((Join-Path $startup "voice-code.lnk"))
$lnk.TargetPath       = (Resolve-Path .\dist\voice-code.exe).Path
$lnk.WorkingDirectory = (Get-Location).Path
$lnk.Description      = "voice-code push-to-talk companion"
$lnk.Save()
```

`Win+R` -> `shell:startup` opens that folder to check or remove it. The backend starts itself:
compose services use `restart: unless-stopped`, so they come back with Docker Desktop.

---

## Building the standalone exe

```powershell
.\scripts\build-client.ps1
```

produces `dist\voice-code.exe` - a single file you can run without a Python install and point the
startup shortcut at. It reads `config/client.yaml` from the working directory, then from beside the `.exe`
(so `dist\config\client.yaml` or a `config\` folder next to wherever you copy it works), and it
still needs the backend running.

The exe bundles only the companion: `httpx`, `pyyaml`, `numpy`, `sounddevice`, `pynput`, `pywin32`,
`pystray`, `pillow`, `tkinter`. It deliberately **excludes** `faster-whisper`, `ctranslate2`, CUDA
and everything LLM-related, which live in the container and would add gigabytes of dead weight to
a binary that never calls them.

---

## Project layout

```
voice-code/
├─ compose.yaml                  backend service, GPU reservation, optional bundled-model profile
├─ docker/Dockerfile             CUDA 12.8.1 + cuDNN runtime, uv-installed Python 3.14
├─ pyproject.toml                deps (server / client extras), ruff, mypy, pytest config
├─ uv.lock
├─ .env.example                  every backend setting, documented; copy to .env
├─ config/
│  ├─ client.example.yaml        every companion setting; copy to client.yaml
│  └─ glossary.yaml              spoken form -> canonical engineering term
├─ modes/
│  ├─ context.md                 default; keeps references, wraps for Claude Code
│  ├─ dictation.md               punctuation only
│  ├─ clean.md                   short explicit request
│  └─ task.md                    expanded task + open questions
├─ scripts/                      install-client.ps1, start.ps1, stop.ps1,
│                               build-client.ps1, smoke-test.ps1
├─ src/
│  ├─ voice_code_server/
│  │  ├─ main.py                 create_app(), lifespan, uvicorn entry point
│  │  ├─ api.py                  routes, error mapping, request ids
│  │  ├─ config.py               pydantic-settings, env vars
│  │  ├─ models.py               request/response models
│  │  ├─ transcription.py        device resolution, WAV decoding, faster-whisper
│  │  ├─ modes.py                mode file parsing and registry
│  │  ├─ glossary.py             glossary loading and prompt rendering
│  │  ├─ llm.py                  OpenAI-compatible client, output cleanup
│  │  ├─ processor.py            STT -> mode -> LLM -> wrapper pipeline
│  │  └─ health.py               server state and /health assembly
│  └─ voice_code_client/
│     ├─ main.py                 wiring, delivery, tray, CLI
│     ├─ state.py                hotkey parsing and the push-to-talk state machine (pure logic)
│     ├─ hotkeys.py              pynput adapter, layout-independent key normalisation
│     ├─ recorder.py             sounddevice capture, in-memory WAV encoding
│     ├─ api_client.py           httpx calls to the backend
│     ├─ clipboard.py            Win32 clipboard, SendInput paste, window handles
│     ├─ overlay.py              tkinter status overlay that never takes focus
│     └─ config.py               client.yaml loading and validation
└─ tests/
   ├─ conftest.py                fakes and builders shared by the suite
   ├─ unit/                      no GPU, microphone, network or Docker required
   └─ integration/               marked "integration"; needs a running backend/GPU
```

---

## Development

```powershell
uv sync --all-extras          # both extras plus the dev group
uv run ruff check .
uv run ruff format .
uv run mypy
uv run pytest
```

`scripts\install-client.ps1` syncs `--extra client` only, which uninstalls the server packages
the tests import. Run `uv sync --all-extras` before `uv run pytest`, and re-run the installer
afterwards if you want the companion's lean environment back.

`pytest` is configured with `-m 'not integration'`, so the default run needs neither a GPU nor a
backend. To run the integration tests against a live stack:

```powershell
uv run pytest -m integration
```

Ruff is set to line length 100 and `target-version = py314`; mypy runs in strict mode over `src`
and `tests`.

### Why Python 3.14 everywhere

Backend and companion both target 3.14, with no 3.13 fallback anywhere. The usual reason to hold
back is the STT stack, so it was checked first, in the actual image
(`nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04`): `faster-whisper==1.2.1` and `ctranslate2==4.8.2`
publish working cp314 wheels, they import cleanly, `ctranslate2.get_cuda_device_count()` returns 1,
`float16` / `bfloat16` / `int8` are all reported as supported compute types, and a real `turbo`
model load plus transcription on CUDA succeeded. Both versions are pinned exactly for that reason -
this is the pairing that was verified on this machine. Everything else in the dependency set (
FastAPI, pydantic 2, httpx, numpy 2.5, sounddevice, pynput, pywin32, pystray, PyInstaller) already
ships 3.14 wheels.
