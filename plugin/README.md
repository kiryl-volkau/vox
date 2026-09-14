# Vox for IntelliJ IDEA

The primary Vox client: a trigger inside the IDE that records the microphone, sends the audio to the
local Vox backend, and types the rewritten text into the terminal tab you are looking at. The
Windows companion (`vox-client`) stays for dictating outside the IDE.

Built and tested against IntelliJ IDEA Ultimate 2026.2.2 (build IU-262.10315.125).

## Building

`java` on PATH is JDK 1.8 and is far too old for both Gradle and the IntelliJ Platform. The build
must run on the JetBrains Runtime that ships with the IDE:

- `C:\Users\skiri\.jdks\jbr-25.0.2`, or
- `C:\Users\skiri\AppData\Local\Programs\IntelliJ IDEA Ultimate\jbr`

PowerShell:

```powershell
cd plugin
$env:JAVA_HOME = "C:\Users\skiri\.jdks\jbr-25.0.2"
.\gradlew.bat buildPlugin --no-daemon
```

Git Bash:

```bash
cd plugin
JAVA_HOME=/c/Users/skiri/.jdks/jbr-25.0.2 ./gradlew buildPlugin --no-daemon
```

The artefact is `plugin/build/distributions/vox-idea-<version>.zip`.

By default the build compiles against the IDE installed on this machine
(`platformLocalPath` in `gradle.properties`), which is both faster than downloading a second SDK and
an exact match for the API the plugin will run on. Blank that property to download
`platformVersion` from JetBrains instead.

`.\gradlew.bat runIde` starts a sandbox IDE with the plugin loaded.

## Installing

Settings | Plugins | gear icon | Install Plugin from Disk…, pick the zip, restart the IDE.

## Using it

Everything lives in one submenu, **Tools | Vox**:

| Action                        | Default shortcut     | What it does                                        |
|-------------------------------|----------------------|-----------------------------------------------------|
| Dictate                       | Ctrl+Alt+Shift+D     | First press starts recording, second press sends it |
| Cancel Dictation              | Ctrl+Alt+Shift+X     | Throws the running recording away                   |
| Transcripts and Prompt Bench  | Ctrl+Alt+Shift+T     | Opens the stored transcripts and the prompt bench   |
| Edit Project Context…         | none                 | Opens `.vox.md`, creating it from a template        |
| Settings…                     | none                 | Opens Settings \| Tools \| Vox directly             |

The status bar widget (`Vox`, `Vox ● REC`, `Vox …`) shows the state; clicking it is the same as
Dictate. Rebind any of them in Settings | Keymap.

Vox: Dictate also swaps its own icon between a plain microphone and a recording-coloured one while
it is capturing, so the toolbar or menu entry reflects the state without reading the text.

Ctrl+Alt+Shift+V, the shortcut this plugin was originally meant to use, is already the IDE's own
`EditorPasteSimple` ("Paste as Plain Text") in the default keymap, so D (dictate) and X (discard)
are used instead. Nothing stops you from moving Vox onto V in the keymap and dropping the paste
binding.

The trigger is a toggle, not push-to-talk: an IDE action fires on key press and never reports the
key going up.

## Delivery

The result is typed into the selected tab of the Terminal tool window as if you had typed it. Vox
never sends a newline and never executes anything - you read the command and press Enter yourself.
Multi-line text is sent as a bracketed paste so the shell treats it as one block.

If no terminal tab is open, if the Terminal plugin is disabled, or if the terminal API is not the
one Vox expects, the text goes to the clipboard and a notification says so. The same happens when
"Type the result into the active IDE terminal" is switched off in the settings.

Two terminal engines are supported. The reworked engine (the 2026.2 default) exposes
`TerminalView.sendText` / `TerminalView.createSendTextBuilder()`, which lives in the Terminal
plugin's `intellij.terminal.frontend` content module; a plugin can only link against a content module
by requiring it, so Vox reaches it through the `"TerminalView"` data key and reflection instead, and
keeps loading when the Terminal plugin is absent. The classic engine exposes `JBTerminalWidget`,
which is part of the platform, and Vox writes to its `TtyConnector` directly.

## Settings

Settings | Tools | Vox, stored per application in `vox.xml`:

| Setting                  | Default                 | Meaning                                                 |
|--------------------------|-------------------------|---------------------------------------------------------|
| Backend URL              | `http://127.0.0.1:8765` | Root of the Vox backend                                 |
| Request timeout          | 180 s                   | Whole `/v1/process` round trip; connect is fixed at 3 s  |
| Microphone               | System default          | Matched by name, so a device index never goes stale      |
| Max recording            | 120 s                   | The recording stops itself and is sent at the cap        |
| Answer language          | Auto                    | The language of the finished message; Auto follows the speech |
| Type into terminal       | on                      | Off means always use the clipboard                       |
| Max `.vox.md` size       | 8000 bytes              | Larger files are truncated on a line boundary            |
| Send Claude Code context | on                      | Attaches the recent conversation of this project         |
| Exchanges to send        | 3                       | How many request/reply pairs are attached                |
| Max conversation size    | 6000 bytes              | Oldest exchanges are dropped first                       |

"Test Connection" calls `GET /health` and reports speech recognition and the language model
separately, warning when either is not ready rather than printing a model name that the backend
cannot actually reach.

## The language model

The **Language model (on the backend)** group configures the LLM the backend talks to, without
editing `.env` or restarting a container:

