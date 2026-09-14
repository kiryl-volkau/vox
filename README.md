# vox

Voice input for Claude Code on Windows 11.

Press the trigger, say what you want in Russian mixed with English engineering terms, press it
again. A moment later the cleaned-up text is sitting in the Claude Code prompt in the IntelliJ IDEA
terminal. **Enter is never pressed for you.** You read the text, edit it if you want, and submit it
yourself.

The project is called **vox**. The checkout directory may still be named `voice-code` - it was
renamed in place and the folder name is cosmetic. Everything that has a name of its own is `vox`:
the packages `vox_server` and `vox_client`, the console scripts `vox-server` and `vox-client`, the
containers `vox-backend` and `vox-ollama`, and the compose project, which `compose.yaml` pins with
`name: vox` so renaming the folder again cannot orphan the volumes.

Three pieces:

* a **Docker backend** (Linux, CUDA) running faster-whisper on the GPU plus a local
  OpenAI-compatible LLM that turns spoken speech into a usable prompt;
* an **IntelliJ IDEA plugin** - the primary client. It records, sends, and types the result straight
  into the terminal tab you are looking at, and it sends the open project's `.vox.md` as context;
* a **native Windows companion** that owns two global hotkeys, the microphone, an on-screen overlay,
  the clipboard and the paste - for dictating into everything that is not the IDE.

Nothing leaves the machine: no telemetry, no cloud API, no database, and audio is never written to
disk. **The backend never reads your repository** - the client sends one file, the one you wrote
for it.

---

## Architecture

```
  WINDOWS 11 HOST (your interactive session)           DOCKER DESKTOP / WSL2
  =========================================            =====================

  +----------------------------------+
  | IntelliJ IDEA                    |
  |   Terminal                       |
  |     Claude Code   <--------+     |
  +----------------------------|-----+
                               |  typed in by the plugin, or pasted by
                               |  the companion - Enter is NEVER sent
  +----------------------------|-----+           +--------------------------------+
  | vox plugin (in the IDE)    |     |           | vox-backend                    |
  |   Ctrl+Alt+Shift+D, a toggle     |           |                                |
  |   javax.sound, WAV kept in RAM   | multipart |  FastAPI on 0.0.0.0:8765       |
  |   <open project>/.vox.md         |   POST    |    |                           |
  +---------------+------------------+-----------+->  +- decode WAV -> 16 kHz mono|
                  |                   /v1/process|    |                           |
  +---------------+------------------+           |    +- faster-whisper "turbo"   |
  | vox companion (native)           |           |    |  CTranslate2 + CUDA ------+--> RTX 5070 Ti
  |   Ctrl+Alt+Space/D, held (pynput)|           |    |                           |     16 GB
  |   sounddevice, WAV kept in RAM   |           |    +- prompts                  |
  |   <focused project>/.vox.md      |           |    |  prompt.md, dictation.md  |
  |   clipboard + SendInput paste    |           |    |  + config/glossary.yaml   |
  |   overlay (never takes focus)    |           |    |  + .vox.md in the SYSTEM  |
  +----------------------------------+           |    |    message                |
                                                 |    |                           |
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
       LLM_BASE_URL=http://ollama:11434/v1  -----------> container "vox-ollama"
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
loopback. The IntelliJ plugin lives inside the IDE for the same kind of reason: it is the only
client that already knows which project is open and which terminal tab you are looking at.

### Which client to use when

Both clients speak the same HTTP contract to the same backend, and both can run at once. The plugin
only ever calls `/v1/process`; the companion also has `/v1/dictate` behind its second hotkey.

| | IntelliJ plugin | Windows companion |
|---|---|---|
| Where the text lands | Typed into the active Terminal tab | Pasted into whatever window has focus |
| Trigger | A toggle: press to start, press to stop | Push-to-talk: hold `record` or `dictate`, release to send |
| Prompt | Always the task prompt - no dictation option | Task prompt on `record`, dictation prompt on `dictate` |
| `.vox.md` | The open project's, automatically | The project whose folder name is in the focused window's title, and only if you listed its root in `client.yaml` |
| Needs | Nothing beyond the IDE | A Python install or `vox.exe`, plus a tray process |

Use the **plugin** for talking to Claude Code, which is the whole point of the project: it knows the
project, it knows the terminal, and it needs no configuration. Use the **companion** when the target
is not an IDE terminal - a browser, a chat window, a commit dialog - or when you want its `dictate`
hotkey, which returns your own words cleaned up instead of turned into a task (see
[The prompts](#the-prompts)). The plugin has only one trigger and always runs the task prompt: there
is no dictation prompt inside the IDE.

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
| IntelliJ IDEA 2026.2+ | For the plugin. Built against Ultimate 2026.2.2 (IU-262.10315.125). |
| A JetBrains Runtime 21+ | Only to *build* the plugin. `C:\Users\<you>\.jdks\jbr-25.0.2`, or the `jbr` folder inside the IDE installation. `java` on PATH is JDK 1.8 here and is far too old. |

The backend image installs its own Python 3.14 with `uv`, so the host Python matters only for the
companion and the tests. Installing a prebuilt plugin zip needs no JDK at all.

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

### Option C - somebody else's hardware

The LLM does not have to run on this machine. `LLM_BASE_URL` accepts any OpenAI-compatible
endpoint - a box on the LAN, a company gateway, a hosted provider - and the client sends
`Authorization: Bearer $LLM_API_KEY` with every call and probes `GET $LLM_BASE_URL/models` for
readiness.

```
LLM_BASE_URL=https://api.example.com/v1
LLM_MODEL=<the model id that endpoint serves>
LLM_API_KEY=<a real key>
LLM_WARMUP=false
```

This is how you take the LLM off the local GPU entirely: Whisper keeps it to itself, which frees
roughly the size of the model you were running (~4.7 GB for a 7B Q4). Set `LLM_WARMUP=false` with
it - the warmup completion exists to force a local model off disk into VRAM, so against a hosted
endpoint it buys nothing and is billed like any other request.

Two things to be clear-eyed about:

- **Transcripts leave the machine.** The endpoint receives the transcript, your `.vox.md` and any
  conversation context you send. Audio never does - Whisper still runs locally - but the text of
  everything you dictate does. That is the one guarantee the local-only setup makes and this gives
  up, so it should be a decision rather than a default.
- **OpenAI-compatible means the chat completions protocol.** Anthropic's own Messages API is not
  that, and pointing this at it will not work; it needs a different client, not a different URL.

Settings | Tools | Vox | Test Connection reports the LLM separately from speech recognition and
warns when either is not ready, which is the failure you actually hit with a remote endpoint.

None of this needs `.env` at all if you would rather not touch it: the same four settings are
editable from the plugin, which writes them through `PUT /v1/config`. The backend reconfigures its
running client, persists the change to the `vox-state` volume so it survives a restart, and
re-probes the endpoint before answering. `.env` stays the boot default and the stored setting wins
over it; delete the volume to fall back.

`PUT /v1/config` is unauthenticated and safe only because the backend binds to loopback - anything
already running as you could repoint where transcripts go. Never expose that port.

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

**3. Build and install the IntelliJ plugin**

```powershell
cd plugin
$env:JAVA_HOME = "C:\Users\skiri\.jdks\jbr-25.0.2"
.\gradlew.bat buildPlugin --no-daemon
```

Then in the IDE: **Settings | Plugins | gear icon | Install Plugin from Disk...**, pick
`plugin\build\distributions\vox-idea-0.1.0.zip`, and restart. Details, including how to rebind the
shortcut, are in [The IntelliJ plugin](#the-intellij-plugin).

**4. Install the Windows companion (optional)**

Only needed for dictating outside the IDE.

```powershell
.\scripts\install-client.ps1
```

which creates the host virtualenv and installs the client dependencies, equivalent to:

```powershell
uv sync --extra client
```

**5. Client configuration (optional)**

```powershell
Copy-Item config\client.example.yaml config\client.yaml
```

Every value in the example is the default, so you only need this file to change something - a
different microphone, different hotkeys, a longer timeout, or the project roots that make
[`.vox.md`](#project-context---voxmd) work. `config/client.yaml` is gitignored.

---

## Daily use

The plugin needs nothing but the backend:

```powershell
docker compose up -d
```

and the IDE is then ready - `Ctrl+Alt+Shift+D`, speak, `Ctrl+Alt+Shift+D`.

To bring the companion up as well:

```powershell
.\scripts\start.ps1
```

starts the compose stack, waits for `/health` to report `ready`, and launches the companion.
By hand that is:

```powershell
docker compose up -d
uv run vox-client
```

To shut everything down:

```powershell
.\scripts\stop.ps1
```

The companion adds a notification-area icon with **Status**, **Open config folder** and **Quit**.
It has no window of its own - only the small overlay that appears while you speak. If the tray
icon cannot be created the app logs it and keeps working.

---

## The IntelliJ plugin

The primary client. It records the microphone, posts to the backend, reads `.vox.md` from the open
project, and types the result into the terminal tab you are looking at. It always calls
`POST /v1/process` and runs the task prompt - there is no dictation-prompt trigger in the IDE; use
the companion's `dictate` hotkey for that (see [The prompts](#the-prompts)). Built and tested
against IntelliJ IDEA Ultimate 2026.2.2 (build IU-262.10315.125).

### Building

`java` on PATH here is JDK 1.8, far too old for both Gradle and the IntelliJ Platform. The build
must run on a JetBrains Runtime:

```powershell
cd plugin
$env:JAVA_HOME = "C:\Users\skiri\.jdks\jbr-25.0.2"
.\gradlew.bat buildPlugin --no-daemon
```

The JBR bundled with the IDE works just as well - point `JAVA_HOME` at
`C:\Users\skiri\AppData\Local\Programs\IntelliJ IDEA Ultimate\jbr` instead. From Git Bash the same
build is:

```bash
cd plugin
JAVA_HOME=/c/Users/skiri/.jdks/jbr-25.0.2 ./gradlew buildPlugin --no-daemon
```

The artefact is `plugin\build\distributions\vox-idea-0.1.0.zip`.

By default the build compiles against the IDE installed on this machine - `platformLocalPath` in
`plugin/gradle.properties` - which is faster than downloading a second SDK and is an exact match for
the API the plugin will run on. Blank that property to download `platformVersion` from JetBrains
instead. `.\gradlew.bat runIde` starts a sandbox IDE with the plugin loaded, which is how to try a
change without touching your real one.

### Installing

**Settings | Plugins | gear icon | Install Plugin from Disk...**, pick the zip, restart the IDE.
The plugin requires build 262 or newer and declares no upper bound.

### The shortcut

`Ctrl+Alt+Shift+D` starts a dictation and stops it; `Ctrl+Alt+Shift+X` throws a running one away.
Both are ordinary IDE actions, so **Settings | Keymap**, search for `Vox`, right-click, *Add
Keyboard Shortcut* rebinds them like anything else.

`Ctrl+Alt+Shift+V` would have been the obvious binding and is deliberately not used: the default
keymap already gives it to `EditorPasteSimple` ("Paste as Plain Text"). Nothing stops you from
moving Vox onto it in the keymap and dropping the paste binding.

### Settings

**Settings | Tools | Vox**, stored per application in `vox.xml`:

| Setting | Default | Meaning |
|---|---|---|
| Backend URL | `http://127.0.0.1:8765` | Root of the vox backend. |
| Request timeout | 180 s | The whole `/v1/process` round trip; the connect timeout is fixed at 3 s. |
| Max recording | 120 s | The recording stops itself and is sent at the cap. |
| Max `.vox.md` size | 8000 bytes | Larger files are truncated on a line boundary; `0` switches the project file off. |
| Type the result into the terminal | on | Off means always use the clipboard. |

