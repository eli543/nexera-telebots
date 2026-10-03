# Voice notes

These scripts turn a Telegram voice or audio note into text on the Mac. The
poller runs `transcribe-voice <audio-path>` and forwards the transcript as the
message. It prints only the transcript to stdout.

The `voice-notes` skill handles a note when the poller cannot transcribe it.
The `voice` option in `fleet.json` is read by the setup checklist and fallback
skill; it does not select or disable poller transcription. Create an
`autotranscribe.off` marker in a bot's state directory to disable automatic
transcription for that bot. Remove the marker to re-enable it.

## Two backends

| Script | Backend | Best on | Speed |
|---|---|---|---|
| `transcribe-voice` | Warm daemon, then an installed local backend | Any Mac | Depends on backend |
| `transcribe-voice-mlx` | mlx-whisper (`whisper-large-v3-turbo`) | Apple Silicon | Fast after model load |
| `transcribe-voice-faster` | faster-whisper | Intel Mac | Slower |

`transcribe-voice` first tries the optional warm daemon. Without a daemon
reply, it uses mlx-whisper when installed on Apple Silicon, then tries the
faster-whisper script as the local fallback. If no backend succeeds, the poller
passes the original voice note to the fallback skill.

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
