"""
audit this by just reading it, its like 400 lines and most of them are comments.
or paste it into chatgpt and ask it if it keeps any logs. who cares nerds idgaf

---

If you are editing this, three rules keep it honest:

  1. No sender identifier gets stored, counted or logged. Ever. The one thing
     ever derived from one is today's number (see Numbering), keyed under a
     secret that lives in memory and dies at midnight UTC.
  2. Nothing is written to disk. No database, no queue, no filesystem imports.
  3. emit() is the only thing that prints, and it only takes fixed strings.

Break one of those and it is a different bot with the same name.
"""

from __future__ import annotations

import datetime
import hmac
import os
import secrets
import sys
import time

import discord
from discord import app_commands
from discord.ext import tasks

# --------------------------------------------------------------------------
# User-facing strings
#
# Every string a sender can ever see is collected here so that the form
# handler below stays short enough to read in one pass.
# --------------------------------------------------------------------------

TEXT_BUTTON_PROMPT = "The channel is read-only. Use the button."
TEXT_BUTTON_LABEL = "Post anonymously"
TEXT_FORM_TITLE = "Post anonymously"
TEXT_FORM_LABEL = "Your message"

TEXT_EMPTY = "Nothing to post - the message was blank."
TEXT_RATE_LIMITED = "The relay is busy right now. Wait a moment and send it again."
TEXT_PAUSED = "The relay is paused by the moderators. Your message was not posted."
TEXT_WRONG_SERVER = "This only works in the server it posts to."
TEXT_NO_NUMBERS = "Every number for today has been dealt. Try again after midnight UTC."
TEXT_FAILED = "Discord rejected the post, so it did not go through. Try again."
TEXT_UNAVAILABLE = "The relay channel is not reachable right now. Your message was not posted."

# Posted in the relay channel itself, not to a sender.
TEXT_RESHUFFLED = "Numbers reshuffled. Any Human above this line is unrelated to any Human below it."


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"[anonbot] missing required environment variable: {name}")
    return value


def _require_id(name: str) -> int:
    raw = _require(name)
    if not raw.isdigit():
        sys.exit(f"[anonbot] {name} must be a numeric Discord ID")
    return int(raw)


def _optional_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        sys.exit(f"[anonbot] {name} must be an integer")


TOKEN = _require("DISCORD_TOKEN")
GUILD_ID = _require_id("ANON_GUILD_ID")
CHANNEL_ID = _require_id("ANON_CHANNEL_ID")

# Discord caps a bot's message content at 2000 characters, and every post
# spends a few of those on its "Human 042" line. The form below refuses to
# submit anything longer than fits, so the limit is enforced by Discord's own
# client before the text ever reaches this program.
TAG_OVERHEAD = len("**Human 000**\n")
MAX_MESSAGE_CHARS = min(_optional_int("MAX_MESSAGE_CHARS", 2000), 2000 - TAG_OVERHEAD)
RATE_LIMIT_BURST = _optional_int("RATE_LIMIT_BURST", 5)
RATE_LIMIT_PER_MINUTE = _optional_int("RATE_LIMIT_PER_MINUTE", 12)

# Injected at build time, so the startup line can say which commit is running.
BUILD_SHA = os.environ.get("GIT_SHA", "unknown")


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def emit(event: str) -> None:
    """The only place this program writes anything, anywhere.

    Every call site passes a fixed string. Message content, user IDs and
    channel IDs are never formatted into an event, so redirecting this
    stream to a file would still capture nothing about who said what.
    """
    print(f"[anonbot] {event}", flush=True)


# Monotonic tallies, kept so moderators can see throughput without anyone
# being able to derive who produced it. Reset on every restart.
COUNTS: dict[str, int] = {}


def _count(key: str) -> None:
    COUNTS[key] = COUNTS.get(key, 0) + 1


# --------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------


