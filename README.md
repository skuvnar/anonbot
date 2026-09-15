# anonbot

Tap the button in #anon, a box pops up, what you write in it appears as
`Human 042`. Same number all day, new number tomorrow. That's the whole bot.

No database, no logs, one file. If you don't believe it, read it.

One thing worth knowing before you use it: Discord still sees what you
submit. Anonymous to the server is not the same as anonymous to Discord.
Act accordingly.

## Setup

**1.** Make an app at <https://discord.com/developers/applications>, add a bot.

- **Public Bot: off**
- **Message Content Intent: off** — it doesn't need it, and turning it on gives
  the bot the ability to read every channel in your server. It doesn't ask
  for any message intent at all: Discord never sends it a message from anyone,
  in a channel or a DM. The button is the only way in.
- copy the token

**2.** Invite it with scopes `bot` and `applications.commands`, and exactly
two permissions: **View Channel** and **Send Messages**.

**3.** In `#anon`, deny **Send Messages** for `@everyone`, then add the bot's
role to the channel with **Send Messages** allowed. The second half matters: a
deny on `@everyone` applies to the bot too unless its role gets its own
allow. The channel is read-only on purpose: there's no text box to fire
something off under your own name by accident, and Discord won't let a slash
command run there either. The bot keeps a button at the bottom, and that's
how people post.

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
| `MAX_MESSAGE_CHARS` | `2000` | Longest message it'll accept. Capped at 1986 so the number line fits |
| `RATE_LIMIT_BURST` | `5` | Messages back to back, whole server |
| `RATE_LIMIT_PER_MINUTE` | `12` | Sustained rate, whole server |

## Commands

Posting isn't a command. It's the button.

| Command | Who | What |
|---|---|---|
| `/pause` | mods | Stop relaying. Messages get refused, not queued. |
| `/resume` | mods | Start again. |
| `/status` | anyone | Message counts since last restart. Totals only. |

## Numbers

Every post is signed `Human 042`. Same person, same number, until midnight
UTC. Then the deck is reshuffled, the bot says so in the channel, and
yesterday's Human 042 is nobody in particular. A restart reshuffles too, and
the bot announces that the same way.

How it recognises you without remembering you: your user ID goes through a
keyed hash (HMAC) under a secret that exists only in the bot's memory. The
secret is made at startup and again at midnight. Only the hash is kept, and
only as the key for looking up your number. No secret, no way back.

What that means in practice:

- Numbers are dealt from a shuffled deck of `000`-`999`. Nobody shares one,
  and the number doesn't tell you who wrote first.
- After midnight or a restart, nobody can recover who was who. Not the person
  running it. It's gone.
- During the day, whoever has root on the box could dump the bot's memory,
  take the secret, and work it out. They could also just edit the bot to log
  everything. That's the same trust you were already extending, not a new one.
- A number is a trail. For one day, everything under it can be read together,
  and how you write or when you post can give you away. The reset keeps the
  trail short. Don't say anything under a number that you wouldn't want joined
  up with the rest of that day.

## Things it doesn't do

- **Attachments.** The box takes text. Images carry metadata that identifies you.
- **Read DMs.** It doesn't ask Discord for them, so a DM to it goes nowhere.
- **Queue anything while it's down.** If it didn't appear in the channel, it
  didn't post.
- **Let mods ban a sender.** There's nothing to ban, and a number isn't a
  person. Delete the message and `/pause` if someone's being a dick.
- **Per-person rate limits.** One bucket for the whole server, because a
  per-person limit means keeping track of people. If a mod needs to cut one
  person off, hiding `#anon` from a role does it, and the bot never knows.
- **Remember you past midnight UTC.** The number table and the secret behind
  it are thrown away every day and on every restart.
