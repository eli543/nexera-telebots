# The fleet: N bots, one brain

Most projects that run several Telegram bots make them different: a coding
specialist, a research specialist, a scheduler. This one makes them the same.
Every bot is an identical Claude Code setup that reads the same knowledge base.
The value is not division of labor; it is having several independent chat
surfaces into one operation, any of which can pick up where another left off.

## Why identical

- One knowledge base to maintain. A fact you teach the operation is a fact every
  bot knows, because they all read the same `CLAUDE.md` and the same notes.
- Any bot can answer anything. There is no "wrong bot for this question."
- Sessions are portable. Because the setups are identical, a conversation started
  in one bot resumes cleanly in another. That is what powers the cross-bot resume
  picker.

What differs between bots is deliberately small: the name, the key, the working
directory, the state directory, the token, and an emoji. Everything else, the
hooks, the permission stance, the watchdog logic, is shared.

## The shared brain in practice

The bots share knowledge the same way any Claude Code project does, through
context files.

- A `CLAUDE.md` that each bot's working directory can see, holding the rules and
  facts you want every bot to follow. If you keep your knowledge in a notes vault,
  point the bots at it and reference it from `CLAUDE.md`.
- Because each bot runs in its own working directory, you can either give each a
  copy of the same `CLAUDE.md` or point them all at one shared location. A single
  shared source is easier to keep consistent: edit once, every bot updates.

There is no separate database. The "brain" is your context files plus Claude
Code's own session transcripts, which the resume and restore features read.

## Cross-bot resume

From any bot, `/resume` builds a numbered list of recent sessions across every
bot in the fleet, newest first, each with a short topic label:

```
/resume
  1  🤖 Main      invoice script for the Q3 batch      12m ago
  2  🔬 Research  competitor pricing pull              1h ago
  3  🤖 Main      debugging the deploy hook            3h ago
  ...
```

`/resume 3` opens session 3 in the bot you are chatting with. The transcript is
copied into this bot's project directory and the bot relaunches resumed into it.
The origin bot is left untouched, so you can open the same conversation in two
bots if you want.

The topic labels use the same optional key as the reply judge. With no key, the
list still renders, using a short heuristic preview instead of a generated label.

The picker list is regenerated on each `/resume`. If you send `/resume 3` more
than a few hours after the list was built, it refuses and asks you to send
`/resume` again, because the numbering drifts as sessions age.

## Adding a bot

1. Create a bot with @BotFather and copy its token.
2. Add an entry to `~/.claude/telegram-fleet/fleet.json`:

```json
{
  "name": "Ops",
  "key": "ops",
  "workdir": "~/claude-fleet/ops",
  "state_dir": "~/.claude/channels/fleet-ops",
  "emoji": "🛠"
}
```

3. Create the state directory and write the token into its `.env` at `chmod 600`,
   or run `/telegram:configure` from that bot. Seed `access.json` with your
   allowlist.
4. Pair and lock down: DM the new bot, run `/telegram:access pair <code>`, then
   `/telegram:access policy allowlist`.
5. The watchdog picks the new bot up on its next pass. No script edits are needed;
   every engine reads the bot list from `fleet.json`.

The `fleet-ops` skill has the day-two version of this, plus token rotation,
reading logs, and the kill switches.

## Keys and paths

- `key` must match `^[a-z0-9][a-z0-9-]{0,30}$` and be unique. It is the handle
  every script uses.
- Paths may use `~`; every consumer expands it.
- The state directory should keep the `fleet-` prefix so it never collides with
  the stock plugin's default `telegram` directory.

A single bot is the default install. A fleet is simply two or more entries in the
same file.