class GlobalRateLimiter:
    """A token bucket shared by everyone who writes in.

    Deliberately global. A per-sender limit would mean holding an identifier
    for each person who has recently sent something, and the numbering below
    is the only place this bot tolerates anything like that. The trade is
    that one person sending a flood consumes everyone's budget until it
    refills; the moderator kill switch exists for that case.

    Uses a monotonic clock, so not even a wall-clock timestamp is retained.
    """

    def __init__(self, capacity: int, refill_per_minute: int) -> None:
        self._capacity = float(max(capacity, 1))
        self._refill_per_second = max(refill_per_minute, 1) / 60.0
        self._tokens = self._capacity
        self._updated = time.monotonic()

    def take(self) -> bool:
        now = time.monotonic()
        elapsed = now - self._updated
        self._updated = now
        self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_per_second)
        if self._tokens < 1.0:
            return False
        self._tokens -= 1.0
        return True


_rate_limiter = GlobalRateLimiter(RATE_LIMIT_BURST, RATE_LIMIT_PER_MINUTE)


# --------------------------------------------------------------------------
# Numbering
#
# Every post is signed "Human 042" so readers can tell one sender's messages
# apart from another's within a day, and nothing more. The number says
# nothing about who you are: it is dealt from a shuffled deck of 000-999, so
# no two people share one, and it does not even reveal who wrote first.
#
# Handing you the same number on your second message means recognising you,
# and that is the one place this program touches a sender identifier. It is
# run through HMAC (a hash mixed with a secret key) and only the digest is
# kept, as the lookup key for your number. The secret is generated at startup
# and again at midnight UTC, when the deck is reshuffled and the table thrown
# away. Without the secret, a digest is noise.
#
# What that buys and what it does not, plainly:
#
#   - Nothing is written anywhere. The table lives in this process and
#     nowhere else. Rule 2 stands.
#   - After midnight, or a restart, nobody can recover the mapping. Not the
#     operator, not a subpoena, not a backup. The secret is gone.
#   - During the day, root on the host could dump this process's memory,
#     take the secret, hash every member of the server and read off the
#     table. Root could equally have edited this file to log everything, so
#     that is the trust you were already extending, not a new one.
#   - A number is a trail. Everything under "Human 042" can be read together
#     for a day, and writing style or posting times can give people away.
#     The midnight reset is what keeps the trail short.
# --------------------------------------------------------------------------


