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

| Action                | Default shortcut     | What it does                                         |
|-----------------------|----------------------|------------------------------------------------------|
| Vox: Dictate          | Ctrl+Alt+Shift+D     | First press starts recording, second press sends it  |
| Vox: Cancel Dictation | Ctrl+Alt+Shift+X     | Throws the running recording away                    |

Both actions are in the Tools menu, and the status bar widget (`Vox`, `Vox ● REC`, `Vox …`) shows the
state; clicking it is the same as Vox: Dictate. Rebind either action in Settings | Keymap.

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

| Setting                  | Default                 | Meaning                                              |
|--------------------------|-------------------------|------------------------------------------------------|
| Backend URL              | `http://127.0.0.1:8765` | Root of the Vox backend                              |
| Mode                     | `context`               | `dictation`, `clean`, `task` or `context`            |
| Request timeout          | 180 s                   | Whole `/v1/process` round trip; connect is fixed at 3 s |
| Max recording            | 120 s                   | The recording stops itself and is sent at the cap    |
| Max `.vox.md` size       | 8000 bytes              | Larger files are truncated on a line boundary        |
| Type into terminal       | on                      | Off means always use the clipboard                   |

"Test Connection" calls `GET /health` and reports the backend status, the speech model and its
device, and the language model.

## Project context

When `<project root>/.vox.md` exists, its text is sent with every request in the `project` field of
`POST /v1/process`, truncated to the configured byte budget on a line boundary; the project name
goes with it in `project_name`, which the backend uses for logging only. The plugin reads that one
file and nothing else: the repository never leaves the machine, and the backend never reads it.

## Audio

Mono 16-bit signed little-endian PCM from the default input device, at the first rate the mixer
accepts out of 16 kHz, 48 kHz and 44.1 kHz, wrapped in a RIFF/WAVE file that only ever exists in
memory. Nothing is ever written to disk.
