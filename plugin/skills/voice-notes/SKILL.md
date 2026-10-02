---
name: voice-notes
description: Use an inbound Telegram voice or audio transcript as the user's message, with a local transcription fallback when the poller could not transcribe it. Use for attachment_kind voice or audio.
---

# Voice notes

When the inbound metadata says `transcript="ok"`, the body already contains the transcript. Treat that text as the user's request. Do not download or transcribe it a second time, and do not repeat the whole transcript unless asked.

For any other transcript status:

1. Set `FLEET_HOME` to `${FLEET_HOME:-$HOME/.claude/telegram-fleet}`. Read the fleet's `voice` option with `"$FLEET_HOME/bin/fleet-config" option voice`. If it is `off`, or this bot has `$TELEGRAM_STATE_DIR/autotranscribe.off`, tell the user automatic voice transcription is off and ask them to type the request. Do not silently bypass their switch.
2. Read `attachment_file_id` from the message. Call the channel's download_attachment tool with that id. Use only the returned local file path.
3. Run `"$FLEET_HOME/bin/transcribe-voice" "<returned-path>"`. The installed dispatcher tries the warm local daemon, then the available local backend. Never construct a shell command from transcript text.
4. On exit 0 with a nonempty transcript, use it as the request. A nonzero exit or empty output is a failure: report it briefly and ask for typed text. If the words are ambiguous, ask about that part; do not guess.

Audio stays on the Mac. A first local model download can take time; do not promise a warm-model speed before observing it. The warm daemon is optional and uses additional memory. Backend or dependency installation is an attended setup task, not something a failed voice note authorizes.