class DailyNumbering:
    """The day's secret, the deck, and the numbers dealt so far."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._secret = secrets.token_bytes(32)
        self._deck = list(range(1000))
        secrets.SystemRandom().shuffle(self._deck)
        self._dealt: dict[bytes, int] = {}

    def tag(self, user_id: int) -> str | None:
        """Today's label for this sender, dealing a fresh one on first sight.

        Returns None only once all thousand numbers are taken, which would
        take a thousand different senders in one day. Refusing beats clashing.
        """
        digest = hmac.digest(self._secret, str(user_id).encode(), "sha256")
        number = self._dealt.get(digest)
        if number is None:
            if not self._deck:
                return None
            number = self._deck.pop()
            self._dealt[digest] = number
        return f"Human {number:03d}"


_numbering = DailyNumbering()


# --------------------------------------------------------------------------
# Client
#
# Intents are the mechanism by which Discord decides what this bot is allowed
# to receive. What is requested here, and what is not, is the load-bearing
# privacy claim of the whole project:
#
#   guilds - channel topology, so the relay channel can be resolved. Grants
#            no access to messages or to member lists.
#
# That is the whole list. No message intent of any kind is requested, so
# Discord never sends this bot a message from anyone, anywhere - not from a
# server channel, not from a DM. The only thing a person can hand it is the
# form behind the button, and the only thing in that form is the text box.
#
# message_content must stay switched off in the developer portal. It is not
# needed, and it is the only mechanism by which a bot can read what people
# say in server channels.
# --------------------------------------------------------------------------

intents = discord.Intents.none()
intents.guilds = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

_relay_channel: discord.TextChannel | None = None
_button_id: int | None = None
_started = False
_paused = False


async def _whisper(interaction: discord.Interaction, text: str) -> None:
    """Reply to the person who submitted the form, and to nobody else.

    Every reply in this file goes through here so that the ephemeral flag is
    set in exactly one place. It is load-bearing. Discord records whoever
    triggered an interaction on any public reply to it and the client shows
    that name, which would put the sender above their own anonymous post.
    Do not add a second way of replying.

    Once an interaction has been acknowledged (the form does this first, see
    on_submit) a reply has to go out as a follow-up instead. Both paths are
    private; a failed reply is not worth recording.
    """
    send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
    try:
        await send(text, ephemeral=True)
    except discord.HTTPException:
        pass


async def _announce_reshuffle() -> None:
    """Tell the channel that every number has just changed hands.

    Posted at midnight UTC, and once at startup because a restart deals a
    new deck too. A failed post is not retried: readers will notice the
    numbers changed, and there is nothing worth recording about the failure.
    """
    if _relay_channel is None:
        return
    try:
        await _relay_channel.send(TEXT_RESHUFFLED, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        pass


async def _place_button() -> None:
    """Keep the button as the last thing in the channel.

    The channel is read-only for everyone but the bot, so there is no text
    box; the button stands where one would be. It is re-posted after every
    relay and every reshuffle notice so it never scrolls out of reach. Only
    the bot's own previous button is deleted, which needs no permission
    beyond posting. After a restart the previous one is unknown and stays
    where it is: it still works, and a moderator can delete it.
    """
    global _button_id
    if _relay_channel is None:
        return
    if _button_id is not None:
        try:
            await _relay_channel.get_partial_message(_button_id).delete()
        except discord.HTTPException:
            pass
    try:
        posted = await _relay_channel.send(TEXT_BUTTON_PROMPT, view=AnonButton())
        _button_id = posted.id
    except discord.HTTPException:
        _button_id = None
        emit("could not post the button - does the bot have Send Messages in the relay channel?")


@tasks.loop(time=datetime.time(hour=0, tzinfo=datetime.timezone.utc))
async def _midnight() -> None:
    _numbering.reset()
    emit("midnight UTC, numbers reshuffled")
    await _announce_reshuffle()
    await _place_button()


class AnonForm(discord.ui.Modal, title=TEXT_FORM_TITLE):
    """The popup behind the button. One text box, nothing else.

    A popup rather than a slash command, because Discord will not let a
    slash command run in a channel where you cannot send messages, and a
    read-only channel is the point: nothing to type into means no posting
    under your own name by accident, and no "someone is typing" the moment
    before an anonymous post appears. A button works in a read-only channel.
    """

    field = discord.ui.Label(
        text=TEXT_FORM_LABEL,
        component=discord.ui.TextInput(
            style=discord.TextStyle.paragraph,
            max_length=MAX_MESSAGE_CHARS,
            required=True,
        ),
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Discord insists on some answer within three seconds of a form being
        # submitted. This one closes the popup and shows the sender nothing.
        # A successful post is its own receipt - it appears in the channel
        # with the number on it - whereas a private "you are Human 042" would
        # sit in the sender's view captioned with their own name, one
        # screenshot away from undoing the whole point. Only refusals get a
        # note, via _whisper.
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            return

        if _relay_channel is None:
            await _whisper(interaction, TEXT_UNAVAILABLE)
            return

        # The button only exists in the one channel, so this cannot normally
        # be reached from anywhere else. Checked anyway, because the guarantee
        # should not rest on where a message happens to sit.
        if interaction.guild_id != GUILD_ID:
            _count("rejected_wrong_server")
            await _whisper(interaction, TEXT_WRONG_SERVER)
            return

        if _paused:
            _count("rejected_paused")
            await _whisper(interaction, TEXT_PAUSED)
            return

        content = self.field.component.value.strip()
        if not content:
            _count("rejected_empty")
            await _whisper(interaction, TEXT_EMPTY)
            return

        if not _rate_limiter.take():
            _count("rejected_rate_limited")
            await _whisper(interaction, TEXT_RATE_LIMITED)
            return

        # Dealt after every other check so a refused message never takes a
        # number. See the Numbering section for what this does with the sender.
        tag = _numbering.tag(interaction.user.id)
        if tag is None:
            _count("rejected_no_numbers")
            await _whisper(interaction, TEXT_NO_NUMBERS)
            return

        # From here on, only `tag` and `content` are in play. Nothing
        # downstream of this point has access to the sender.
        # Posted as an ordinary bot message, not as a reply to the interaction,
        # which is what keeps the sender's name off it. suppress_embeds stops
        # links unfurling into preview cards, so a post appears exactly as it
        # was typed and nothing else.
        #
        # allowed_mentions is the ONLY thing stopping a submission containing
        # @everyone from notifying the whole server. Discord enforces it server
        # side, so it holds regardless of what the text says - but remove it
        # and the relay becomes a mass-ping button for anyone who wants one.
        try:
            await _relay_channel.send(
                f"**{tag}**\n{content}",
                allowed_mentions=discord.AllowedMentions.none(),
                suppress_embeds=True,
            )
        except discord.HTTPException:
            _count("relay_failed")
            await _whisper(interaction, TEXT_FAILED)
            return

        _count("relayed")
        await _place_button()


class AnonButton(discord.ui.View):
    """The one button the bot keeps at the bottom of the channel.

    Persistent: no timeout and a fixed custom_id, and registered with the
    client at startup, so a button left over from an earlier run still opens
    the form.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label=TEXT_BUTTON_LABEL, style=discord.ButtonStyle.primary, custom_id="anonbot:post")
    async def post(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(AnonForm())


@client.event
async def on_ready() -> None:
    global _relay_channel, _started

    try:
        channel = await client.fetch_channel(CHANNEL_ID)
    except discord.HTTPException:
        emit("startup: could not resolve the relay channel")
        return

    # Halting here rather than calling sys.exit(): this runs on an event task,
    # and discord.py only traps Exception, so a SystemExit would kill the task
    # and leave the process alive with no relay channel.
    if not isinstance(channel, discord.TextChannel):
        emit("startup: ANON_CHANNEL_ID is not a text channel - shutting down")
        await client.close()
        return
    if channel.guild.id != GUILD_ID:
        emit("startup: ANON_CHANNEL_ID does not belong to ANON_GUILD_ID - shutting down")
        await client.close()
        return

    _relay_channel = channel

    # on_ready fires again after a reconnect. Everything that must happen
    # exactly once per process lives behind this flag.
    if not _started:
        guild = discord.Object(id=GUILD_ID)
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)
        client.add_view(AnonButton())
        _midnight.start()
        await _announce_reshuffle()
        await _place_button()
        _started = True

    emit(f"ready, build {BUILD_SHA}")


