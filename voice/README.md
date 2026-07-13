# Voice notes

These scripts turn a Telegram voice or audio note into text so Claude can treat
it as a normal prompt. Each one takes an audio path and prints only the
transcript to stdout.

The `voice-notes` skill wires this into the channel flow: when a message arrives
with `attachment_kind` of `voice` or `audio`, Claude downloads the file with the
plugin's `download_attachment` tool, runs the transcriber, and works from the
transcript. Which script it runs is chosen by `fleet-config option voice`.

## Two backends

| Script | Backend | Best on | Speed |
|---|---|---|---|
| `transcribe-voice-mlx` | mlx-whisper (`whisper-large-v3-turbo`) | Apple Silicon | ~1-2s warm |
| `transcribe-voice-faster` | faster-whisper | any CPU or GPU | slower, portable |

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

Anywhere else (Intel Mac, Linux, Windows):

```
pip install faster-whisper
```

The first run downloads the model. On a GPU, set `TRANSCRIBE_COMPUTE=float16`
for a large speedup.

## Usage

```
transcribe-voice-mlx /path/to/note.oga
transcribe-voice-faster /path/to/note.oga en
```

Both accept an optional second argument for the language code (default `en`).
Model overrides: `TRANSCRIBE_MODEL` on either script, plus `TRANSCRIBE_COMPUTE`
on the faster-whisper one.

## Notes

- Voice transcription runs entirely on your machine. No audio leaves the Mac.
- The installer copies both scripts into `$FLEET_HOME/bin` so they are on the
  path the skill uses.
