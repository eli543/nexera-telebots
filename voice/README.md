# Voice notes

These scripts turn a Telegram voice or audio note into text on the Mac. The
poller runs `transcribe-voice <audio-path>` and forwards the transcript as the
message. It prints only the transcript to stdout.

The `voice-notes` skill handles a note when the poller cannot transcribe it.
`fleet-config option voice` selects the backend. An `autotranscribe.off` marker
in the bot's state directory disables poller transcription for that bot.

## Two backends

| Script | Backend | Best on | Speed |
|---|---|---|---|
| `transcribe-voice` | Warm daemon, then an installed local backend | Any Mac | Depends on backend |
| `transcribe-voice-mlx` | mlx-whisper (`whisper-large-v3-turbo`) | Apple Silicon | Fast after model load |
| `transcribe-voice-faster` | faster-whisper | Intel Mac | Slower |

The `voice` option in `fleet.json` selects the behavior:

- `auto` (default): use mlx on Apple Silicon, otherwise faster-whisper, and
  disable voice if neither is installed.
- `mlx`: force the mlx backend.
- `faster`: force the faster-whisper backend.
- `off`: disable voice transcription.

## Install

Apple Silicon (recommended, fastest):

```
uv tool install mlx-whisper
brew install ffmpeg
```

`uv` installs the `mlx_whisper` command to `~/.local/bin`. `ffmpeg` is used to
decode the `.oga` files Telegram sends. The first run downloads the
`whisper-large-v3-turbo` model (a few hundred MB), so the first transcription is
slower than later ones.

On an Intel Mac:

```
pip install faster-whisper
```

The first run downloads the model. Voice processing stays local.

## Usage

```
transcribe-voice /path/to/note.oga
transcribe-voice-mlx /path/to/note.oga
transcribe-voice-faster /path/to/note.oga en
```

Both accept an optional second argument for the language code (default `en`).
Model overrides: `TRANSCRIBE_MODEL` on either script, plus `TRANSCRIBE_COMPUTE`
on the faster-whisper one.

## Notes

- Voice transcription runs entirely on your machine. No audio leaves the Mac.
- The warm daemon is optional and keeps the model in memory. Its LaunchAgent
  template is `daemon/com.claude-fleet.voice.plist.template`; the installer
  must replace `__UV_TOOL_PYTHON__` with the interpreter inside `uv tool dir`
  and `__FLEET_HOME__` with the absolute fleet path. It is never loaded during
  the standard install.
- The daemon listens at `$FLEET_HOME/voice/daemon.sock` (mode 600). The client
  falls back to mlx-whisper, then faster-whisper when the socket is unavailable.
- Optional whole-word name corrections go in
  `$FLEET_HOME/voice/name-fixes.tsv` as `heard<TAB>correct`.