# --------------------------------------------------------------------------
# Moderation and status
# --------------------------------------------------------------------------


def _is_moderator(interaction: discord.Interaction) -> bool:
    user = interaction.user
    return isinstance(user, discord.Member) and user.guild_permissions.manage_messages


@tree.command(name="pause", description="Stop relaying anonymous messages until resumed.")
@app_commands.default_permissions(manage_messages=True)
async def pause(interaction: discord.Interaction) -> None:
    global _paused
    if not _is_moderator(interaction):
        await _whisper(interaction, "Moderators only.")
        return
    _paused = True
    emit("relay paused")
    await interaction.response.send_message("Relay paused. Incoming messages are refused, not queued.")


@tree.command(name="resume", description="Resume relaying anonymous messages.")
@app_commands.default_permissions(manage_messages=True)
async def resume(interaction: discord.Interaction) -> None:
    global _paused
    if not _is_moderator(interaction):
        await _whisper(interaction, "Moderators only.")
        return
    _paused = False
    emit("relay resumed")
    await interaction.response.send_message("Relay resumed.")


@tree.command(name="status", description="Show relay throughput since the last restart.")
async def status(interaction: discord.Interaction) -> None:
    state = "paused" if _paused else "running"
    tally = ", ".join(f"{k}: {v}" for k, v in sorted(COUNTS.items())) or "nothing yet"
    await _whisper(
        interaction,
        f"Relay is **{state}**.\nSince the last restart - {tally}.\n"
        "These are totals only. No per-person figures exist to report.",
    )


if __name__ == "__main__":
    # log_handler=None stops discord.py installing its own logging handler.
    # Without this the library would emit gateway diagnostics of its own and
    # emit() would no longer be the only thing writing to stdout.
    client.run(TOKEN, log_handler=None)
