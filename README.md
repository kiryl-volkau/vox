# vox

Push-to-talk voice input for Claude Code on Windows 11. Hold a key, say what you want, let go -
the cleaned-up text appears in the Claude Code prompt in your IntelliJ terminal.

Speech is recognised by Whisper on your GPU and rewritten by a local LLM, both in Docker. Nothing
leaves the machine: no telemetry, no cloud API, and audio is never written to disk.

**Enter is never pressed for you.** You read the text, fix it if you want, and send it yourself.

## What you need

| | |
|---|---|
| Windows 11 | with an NVIDIA GPU and a current driver |
| Docker Desktop | on the WSL2 backend, with GPU passthrough working |
| IntelliJ IDEA 2026.2+ | the plugin is the main way to use this |
| An LLM endpoint | Ollama or LM Studio on the host, or the bundled `bundled-model` compose profile |

One command decides whether any of this can work:

```powershell
docker run --rm --gpus all nvidia/cuda:12.8.1-base-ubuntu24.04 nvidia-smi
```

If that prints your GPU, you are fine. If it does not, fix that first - nothing else will help.

## Run it

```powershell
git clone https://github.com/kiryl-volkau/vox.git
cd vox
Copy-Item .env.example .env       # set LLM_BASE_URL and LLM_MODEL; every other value has a default
docker compose up -d --build
```

The first start downloads the Whisper weights, so give it a couple of minutes. It is ready when
the log says:

```
stt ready: model=turbo device=cuda compute_type=float16
```

Then build and install the plugin - this is the only step that needs a JDK, and it must be a
JetBrains Runtime 21+, not whatever `java` is on your PATH:

```powershell
cd plugin
$env:JAVA_HOME = "C:\Users\<you>\.jdks\jbr-25.0.2"
.\gradlew.bat buildPlugin --no-daemon
```

In the IDE: **Settings | Plugins | ⚙ | Install Plugin from Disk…**, pick
`plugin\build\distributions\vox-idea-0.1.0.zip`, restart.

## Use it

Put the cursor in the terminal tab running Claude Code and press **Ctrl+Alt+Shift+D**. Speak.
Press it again. The text is typed into the terminal for you to check and send. **Ctrl+Alt+Shift+X**
throws a recording away, **Ctrl+Alt+Shift+T** opens the transcripts.

```
you say:   посмотри этот сервис тут мембершип почему-то второй раз достается
you get:   Посмотри этот сервис; тут membership почему-то второй раз достается.
```

For dictating outside the IDE there is a Windows companion with two global hotkeys -
`.\scripts\install-client.ps1`.

## Things worth knowing

- **The answer comes back in the language you spoke.** Say something in Russian, get Russian; in
  English, get English. Set `DEFAULT_LANGUAGE` if you would rather pin one - dictating in Russian
  and getting the task in English is a supported combination.
- **`.vox.md` in your project root is the context file.** Put your project's terms, constraints and
  two or three worked examples in it - that is what stops `мембершип` coming out as anything but
  `membership`. Keep it short; it is sent with every request. The backend never reads anything else
  from your repository.
- **Nothing you say is invented into a task.** The prompt is built to keep what you said and add
  nothing - a hedge stays a hedge, a question stays a question, and an instruction you withdrew
  mid-sentence does not come back.
- **The prompts are plain markdown files** in the repo root: `prompt.md` rewrites speech into a
  task, `dictation.md` only punctuates it, `analysis.md` reports how a request was read. Edit them,
  rebuild the backend, done - no code change needed.
- **Transcripts are kept in the IDE**, not in your repository, so a bad rewrite can be replayed and
  compared without dictating again. The Vox tool window is where they live.

## Everything else

[**docs/reference.md**](docs/reference.md) has the rest, and it is thorough: architecture, the
prompt contract in full, the HTTP API, request tracing, the evaluation suite, GPU verification,
troubleshooting, and how the pieces fit together.

MIT licensed - see [LICENSE](LICENSE).