| Field | Meaning |
|---|---|
| Endpoint | Any OpenAI-compatible base URL - a local server, a box on the LAN, a hosted provider |
| Model | The model id that endpoint serves |
| API key | Blank leaves the current key alone; a space clears it for a local server that wants none |
| Warm up at startup | Off for a hosted endpoint: there is nothing to load and the call is billed |

**Load** reads what the backend is using; **Apply** writes it through `PUT /v1/config`. The backend
reconfigures its live client, persists the change to a volume so it survives a restart, and
re-probes the endpoint before answering - so Apply tells you immediately that a key is wrong
instead of letting it surface as a timeout mid-dictation.

The key is stored by the backend and never by the IDE, so there is no second copy of it in
`vox.xml`. `.env` remains the boot default and the setting made here wins over it.

Note that `PUT /v1/config` is unauthenticated and safe only because the backend binds to loopback.
Anything already running as you could repoint it; never expose that port.

## Transcripts and the prompt bench

The Vox tool window is both the history and a bench for working on prompts.

**The store.** Every dictation is kept: the raw transcript, the message that was actually sent, the
language, the microphone it came from, how many exchanges of context went with it, and how the
backend read the request. Only the
finished message reaches the terminal, so this is where the transcript survives, and it is what
makes a bad rewrite diagnosable without switching the backend's file tracing on. It persists across
IDE restarts in the project's own IDE state (`vox-transcripts.xml`), newest 200 kept - deliberately
not in the repository, because these are transcripts of everything you said and they have no
business in git.

**The bench.** Pick a stored transcript and press Run: it goes to `POST /v1/transform`, which runs
the same prompts, the same glossary and the same model a real dictation would, with no microphone
involved. The transcript is editable, so a word can be changed and the effect seen immediately.
Three things are yours to choose:

| Control | Effect |
|---|---|
| Prompt: Task / Dictation | Which of the two backend prompts runs - the same choice `/v1/process` and `/v1/dictate` make by path |
| Language | The `## LANGUAGE` section the prompt renders, and the glossary column it uses |
| `.vox.md` | Whether the project file is sent, so its effect on the answer can be isolated |

A replay is never stored. The bench answers "what would this prompt have done", and writing that
back would corrupt the record of what actually happened.

**How it was read.** Under the output sits what the backend's second pass made of the speech: the
verb it heard and what it aimed it at, the constraints it kept, what it read as hedged, and - only
when there is one - the Claude Code affordance it named. That last row is the one that changed the
message: a named tool means a line like "Use a subagent for this." was appended to what you were
about to send. The fields quote the speech, so they stay in the spoken language whatever language
the output came back in. A transcript recorded before this existed, or a request the pass could not
answer for, says so instead; it is never an error, and the message never depends on it.

The prompts themselves stay on the backend. The plugin only chooses which one to apply, so there is
no second copy of the prompt text, the glossary or the language blocks to keep in sync - edit
`prompt.md`, `dictation.md` or `analysis.md`, restart the backend, and press Run again.

## Microphone

Mono 16-bit signed little-endian PCM, at the first rate the mixer accepts out of 16 kHz, 48 kHz and
44.1 kHz, wrapped in a RIFF/WAVE file that only ever exists in memory. Nothing is ever written to
disk.

The device is stored by name rather than by index, because an index means nothing across restarts
and nothing at all across sound stacks - the Windows companion configures the same microphone
through PortAudio in `config/client.yaml`, whose indices do not match Java Sound's. A name works in
both, so the same string in either configuration picks the same microphone. An exact name wins;
failing that the first input device whose name contains the configured text is used, and a device
that is gone falls back to the system default rather than failing the recording.

## Answer language

The picker sets the language the finished message is written in. It defaults to **Auto**, which
answers in whatever language you actually spoke - the backend takes that from Whisper's detection
of the recording, so there is nothing to configure to have Russian come back as Russian. Whatever
it resolves to selects the matching `## LANGUAGE` section of the backend's prompt file and the
matching column of the glossary.

Picking a real language instead pins it regardless of what was said, and that combination is the
point of keeping the two separate: what you speak is recognised independently under the backend's
own `STT_LANGUAGE`, so dictating in Russian with the answer set to English keeps working. Adding a
language is an edit to `prompt.md`, `dictation.md` and `config/glossary.yaml`, not to this plugin.

## Project context

When `<project root>/.vox.md` exists, its text is sent with every request in the `project` field,
truncated to the configured byte budget on a line boundary; the project name goes with it in
`project_name`, which the backend uses for logging only.

The backend splits that file in two. A `## SYSTEM` section becomes instructions layered on top of
the built-in prompt - say how the answer should be shaped, and it is honoured over the prompt's own
format rules, though it can never make the model invent content that was not spoken. Everything
else is project context: vocabulary, constraints and worked examples. A file with no `## SYSTEM`
header is all context, exactly as before.

Tools | Vox: Edit Project Context… opens the file, writing a commented template first when the
project has none.

## Claude Code context

With "Send the recent Claude Code conversation" on, the plugin attaches the last few exchanges of
this project's newest Claude Code session, so a dictated request can say "this method" or "what we
just discussed" and still be understood. The backend puts it in a `<conversation>` block that the
prompt is explicit about: it is there to resolve references, and no task or requirement may be
taken from it.

Sessions are read from
`~/.claude/projects/<project path with ":" "\" and "/" replaced by "-">/<session id>.jsonl`, newest
file first, and only the tail is parsed. Tool calls, tool results, sidechains and hook output are
skipped; only what a person typed and what the assistant wrote back is kept. Reading only - Vox
never writes to those files. A project Claude Code has never run in simply sends no context.
