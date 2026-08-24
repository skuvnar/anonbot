# anonbot

DM the bot, it posts what you said in #anon. That's the whole bot.

No database, no logs, one file. If you don't believe it, read it.

One thing worth knowing before you use it: Discord still has your DMs.
Anonymous to the server is not the same as anonymous to Discord. Act
accordingly.

## Setup

**1.** Make an app at <https://discord.com/developers/applications>, add a bot.

- **Public Bot: off**
- **Message Content Intent: off** — it doesn't need it, and turning it on gives
  the bot the ability to read every channel in your server
- copy the token

**2.** Invite it with scopes `bot` and `applications.commands`, and exactly
three permissions: **View Channel**, **Send Messages**, **Embed Links**.

**3.** In `#anon`, deny **Send Messages** for `@everyone` so nobody fires one
off under their own name by accident.

**4.** Fill in `.env` and start it:

```
cp .env.example .env
docker compose pull
docker compose up -d
```

Turn on Developer Mode (*Settings → Advanced*) to right-click and copy the
server and channel IDs.

## Config

| Variable | Default | What it does |
|---|---|---|
| `DISCORD_TOKEN` | required | Bot token |
| `ANON_GUILD_ID` | required | Server it posts to |
| `ANON_CHANNEL_ID` | required | Channel it posts to |
| `MAX_MESSAGE_CHARS` | `4000` | Longest message it'll accept |
| `RATE_LIMIT_BURST` | `5` | Messages back to back, whole server |
| `RATE_LIMIT_PER_MINUTE` | `12` | Sustained rate, whole server |
| `REQUIRE_GUILD_MEMBERSHIP` | `true` | Ignore people who left the server |

## Commands

| Command | Who | What |
|---|---|---|
| `/pause` | mods | Stop relaying. Messages get refused, not queued. |
| `/resume` | mods | Start again. |
| `/status` | anyone | Message counts since last restart. Totals only. |
| `/attest` | anyone | Which commit it's running. |

## Things it doesn't do

- **Attachments.** Text only. Images carry metadata that identifies you.
- **Queue anything while it's down.** No reply from the bot means it didn't post.
- **Let mods ban a sender.** There's nothing to ban. Delete the message and
  `/pause` if someone's being a dick.
- **Per-person rate limits.** One bucket for the whole server, because a
  per-person limit means keeping track of people.