**Test Connection** calls `GET /health` and reports the backend status, the speech model and its
device, and the language model - the quickest way to tell a wrong URL from a cold Whisper.

### Delivery into the terminal

The result is typed into the selected tab of the Terminal tool window as if you had typed it.
**No newline is ever sent and nothing is executed** - you read the command and press Enter yourself.
Multi-line text goes in as a bracketed paste, so the shell treats it as one block instead of running
the first line the moment it arrives.

If no terminal tab is open, if the Terminal plugin is disabled, if the terminal API is not the one
Vox expects, or if the setting is off, the text goes to the clipboard and a notification says so.
Both terminal engines are handled: the reworked one (the 2026.2 default) through the `TerminalView`
data key, the classic one by writing to the `TtyConnector` of its `JBTerminalWidget`.

### Project context

`<project root>/.vox.md`, when it exists, goes out with every request, truncated to the configured
budget; the project name goes with it and the backend uses that for logging only. No configuration
is needed and no other file is read - see [Project context](#project-context---voxmd).

### Audio

Mono 16-bit signed little-endian PCM from the default input device, at the first rate the mixer
accepts out of 16 kHz, 48 kHz and 44.1 kHz, wrapped in a RIFF/WAVE file that only ever exists in
memory. Nothing is written to disk.

---

## Hotkeys

### In the IDE - the plugin

| Combo | Action | What it does |
|---|---|---|
| `Ctrl+Alt+Shift+D` | Vox: Dictate | First press starts recording, second press stops it and sends it. |
| `Ctrl+Alt+Shift+X` | Vox: Cancel Dictation | Throws the running recording away. |

A toggle, not push-to-talk: an IDE action fires on key press and is never told the key went up, so
there is no "release to send". Both actions also sit in the **Tools** menu, and the status bar
widget (`Vox`, `Vox ● REC`, `Vox …`) shows the state and starts a dictation when clicked.

### Everywhere else - the companion

| Combo | Action | What you get |
|---|---|---|
| `Ctrl+Alt+Space` | record | Push-to-talk: hold to capture, release to send. Runs the task prompt. |
| `Ctrl+Alt+D` | dictate | Push-to-talk: hold to capture, release to send. Runs the dictation prompt - punctuation and filler removal, nothing reformulated. |
| `Esc` | cancel | While recording: drops the audio, sends nothing. |

Hold the combo while you speak; release it when you are done. Releasing the **trigger** key
(`Space` for `record`, `D` for `dictate`) is what ends the recording - releasing `Ctrl` or `Alt`
first does not, in any order. Keyboard auto-repeat is ignored, so recording starts exactly once per
press.

The modifier set must match exactly: `Ctrl+Alt+Shift+Space` is not `Ctrl+Alt+Space` and does
nothing. Pressing either trigger again while a recording or a request is in flight is ignored -
there is never more than one recording at a time, whichever combo started it. `record` and `dictate`
must be different combos - a config where they match is rejected at startup with
`hotkeys.dictate: must differ from hotkeys.record, or it never fires`.

**Enter is never pressed automatically**, by either client. Recognition is good, not perfect, and a
prompt you have not read is a prompt you did not write, so the text lands in the terminal and waits.
The plugin has no switch for this at all: it never sends a newline. The companion's
`paste.auto_submit` defaults to `false`; setting it to `true` in `config/client.yaml` makes it press
Enter right after the paste, if you insist.

**Why not `Ctrl+Space`?** IntelliJ binds it to basic code completion and consumes it before
anything else sees it, so a `Ctrl+Space` hotkey would either never fire or fight the IDE.
`Ctrl+Alt+<key>` combos stay clear of the IntelliJ terminal. All of it is configurable - see
[Changing hotkeys](#changing-hotkeys).

While the companion works the overlay shows, in order: `Recording 00:04` (or `Dictating 00:07` for
the `dictate` combo) -> `Transcribing...` -> `Formatting...` -> `Ready`. It never takes focus: the
window carries
`WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW`, so your caret stays where it was.

---

## The prompts

The backend runs one of two prompts, chosen by the endpoint the caller uses: `prompt.md` in the
repository root formalises the transcript into a task, and `dictation.md` beside it only cleans the
transcript up without changing what it says. A third, `analysis.md`, runs after either of them and
writes no message at all - it reports how the speech was read. Both are a markdown file with a `## SYSTEM` section and
a `## USER` section, no front matter, and both take the same placeholders, `{project}`, `{glossary}`
and `{transcript}`.

### The task prompt - `prompt.md`

`POST /v1/process` runs this one, at `LLM_TEMPERATURE` (default `0.1`). It turns the ASR transcript
into a formalised engineering task, in Russian - one to four sentences saying what to do, and a
bullet list only when you actually enumerated several distinct concrete things. There are no
headings and no sections: uncertainty you spoke stays inside the sentences that carry it rather than
being collected into a block at the end, which is the one shape a small local model reliably
fabricates.

Not every utterance is a task. Dictate a note about what you just did and you get that note, put in
order - the prompt does not promote a report into an instruction or append a conclusion to it.

```
spoken:  посмотри этот сервис тут мембершип почему-то второй раз достается проверь реально ли
         он нужен если нет убери только без новых слоев

pasted:  Посмотри этот сервис; тут membership почему-то второй раз достается. Проверь,
         действительно ли он нужен; если нет, убери только без новых слоев.
```

Note what survived: `мембершип` came back as the identifier `membership` (the glossary did that),
"посмотри" and "проверь" stayed checks instead of becoming orders, the conditional stayed a
conditional, and the "без новых слоёв" constraint kept the thing it qualified.

It guarantees:

- **Nothing invented.** No requirement, file name, acceptance criterion, test or migration that was
  not in the speech, and no characteristic attached to a thing that was not named - "индекс" stays
  "индекс", never "составной индекс" or "уникальный индекс".
- **The verb stays the verb.** "посмотри", "проверь", "глянь" never turn into "сделай" or
  "реализуй".
- **Conditions stay conditions, uncertainty stays uncertain.** "если ... то" is not turned into an
  order; "может быть" and "проверь, реально ли" are not turned into a statement of fact.
- **Negation, scope and constraints survive** attached to whatever they qualified in the speech:
  "только в этом модуле", "не трогая тесты", "без новых слоёв".
- **No unsolicited design.** No new layers, patterns, abstractions, interfaces, factories or
  refactors that were not asked for.
- **Identifiers and English engineering terms are never translated** - `compound`, `lookup`,
  `index`, `migration`, `repository`, `endpoint`, and class, method, file, table and column names
  stay exactly as said.
- **A self-correction erases what it corrected.** Say "хотя нет, подожди, не надо, оставь как есть"
  and only the final decision reaches the output - the retracted idea never comes back as a "но",
  a "чтобы" or a future plan.
- **A misheard word is fixed only when the fix is unambiguous** from context and the glossary;
  otherwise it is left exactly as the recogniser produced it rather than guessed at.
- Filler words and repeats are stripped; the answer is Russian throughout, with identifiers and
  technical terms as the only exception.

### The dictation prompt - `dictation.md`

`POST /v1/dictate` runs this one instead, at `LLM_DICTATION_TEMPERATURE` (default `0.0`, sampling as
greedily as the model allows - reproducing speech wants the least variance, not the most natural
phrasing). Its job is the opposite of the task prompt's: punctuation, capitalisation, sentence
breaks, filler removal and obvious ASR repairs, with broken grammatical agreement corrected - and
nothing reformulated, reordered, added or dropped. This is the prompt for notes, chat messages and
commit descriptions: anywhere you want your own words back, only cleaned up, rather than turned into
a task.

```
spoken:  ну э короче я думаю что э надо вот это вот проверить в общем

pasted:  Я думаю, что это надо проверить.
```

The filler is gone and the sentence is capitalised and punctuated, but it is the same sentence in
the same order - nothing here was reworded, split, merged or promoted into an instruction.

It guarantees:

- **Nothing reformulated, reordered, merged or dropped.** Sentence order is speech order; even a
  fragmented or unclear phrase stays in the answer, cleaned up rather than treated as noise.
- **Filler, stutters and false starts are removed** - "ну", "короче", "как бы", "это самое", a
  started-and-abandoned word, a word repeated twice in a row - deleted rather than folded into a
  parenthetical.
- **Broken agreement is fixed** - case, gender, number - without changing the words that were used.
- **Nothing is invented and nothing becomes a task.** "посмотри" or "проверь" never turns into
  "измени" or "сделай", and a note never grows a conclusion, requirement or next step it was not
  given.
- **Uncertainty and self-corrections behave exactly as in the task prompt** - hedges stay in, and a
  retracted statement is dropped and never resurfaces.
- **Identifiers, numbers and English terms are left exactly as said**; the glossary changes spelling
  only, never content.

### The analysis pass - `analysis.md`

Every request that produced a message is then sent back to the model a second time, with the
transcript and the finished message, and asked one question: how was that speech read? The answer
is a JSON object constrained by a schema (`response_format`), and it reaches you as `analysis` on
the response and as the `analysis` block of [a trace](#tracing-a-request):

```json
{
  "action": "прогони",
  "target": "эти изменения",
  "constraints": ["перед тем как я закоммичу"],
  "uncertainty": [],
  "claude_code": { "tool": "subagent", "why": "прогони проверку" }
}
```

It is a second call rather than extra fields on the rewrite, and that is not an accident: folding
the reporting into the one call measurably cost the rewrite itself, with English requests coming
back in Russian on a third of the evaluation set. Asking twice keeps the message exactly as good as
it was and puts the whole cost in latency - roughly 700-900 ms on this machine, about double the
LLM half of a request.

**Nothing about it can fail your request.** No analysis prompt configured, an endpoint that rejects
`response_format` even on the retry, a timeout, a reply that will not parse - each of those ends
with `analysis: null` and the message you would have got anyway.

`claude_code` is the one field that changes the message: when the pass names a tool, a single line
is appended to what you are about to send Claude Code - "Use a subagent for this.", "Review this
rather than change it.", "Plan this first, do not edit yet." The line is written by the server, not
the model, so the same decision always reads the same way; the model only picks which of the four
values applies, and `"none"` - ordinary work - is by far the most common. Dictation never takes a
line, because dictation hands back the speaker's own words and an instruction there was never
spoken. How often the choice is right is measured by `tests/eval/tools.yaml`, which is deliberately
weighted towards `"none"`: naming a tool nobody asked for puts an instruction in front of Claude
Code, while missing one costs a convenience.

### Which prompt runs

Which prompt a request gets is decided entirely by the endpoint the caller uses - there is no "mode"
field in any request body. The companion picks with its two hotkeys, `record` for the task prompt
and `dictate` for the dictation one (see [Hotkeys](#hotkeys)); `POST /v1/transform`'s `dictation`
field picks it when replaying text without a microphone (see [API](#api)). **The IntelliJ plugin has
only one trigger and always calls `/v1/process`: there is no dictation prompt inside the IDE.**

### When the model gives back nothing usable

The LLM is always called, for either prompt. When it produces nothing usable, the request no longer
fails: the raw transcript is returned instead, a WARNING is logged, and the trace records
`output.fell_back_to_transcript: true` (see [Tracing a request](#tracing-a-request)) - losing what
someone just said costs more than handing back an unpolished transcript.

### Format and editing

Placeholders are literal text replacement (`str.replace`, never `str.format`), so other braces in
the prompt text are safe. `{project}` belongs in `## SYSTEM`: it is replaced with the caller's
`.vox.md` wrapped in a delimited context block, or with nothing at all when there is none, so no
empty heading is left behind - see [Project context](#project-context---voxmd). `{transcript}` and
`{glossary}` belong in `## USER`, with the transcript at the end - wrapped in a `<transcript>`
tag and followed by nothing but a fixed line of instruction - for the reason given in that same
section. A
missing `## USER` section falls back to the transcript alone as the user message; a missing
`## SYSTEM` section is a startup error - the server has nothing to serve without one. Both prompt
files are parsed and rendered the same way.

Edit `prompt.md` or `dictation.md` directly. **No code change is needed** - `vox-server` reads each
file once at startup (`--prompt-path` / `PROMPT_PATH`, default `prompt.md`; `--dictation-prompt-path`
/ `DICTATION_PROMPT_PATH`, default `dictation.md`; `--analysis-prompt-path` /
`ANALYSIS_PROMPT_PATH`, default `analysis.md`). A native run just needs a restart; in Docker both
files are baked into the image exactly like the glossary, so editing either needs a rebuild:

```powershell
docker compose up -d --build backend
```

`POST /v1/transform` replays either prompt against text you already have, without dictating
anything - set `"dictation": true` in the body to try the dictation prompt instead of the task one.
Handy for that edit-rebuild-try loop. See [API](#api).

---

## Project context - `.vox.md`

A project may keep a `.vox.md` in its root. **It belongs to the project you are talking about, not
to this repository**:

```
C:\Users\you\IdeaProjects\jigward\.vox.md
```

The client reads that one file and sends its text with the request. Nothing else is ever opened, and
the backend still never touches your repository. The text goes into the LLM's **system** prompt,
ahead of everything that changes per request.

### Why the system prompt, and why it must stay short

Two measurements on this machine, with `qwen2.5:7b-instruct` on Ollama, decide the whole design:

* A 455-token system prompt costs **87 ms on the first call and 24-27 ms on every call after it**,
  even when the user message is different every time. Ollama caches the prompt *prefix*, so context
  that sits in front of the transcript is reused instead of recomputed. Project context in the
  system prompt is, per request, practically free.
* Generated output costs about **8 ms per token**. That is the real bill.

Which is also why the transcript sits at the **end** of the user message: put project context after
it and every request changes the prefix, the cache misses, and you pay a full prefill instead of
25 ms. The only thing that follows the transcript is a fixed line telling the model to rewrite it
rather than answer it - constant text after a variable transcript costs a few tokens of prefill,
not the prefix.

So the price of `.vox.md` is not reading it - it is what it does to the model. More context makes
the model chattier, and a chattier model is both slower and less precise: a longer answer has more
room to invent a requirement you never spoke.

That gives one rule. **Write terms, constraints and two or three worked examples. Do not write prose
about your architecture.** A paragraph explaining that the project is hexagonal with ports and
adapters buys nothing and is paid for in every generated token afterwards; one line saying "не
предлагай новые слои и абстракции" changes the output. Names the recogniser mangles, rules you
would otherwise repeat in every prompt, and a spoken-to-written example pair earn their tokens.
Nothing else does.

The client truncates the file to its byte budget on a line boundary before sending, and the backend
truncates again at `max_project_bytes` (8000, `MAX_PROJECT_BYTES`) and logs a warning naming the
byte count - an oversized file is never a failed request, just a silently shorter one. Treat 8000
bytes as a ceiling, not a target: a good `.vox.md` is well under one screen.

### A complete example

```markdown
# Jigward

Spring Boot 3 + PostgreSQL. Новый код на Kotlin, legacy-модули на Java.

## Термины
- Jigward — название проекта. Не «джигвард», не «Джиг-Вард».
- membership — строка в таблице `user_memberships`. Не «подписка».
- jig — единица работы планировщика; во множественном числе «jigs».
- FSM — автомат статусов заказа в `OrderStateMachine`.

## Ограничения
- Не предлагай новые слои, порты, адаптеры, фабрики и рефакторинги.
- Миграции только Flyway: `db/migration/V<N>__<name>.sql`.
- Тесты — JUnit 5 + Testcontainers, репозитории не мокаем.

## Примеры
- сказано: «тут мембершип второй раз достаётся»
  нужно:   «В текущем сервисе membership загружается второй раз.»
- сказано: «добавь миграцию на индекс по юзер айди и статусу»
  нужно:   «Добавь Flyway-миграцию с составным индексом по (user_id, status).»
- сказано: «проверь реально ли нужен этот джиг»
  нужно:   «Проверь, нужен ли этот jig.» — вопрос, а не приказ.
```

That is under 1500 bytes - a fifth of the budget - and it is already most of the value. The examples
are the part people skip and should not: they teach the model the *shape* of a correct answer, which
no amount of description does.

This repository keeps its own `.vox.md` at its root, in Russian, as a real worked example of the
same shape - what the project is, its module names, its HTTP contract and the constraints it must
not violate.

### Turning it on

The **plugin** needs no configuration. It reads `.vox.md` from the root of the open project, sends
it, and sends the project name alongside for the backend's logs. Set *Max `.vox.md` size* to `0` in
**Settings | Tools | Vox** to switch the whole thing off.

The **companion** has no IDE to ask, so it works it out from the title of the window that was
focused when you pressed the hotkey, against a list you give it in `config/client.yaml`:

```yaml
project:
  roots:
    - "C:/Users/you/IdeaProjects/jigward"
    - "C:/Users/you/IdeaProjects/vox"
  file_name: ".vox.md"
  detect_from_window: true
  max_bytes: 8000
```

A root matches when its folder name occurs in the window title, case-insensitively, which is how
IntelliJ and most editors title their windows; the longest matching folder name wins, so a root
named `vox` cannot shadow one named `vox-client`. With `roots: []` - the default - or
`detect_from_window: false`, no project context is ever sent.

Nothing here can cost you a request: a missing file, an unreadable one, an empty one or no match at
all simply means no project context, and the request goes out without it.

### What the backend does with it

`Prompt.render_system(project)` replaces the literal `{project}` placeholder in the `## SYSTEM`
section of whichever prompt file the request uses. An empty value replaces it with nothing at all,
so no dangling header is left behind; a non-empty one is wrapped in a delimited block that tells the
model these are the repository's terms and constraints and that it must not add anything from them
that was not asked for.

Project text is never logged at `INFO`. The backend logs only the project name and the byte count;
the text itself appears at `DEBUG`, and only with `LOG_TEXT=true`. The one thing that puts it on
disk is [tracing](#tracing-a-request), which is off unless you switch it on.

---

## Customising

### Changing hotkeys

Edit `hotkeys` in `config/client.yaml`:

```yaml
hotkeys:
  record: "ctrl+alt+space"
  dictate: "ctrl+alt+d"
  cancel: "esc"
```

A binding is `mod+mod+key`: modifiers are `ctrl`, `alt`, `shift`, `win` (aliases `control`,
`option`, `cmd`/`super`/`meta`/`windows` are accepted), and exactly one trigger key -
`space`, `esc`, `a`-`z`, `0`-`9`, `f1`-`f12`. `record` and `dictate` must parse to different combos,
or the companion refuses to start with `hotkeys.dictate: must differ from hotkeys.record, or it
never fires`. Restart the companion to apply. An unparseable combo, or one whose trigger key the
listener never reports, makes the companion exit with `config error: ...` instead of starting
half-configured.

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

### Choosing a different microphone

```powershell
uv run vox-client --list-devices
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

## The backend command line

`vox-server` takes options for everything worth changing at launch. The precedence is
**command line > environment (including `.env`) > built-in default**: an option you do not pass is
not an override, so the container keeps working on environment variables alone.

| Option | Environment variable | Default |
|---|---|---|
| `--host` | `VOICE_CODE_HOST` | `0.0.0.0` |
| `--port` | `VOICE_CODE_PORT` | `8765` |
| `--stt-model` | `STT_MODEL` | `turbo` |
| `--stt-language` | `STT_LANGUAGE` | `ru` |
| `--stt-device` | `STT_DEVICE` | `auto` (`auto`, `cuda`, `cpu`) |
| `--stt-compute-type` | `STT_COMPUTE_TYPE` | `float16` |
| `--stt-beam-size` | `STT_BEAM_SIZE` | `1` |
| `--llm-base-url` | `LLM_BASE_URL` | `http://host.docker.internal:11434/v1` |
| `--llm-model` | `LLM_MODEL` | `qwen2.5:7b-instruct` |
| `--llm-timeout-seconds` | `LLM_TIMEOUT_SECONDS` | `120.0` |
| `--llm-temperature` | `LLM_TEMPERATURE` | `0.1` |
| `--llm-max-tokens` | `LLM_MAX_TOKENS` | `1024` |
| `--prompt-path` | `PROMPT_PATH` | `prompt.md` |
| `--dictation-prompt-path` | `DICTATION_PROMPT_PATH` | `dictation.md` |
| `--analysis-prompt-path` | `ANALYSIS_PROMPT_PATH` | `analysis.md` |
| `--glossary-path` | `GLOSSARY_PATH` | `config/glossary.yaml` |
| `--processing-concurrency` | `PROCESSING_CONCURRENCY` | `1` |
| `--log-level` | `LOG_LEVEL` | `INFO` |
| `--log-text` / `--no-log-text` | `LOG_TEXT` | off |
| `--trace-dir` | `VOX_TRACE_DIR` | unset - [tracing](#tracing-a-request) is off |
| `--trace-keep` | `VOX_TRACE_KEEP` | `200` |

`--version` prints the version and exits, and `--help` lists the same options. The remaining
settings stay environment-only, because they are set once and never per launch: `STT_VAD_FILTER`,
`STT_GLOSSARY_HOTWORDS`, `LLM_API_KEY`, `LLM_DICTATION_TEMPERATURE` (`0.0`, the sampling temperature
the dictation prompt uses instead of `LLM_TEMPERATURE` - see [The prompts](#the-prompts)),
`MAX_AUDIO_BYTES`, `MAX_AUDIO_SECONDS`, `MAX_PROJECT_BYTES` (8000, the ceiling on project context
- see [Project context](#project-context---voxmd)), `MAX_CONTEXT_BYTES` (6000, the ceiling on the
conversation context a client may attach) and `DEFAULT_LANGUAGE` (`en`, the language answers are
written in when a request asks for no particular one).

### Running the backend natively

Handy for trying a different model or a smaller Whisper without editing `.env` and recreating the
container:

```powershell
uv sync --extra server
uv run vox-server --llm-base-url http://127.0.0.1:11434/v1 --stt-model small --log-level DEBUG
```

Note `127.0.0.1` rather than `host.docker.internal`: outside a container the model server is just
localhost. Paths resolve against the working directory, so run this from the repo root or pass
`--prompt-path`, `--dictation-prompt-path`, `--analysis-prompt-path` and `--glossary-path` as well. A native run has to supply
its own CUDA and cuDNN runtime for CTranslate2 - which is precisely what the container exists to
avoid - so on Windows expect `stt.device` to come up `cpu` unless you have already installed them.

The image runs `python -m vox_server.main` with no arguments, so the container is configured
entirely through `.env`; to pass a flag there instead, add a `command:` to the backend service in
`compose.yaml`.

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

## Tracing a request

The one log line per request says how long each stage took and nothing about what was in them. When
the text that comes back is wrong, that is not enough. Tracing fills it in after the fact: with it
on, the backend writes one JSON file per request holding the whole provenance of that request -
what Whisper heard, which `.vox.md` arrived and how much of it survived, the exact system and user
prompts that were assembled, what the model returned raw, whether the output cleanup changed it,
the final text, and the timings of each stage. Dictate once, open the trace, and read what happened
instead of guessing at it.

**It is off by default, and it stays off until you ask for it.** This is the only part of vox that
puts transcripts and prompts on disk - everything else holds them in memory and drops them (see
[Privacy](#privacy)). A trace file contains what you dictated, verbatim, along with your project
context, the whole prompt and the model's answer. Switch it on while you are chasing something,
then switch it back off and delete the files.

### Switching it on

Add to `.env` in the repo root:

```dotenv
VOX_TRACE_DIR=/traces
VOX_TRACE_KEEP=200
```

and restart the backend so it picks them up:

```powershell
docker compose up -d
```

`compose.yaml` already mounts the repository's own `traces` directory into the container at
`/traces`, so the files land in `<repo>\traces\` on Windows, and `/traces/` is gitignored. Tracing
is on exactly when `VOX_TRACE_DIR` is set - there is no second switch - and the startup log says so
out loud, in one line:

```
request tracing is ON: the newest 200 traces go to /traces, each holding the transcript, the prompts and the model output
```

With it off, nothing about tracing is logged at all. A [native run](#running-the-backend-natively)
takes `--trace-dir` and `--trace-keep` instead of the environment variables.

To switch it off again, delete the `VOX_TRACE_DIR` line from `.env` (or comment it out), restart,
and remove what was already written:

```powershell
docker compose up -d
Remove-Item -Recurse -Force .\traces
```

Switching tracing off stops new files; it does not delete the ones already on disk. That second
command is the part people forget.

### Reading one

```powershell
.\scripts\show-trace.ps1                              # the newest request, long text cut short
.\scripts\show-trace.ps1 -Full                        # the same, every prompt and transcript in full
.\scripts\show-trace.ps1 -List                        # a table of the last 20 requests
.\scripts\show-trace.ps1 -Last 5                      # the last five reports (with -List, five rows)
.\scripts\show-trace.ps1 -RequestId 3f9a1c07 -Full    # one request; a unique prefix is enough
```

`-List` prints time, request id, project, status and total ms - enough to find the request you
mean. The id is also in the backend log line for that request and in the response's `X-Request-ID`
header. `-TraceDir` points the script at a directory other than `traces`. With nothing to show it
prints how to switch tracing on and exits 0; an unknown request id exits 1. A file that is not
readable JSON is skipped with a warning rather than killing the run.

The files are plain JSON, written UTF-8 without escapes and indented, so opening one in an editor
works just as well - Russian reads as Russian.

### What is in a trace

The name is the UTC timestamp followed by the request id, so sorting by name sorts by time:

```
traces\20260912-142233-518-3f9a1c07b2d4.json
```

Trimmed, with the long text cut - a real file carries all of it:

```json
{
  "request_id": "3f9a1c07b2d4",
  "started_at": "2026-09-12T14:22:33.518291+00:00",
  "endpoint": "process",
  "status": "ok",
  "error": null,
  "client": { "id": "vox-idea", "version": "0.1.0", "audio_seconds": 6.4 },
  "audio": { "bytes": 205856, "decoded_seconds": 6.43 },
  "stt": {
    "model": "turbo", "device": "cuda", "compute_type": "float16",
    "language": "ru", "language_probability": 1.0, "duration_ms": 374,
    "transcript": "посмотри этот сервис тут мембершип почему-то второй раз достается"
  },
  "project": {
    "name": "Jigward", "received_bytes": 1840, "used_bytes": 1840,
    "truncated": false, "text": "# Jigward\n\n## Термины\n..."
  },
  "glossary": { "entries": 71, "prompt_block_chars": 1786 },
  "prompt": {
    "kind": "task",
    "system_chars": 2274, "user_chars": 1902,
    "system": "Ты редактор ... ## Контекст проекта\n# Jigward\n...",
    "user": "## Словарь\n... ## Текст\nпосмотри этот сервис тут мембершип ..."
  },
  "llm": {
    "model": "qwen2.5:7b-instruct",
    "base_url": "http://host.docker.internal:11434/v1",
    "temperature": 0.1, "duration_ms": 902,
    "raw_output": "```\nПроверь, зачем в текущем сервисе membership загружается второй раз.\n```",
    "cleaned_output": "Проверь, зачем в текущем сервисе membership загружается второй раз.",
    "cleanup_changed": true
  },
  "output": {
    "chars": 67,
    "fell_back_to_transcript": false,
    "text": "Проверь, зачем в текущем сервисе membership загружается второй раз."
  },
  "timings_ms": { "transcription": 380, "llm": 910, "total": 1298 }
}
```

| What you want to know | Where to look |
|---|---|
| Did Whisper mishear me? | `stt.transcript` - the exact text that went on to the model, before the prompt touched it. `stt.language` with `language_probability`, and `audio.decoded_seconds` against `client.audio_seconds`, say whether it heard the right language and the whole recording. |
| Did my `.vox.md` get picked up? | `project.name` and `project.used_bytes`. A `null` name means the request carried no project context at all - the client decided that, and its own log says why. `truncated: true` means the file was over `MAX_PROJECT_BYTES` (8000) and the tail was dropped. `project.text` is the context the model actually saw, not the file on disk. |
| Did the cleanup eat my text? | `llm.cleanup_changed`. When it is `true`, read `llm.raw_output` against `llm.cleaned_output`: the cleanup strips code fences, quoting and model preamble, and this is where you see it taking a bite it should not have. |
| Why did the model answer *that*? | `prompt.system` and `prompt.user`, verbatim and complete - the prompt's instructions plus the project context in the system prompt, the glossary and your transcript in the user message, the transcript last inside its `<transcript>` tag. `llm.model` and `llm.temperature` say who answered and how loosely. |
| Which prompt ran, task or dictation? | `prompt.kind`. `endpoint` cannot tell you: it names the pipeline stage, not the HTTP path, so both `/v1/process` and `/v1/dictate` write `endpoint: "process"`. |
| Why was it slow? | `timings_ms` - `transcription` against `llm` says which half to blame. A slow first half with `stt.device: "cpu"` is the CUDA fallback; a slow `llm` half is usually a model too big for the GPU (see the VRAM note above). `stt.duration_ms` and `llm.duration_ms` are the stages themselves, the `timings_ms` pair the wall clock around them. |
| What was actually delivered? | `output.text` - what the plugin typed or the companion pasted, identical to `llm.cleaned_output` unless `output.fell_back_to_transcript` is `true`, in which case it is the raw transcript instead. |
| Where did the message diverge from what I said? | `analysis.fields` - `action` and `target` are the verb the model thought it heard and what it aimed it at, `constraints` the limits it kept, `uncertainty` what it read as hedged. A constraint you spoke that is missing from the list is the fastest way to see the rewrite dropped it. |
| Why did a "use a subagent" line appear? | `analysis.fields.claude_code` - `tool` is what the second pass chose and `why` quotes the words that decided it. `parsed: false` with a filled `raw_output` means the pass ran and could not be read, so no line was appended at all. |
| It failed - on what? | `status` is `"error"`, and `error.type` / `error.message` name the exception. Failed requests are traced too, with every stage that completed before the failure filled in, which is usually the point. |

Sections that do not apply to a request are `null` rather than empty: `audio` and `stt` for
`/v1/transform`, `project` for `/v1/transcribe`, and `prompt`, `glossary` and `llm` for anything
that never reached the model. `LLM_API_KEY` never appears in a trace, and `llm.base_url` has any
`user:password@` stripped out of it.

### Retention

`VOX_TRACE_KEEP` (200 by default) is how many files are kept. After each write the directory is
pruned to the newest that many, so it cannot grow without bound; at least one file is always kept,
whatever you set. Nothing rotates and nothing is compressed - pruning only deletes the oldest.

A trace that cannot be written never costs you a request: the backend logs one WARNING, the request
finishes normally, and its log line ends with `trace=-`. Files appear atomically, so a trace you
open is always a complete one.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Overlay says `Service unavailable` | The backend is not reachable on `127.0.0.1:8765`. The companion retries once automatically and then discards the audio. Check `docker compose ps`, then `docker compose logs backend`. Note the port is published as `127.0.0.1:8765:8765` - loopback only, by design. |
| Overlay says `Backend warming up`, or requests answer `warming` | Whisper is still loading; the first start also downloads the weights. Watch `docker compose logs -f backend` until `stt ready:`. `/health` always returns HTTP 200 - read `status` from the body. |
| `Backend timeout` | The request exceeded the client's budget - `server.timeout_seconds` (180 s) for the companion, *Request timeout* (180 s) for the plugin - or the backend's `LLM_TIMEOUT_SECONDS` (120 s). Usually a model too large for the GPU, so it is running partly on the CPU. See the VRAM note above. |
| `stt.device` is `cpu` | CUDA was not usable at load time, so it fell back (and `float16` downgraded to `int8`). Re-run the `docker run --gpus all ... nvidia-smi` check, restart Docker Desktop, and make sure the backend image is the `-cudnn-` CUDA flavour - CTranslate2 4.8.2 needs cuDNN 9 at runtime, which the plain runtime image does not ship. |
| `llm.ready` is `false` | The container cannot reach the model server. For a Windows-side Ollama, `OLLAMA_HOST=0.0.0.0` must be set *and Ollama restarted*; `LLM_BASE_URL` must use `host.docker.internal`, not `127.0.0.1` (inside the container that is the container itself). Test with the `urllib` one-liner above, and check the Windows firewall. |
| `Ctrl+Alt+Shift+D` does nothing in the IDE | (1) Something else owns the combo - **Settings -> Keymap**, search for `Vox`, and rebind whichever action loses. (2) There is no open project: both actions are disabled on the welcome screen. (3) The plugin is not installed or not enabled - check **Settings -> Plugins**, and look for the `Vox` status bar widget. |
| The plugin copies the result instead of typing it | The notification says why: no terminal tab is open, the Terminal plugin is disabled, *Type the result into the terminal* is off in **Settings -> Tools -> Vox**, or the terminal API is not one Vox recognises. Open a terminal tab, or paste with `Ctrl+V`. |
| `.vox.md` seems to be ignored | Plugin: the file must sit in the project root itself, be non-empty, and *Max `.vox.md` size* must not be `0`. Companion: the project's directory must be listed in `project.roots` and its folder name must appear in the title of the window you were in. The companion logs the reason at INFO as `project_status=` in `%LOCALAPPDATA%\vox\client.log`, the plugin at `DEBUG`, and the backend logs `project=<name> project_bytes=<n>` for every request that carried one. To see the text that actually arrived, switch [tracing](#tracing-a-request) on. |
| Hotkey does nothing | (1) The companion is not running - look for the tray icon. (2) Something else owns the combo; try another. (3) **The target app is elevated.** Windows refuses synthetic input from a lower-integrity process, so if IntelliJ runs as Administrator the companion must be elevated too - run it as Administrator, or better, stop running the IDE elevated. (4) A malformed binding: the companion prints `config error:` naming the offending key and exits with code 2. (5) An unsupported trigger key - only the keys listed under [Changing hotkeys](#changing-hotkeys) are recognised. |
| Recording never stops | Only the trigger key's release stops it, and `audio.max_seconds` (120 s) caps it regardless. Press `Esc` to discard. |
| `Microphone unavailable` / `Microphone error` | The device is missing, in use exclusively by another app (Zoom, Teams, OBS), or blocked. Check Settings -> Privacy & security -> Microphone -> "Let desktop apps access your microphone", then `uv run vox-client --list-devices` and pin `audio.input_device`. |
| `No audio captured` | The stream opened but produced no frames - almost always the wrong input device, or a muted mic. |
| Text pasted into the wrong window | Should not happen: with `paste.only_if_target_window_unchanged: true` the companion compares the foreground window against the one recorded when you started speaking and, if focus moved, copies instead of pasting and shows `Copied - focus changed` - press `Ctrl+V` yourself. If you disabled that check, this is why. The companion also waits (up to 1 s) for you to release `Ctrl+Alt` before pasting, so the target receives `Ctrl+V` and not `Ctrl+Alt+V`. |
| Clipboard not restored, or `Clipboard busy` | The Windows clipboard is frequently locked by another process; `set_text` retries a few times. Restore happens `paste.restore_delay_ms` (600 ms) after the paste and is unconditional, so anything you copy inside that short window is overwritten by the restored text; lengthen or disable it if that bites. Failures to save or restore are logged and never fail the request. Set `paste.preserve_clipboard: false` to switch the behaviour off. |
| Hotkeys stop working on a Russian layout | Handled: when a `KeyCode` has no ASCII `char` (as with Cyrillic), the key is derived from the virtual-key code, so `Ctrl+Alt+D` fires on the physical `D`/`В` key in either layout. If it still misbehaves, run with `logging.level: DEBUG` and check which key name the listener reports. |
| Wrong words for English terms | Add the spoken form to `config/glossary.yaml` and rebuild the backend. |
| The text comes back wrong, but nothing errored | Nothing in the log will tell you why - the log never contains the text. Switch [tracing](#tracing-a-request) on, dictate the same thing again, and read the transcript, the prompt and the raw model output with `.\scripts\show-trace.ps1`. |

### Where the logs are

The backend logs one INFO line per successful request, and nothing else per request:

```powershell
docker compose logs -f backend
```

```
request_id=3f9a1c07b2d4 audio_s=6.43 stt_ms=380 llm_ms=910 total_ms=1298 out_chars=67 project=Jigward project_bytes=1840 system_chars=2274 user_chars=1902 trace=20260912-142233-518-3f9a1c07b2d4.json
```

There is no transcript in it, by design. `project=` is the name the client reported and
`project_bytes=` the size of the `.vox.md` it sent (`-` and `0` when none came); `system_chars` and
`user_chars` are the size of the prompt built from them, which is the quickest check that a project
file actually reached the model; and `trace=` names that request's trace file, or `-` when tracing
is off. `LOG_TEXT=true` together with `LOG_LEVEL=DEBUG` adds the transcript and the output to the
log - a debugging switch, not a setting (see [Privacy](#privacy)). For the text itself and the
whole prompt, use [tracing](#tracing-a-request) rather than turning the log up.

The companion logs to `%LOCALAPPDATA%\vox\client.log` (rotated at 1 MB, two old files kept) - it
has no console of its own when `start.ps1` launches it - and prints one INFO line per recording
before it sends:

```
sending prompt=task project=Jigward project_file=C:\src\Jigward\.vox.md project_bytes=1840 truncated=False project_status=ok window=328964
```

`prompt` is `task` or `dictation`, whichever combo was held down. `project_status` is why the
project context is or is not there: `ok`, `off` (detection disabled or no usable window title),
`no-match` (no configured root's folder name occurs in the window title), or `missing` /
`unreadable` / `empty` for a root that matched but whose file could not be used.
`logging.level: DEBUG` in `config/client.yaml` adds the rest. The plugin logs into the IDE's own
`idea.log`.

---

## API

Base URL `http://127.0.0.1:8765`. Every response carries an `X-Request-ID` header. Errors are
always `{"error": "...", "detail": "..."}` - never a stack trace, never the API key.

| Method | Path | Body | Returns |
|---|---|---|---|
| `GET` | `/health` | - | `HealthResponse`. Always HTTP 200; read `status` (`ready` / `warming` / `degraded`). |
| `POST` | `/v1/process` | multipart: `audio` (WAV), optional `project`, `project_name`, `context`, `language`, `audio_seconds`, `client_id`, `client_version` | `ProcessResponse` - the full pipeline, task prompt. |
| `POST` | `/v1/dictate` | Same fields as `/v1/process` | Same shape as `ProcessResponse` - the full pipeline, dictation prompt instead. |
| `POST` | `/v1/transcribe` | multipart: `audio`, optional `language` | `{request_id, transcript, language, timings_ms}` - STT only, no LLM. |
| `POST` | `/v1/transform` | JSON `{"text": "...", "dictation": false, "project": null, "context": null, "language": null}` | `{request_id, output, timings_ms}` - replay a prompt on existing text, no STT. `dictation: true` picks the dictation prompt. Handy for iterating on either prompt. |

Which prompt runs is decided by the path (`/v1/process` vs `/v1/dictate`), or by the `dictation`
field for `/v1/transform` - there is no "mode" field anywhere else in a request body. The two audio
endpoints are otherwise identical, down to the response shape; see [The prompts](#the-prompts).

`project` is the raw text of the caller's [`.vox.md`](#project-context---voxmd), already truncated
by the client; the server truncates again at `max_project_bytes` and warns rather than failing the
request, then splits off its `## SYSTEM` section as extra system instructions. `project_name` is a
short name used for log lines only and never reaches the model. `context` is recent conversation
the caller chose to attach so speech can refer to it, truncated at `max_context_bytes` the same way.
`client_id` identifies the caller in the logs - `vox-windows` for the companion, `vox-idea` for the
plugin.

`language` is the language the **answer** is written in, defaulting to `DEFAULT_LANGUAGE` (`en`). It
is deliberately not the spoken language: speech recognition keeps using `STT_LANGUAGE`, so Russian
speech can produce an English task. It selects the prompt file's `## LANGUAGE <code>` section and
the matching spellings in the glossary. On `/v1/transcribe`, which returns speech as recognised and
never reaches a prompt, `language` means the spoken language instead.

`POST /v1/process`:

```json
{
  "request_id": "3f9a1c07b2d4",
  "transcript": "посмотри этот сервис тут мембершип почему-то второй раз достается",
  "output": "Проверь, зачем в текущем сервисе membership загружается второй раз.",
  "language": "ru",
  "timings_ms": { "transcription": 380, "llm": 910, "total": 1298 },
  "analysis": {
    "action": "посмотри",
    "target": "мембершип в этом сервисе",
    "constraints": [],
    "uncertainty": ["почему-то"],
    "claude_code": { "tool": "none", "why": "" }
  }
}
```

`output` is what the client delivers - typed into the terminal by the plugin, pasted by the
companion. `POST /v1/dictate` returns the identical shape, with `output` punctuated rather than
formalised.

`analysis` is how the second pass read the speech, and it is `null` whenever that pass could not
answer - see [The analysis pass](#the-analysis-pass---analysismd). Its fields quote the speech, so
they stay in the spoken language whatever `language` the message came back in. Nothing in it is
needed to use `output`; it is there to be read when a message came out wrong.

Error codes: `empty_audio` (400), `audio_too_large` (413), `invalid_request` (422),
`empty_transcript` (422), `llm_error` (502), `stt_unavailable` (503), `llm_unavailable` (503),
`warming` (503), `llm_timeout` (504), `internal_error` (500). An empty or unusable model reply is
not one of these any more: the request falls back to the raw transcript instead of failing (see
[The prompts](#the-prompts)), and only a WARNING in the log and `output.fell_back_to_transcript` in
[a trace](#tracing-a-request) say that it happened.

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
  model caches (`hf-cache`, `ct2-cache`, `ollama-models`). `compose.yaml` also mounts `./traces`,
  which stays empty unless you switch tracing on.
* **Your repository is never read by the backend.** The only file either client opens is the
  `.vox.md` you put in the project root, and only its text is sent - never a path, never a listing,
  never any other file.
* **Transcripts, prompts and project text are never logged at INFO**, on either side. The backend
  logs a project file as its name and byte count and nothing more. `LOG_TEXT=true` (backend) and
  `logging.log_text: true` (client) add the text itself to `DEBUG` output; both default to false,
  and they are debugging switches, not settings.
* **Request tracing is the one exception, and you have to ask for it.** With `VOX_TRACE_DIR` set,
  the backend writes one JSON file per request holding the transcript, the project text, the whole
  prompt and the model output - see [Tracing a request](#tracing-a-request). Unset, which is the
  default, nothing is ever written. `LLM_API_KEY` never appears in a trace, and the trace directory
  is gitignored.
* **Clipboard contents** are read only to restore what you had, and only when
  `paste.preserve_clipboard` is on.
* The backend port is published as `127.0.0.1:8765:8765` - loopback only, not reachable from the
  LAN.

---

## Autostart (optional)

This is about the companion only - the plugin starts with the IDE and needs nothing here.

Not a Windows Service - deliberately. A service runs in session 0, where it has no desktop, no
clipboard, no foreground window and no way to send input to your applications; everything the
companion does would break. It must run as a normal process in your interactive session.

Build the exe (see below), then:

```powershell
$startup  = [Environment]::GetFolderPath("Startup")
$shell    = New-Object -ComObject WScript.Shell
$lnk      = $shell.CreateShortcut((Join-Path $startup "vox.lnk"))
$lnk.TargetPath       = (Resolve-Path .\dist\vox.exe).Path
$lnk.WorkingDirectory = (Get-Location).Path
$lnk.Description      = "vox push-to-talk companion"
$lnk.Save()
```

`Win+R` -> `shell:startup` opens that folder to check or remove it. The backend starts itself:
compose services use `restart: unless-stopped`, so they come back with Docker Desktop.

---

## Building the standalone exe

```powershell
.\scripts\build-client.ps1
```

produces `dist\vox.exe` - a single file you can run without a Python install and point the
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
vox/
├─ compose.yaml                  backend service, GPU reservation, optional bundled-model profile
├─ docker/Dockerfile             CUDA 12.8.1 + cuDNN runtime, uv-installed Python 3.14
├─ pyproject.toml                deps (server / client extras), ruff, mypy, pytest config
├─ uv.lock
├─ .env.example                  every backend setting, documented; copy to .env
├─ prompt.md                     the task prompt: SYSTEM + USER, {project}/{glossary}/{transcript}
├─ analysis.md                   the second pass: how the speech was read, as JSON
├─ dictation.md                  the dictation prompt: same format, punctuates instead of formalising
├─ .vox.md                       this repository's own project context, as a worked example
├─ config/
│  ├─ client.example.yaml        every companion setting; copy to client.yaml
│  └─ glossary.yaml              spoken form -> canonical engineering term
├─ scripts/                      install-client.ps1, start.ps1, stop.ps1,
│                               build-client.ps1, smoke-test.ps1, show-trace.ps1
├─ plugin/                       the IntelliJ IDEA plugin (Kotlin, Gradle)
│  ├─ README.md                  build, install, settings, terminal delivery
│  ├─ build.gradle.kts           IntelliJ Platform Gradle plugin, JVM 21 target
│  ├─ gradle.properties          version, since-build, platformLocalPath
│  └─ src/main/
│     ├─ kotlin/dev/vox/idea/    actions, recorder, HTTP client, terminal inserter,
│     │                          settings page, status widget, .vox.md reader
│     └─ resources/              plugin.xml, optional Terminal dependency,
│                                plugin icon + action icons (light/dark)
├─ src/
│  ├─ vox_server/
│  │  ├─ main.py                 create_app(), lifespan, uvicorn entry point
│  │  ├─ api.py                  routes, error mapping, request ids
│  │  ├─ config.py               pydantic-settings, env vars
│  │  ├─ models.py               request/response models
│  │  ├─ transcription.py        device resolution, WAV decoding, faster-whisper
│  │  ├─ prompt.py               prompt file format: parsing and rendering, shared by both prompts
│  │  ├─ glossary.py             glossary loading and prompt rendering
│  │  ├─ llm.py                  OpenAI-compatible client, output cleanup
│  │  ├─ processor.py            STT -> prompt -> LLM pipeline
│  │  ├─ trace.py                opt-in per-request JSON trace (VOX_TRACE_DIR)
│  │  └─ health.py               server state and /health assembly
│  └─ vox_client/
│     ├─ main.py                 wiring, delivery, tray, CLI
│     ├─ state.py                hotkey parsing and the push-to-talk state machine (pure logic)
│     ├─ hotkeys.py              pynput adapter, layout-independent key normalisation
│     ├─ recorder.py             sounddevice capture, in-memory WAV encoding
│     ├─ api_client.py           httpx calls to the backend
│     ├─ clipboard.py            Win32 clipboard, SendInput paste, window handles
│     ├─ overlay.py              tkinter status overlay that never takes focus
│     ├─ project.py              finds the focused project's .vox.md and truncates it
│     └─ config.py               client.yaml loading and validation
└─ tests/
   ├─ conftest.py                fakes and builders shared by the suite
   ├─ unit/                      no GPU, microphone, network or Docker required
   ├─ integration/               marked "integration"; needs a running backend/GPU
   └─ eval/                      marked "eval"; grades real model output
      ├─ cases.yaml              the transcripts, the terms they must recover, known gaps
      ├─ dataset.py              loading and validating the set
      ├─ scoring.py              term matching and the suggested correction (pure logic)
      └─ judge.py                the LLM that grades sense and faithfulness
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

`pytest` is configured with `-m 'not integration and not eval'`, so the default run needs neither a
GPU nor a backend. To run the integration tests against a live stack:

```powershell
uv run pytest -m integration
```

### The evaluation

`tests/eval` grades what the model actually writes, which no assertion over fakes can. Each case in
`tests/eval/cases.yaml` is a transcript with an engineering term mangled the way speech recognition
really mangles it - "промытоты" for "prompts" - and says which canonical English term the finished
message has to carry instead, which terms it must not invent, and what the speaker meant. The set
is replayed through `POST /v1/transform`, so it needs the LLM but not a microphone or Whisper.

Two gradings run over the same answers:

- **Term recovery**, deterministic. The mangled token is gone, every expected term is present, no
  forbidden term appeared. A failure names the word and prints the sentence as it should have read.
- **The judge**, an LLM. Whether each term makes sense where it landed and whether the meaning
  survived - a request still a request, an uncertainty still an uncertainty, nothing added.

Both are gated on a pass rate rather than on every case, because both grade a model. A case that is
known to fail today carries `known_gap` in the dataset and is reported as XFAIL, so a red run means
a regression rather than a gap somebody already wrote down; delete the field when a case starts
passing and pytest will say XPASS until you do.

```powershell
docker compose up -d
$env:VOX_EVAL = "1"
uv run pytest tests\eval -m eval -v
```

| Variable | Default | Meaning |
|---|---|---|
| `VOX_EVAL` | unset | Must be `1`; without it every eval test skips. |
| `VOX_EVAL_URL` | `http://127.0.0.1:8765` | The backend to grade. |
| `VOX_EVAL_MIN_PASS_RATE` | `0.8` | Floor for term recovery. |
| `VOX_EVAL_MIN_JUDGE_PASS_RATE` | `0.7` | Floor for the judge. |
| `VOX_EVAL_LLM_BASE_URL` / `_MODEL` / `_API_KEY` | the backend's own `LLM_*` | Lets a stronger model do the grading. |

The harness itself is covered offline by `tests/unit/test_eval_scoring.py`,
`test_eval_dataset.py` and `test_eval_judge.py`, which run in the default suite: a scorer that
quietly matched fragments, or a parser that crashed on the prose a small model wraps its JSON in,
would turn the whole evaluation into a rubber stamp.

Ruff is set to line length 100 and `target-version = py314`; mypy runs in strict mode over `src`
and `tests`.

The plugin is a separate Gradle build under `plugin/` and none of the commands above touch it; see
[The IntelliJ plugin](#the-intellij-plugin) for its own build line, and remember that it must run on
a JetBrains Runtime rather than the JDK on PATH.

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
