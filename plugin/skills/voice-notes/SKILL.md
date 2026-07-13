---
name: voice-notes
description: Transcribe a Telegram voice or audio note locally and treat the transcript as the user's message. Use WHENEVER an inbound channel message carries an attachment whose attachment_kind is "voice" or "audio" (a voice note, an audio file, an .oga/.ogg/.m4a/.mp3). Downloads the file with the plugin's download_attachment tool, runs the local transcriber, and works from the resulting text as if the user had typed it.
---

An inbound Telegram message arrived with a voice or audio attachment. Turn it
into text locally, then act on that text as the user's actual message.

## Steps

1. Detect the attachment. The `<channel ...>` tag on the inbound message includes
   `attachment_kind="voice"` (or `"audio"`) and an `attachment_file_id`.

2. Download it. Call the plugin's `download_attachment` tool with that
   `attachment_file_id`. It saves the file into the bot's `inbox/` and returns
   the local path.

3. Transcribe it locally. Choose the script by the fleet's voice option:

   ```
   voice="$($FLEET_HOME/bin/fleet-config option voice 2>/dev/null || echo auto)"
   ```

   - `auto` (default) or `mlx`: on Apple Silicon run
     `$FLEET_HOME/bin/transcribe-voice-mlx <path>`.
   - `auto` on other hardware, or `faster`: run
     `$FLEET_HOME/bin/transcribe-voice-faster <path>`.
   - `off`: do not transcribe; tell the user voice is disabled and ask them to
     type the request.

   `$FLEET_HOME` defaults to `~/.claude/telegram-fleet`. Each script prints only
   the transcript to stdout.

4. Use the transcript as the message. Treat the transcribed text as the user's
   request and handle it normally. Do not echo the full transcript back unless
   the user asks; a brief confirmation of what you heard is enough for anything
   ambiguous.

## Notes

- Transcription runs entirely on the machine. No audio leaves it.
- The first run downloads the whisper model and is slow; later runs are fast
  (about 1-2 seconds warm on Apple Silicon).
- If a script exits non-zero, report that voice transcription failed and ask the
  user to type the request, rather than guessing at the audio.
- Install details and the backend choice are in `voice/README.md`.
