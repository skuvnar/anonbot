"""
audit this by just reading it, its like 400 lines and most of them are comments.
or paste it into chatgpt and ask it if it keeps any logs. who cares nerds idgaf

---

If you are editing this, three rules keep it honest:

  1. No sender identifier gets stored, counted or logged. Ever. The one thing
     ever derived from one is today's number (see Numbering), keyed under a
     secret that lives in memory and dies at midnight UTC.
  2. Nothing is written to disk. No database, no queue. The one thing read
     from disk is the list of file names in avatars/, once, at startup.
  3. emit() is the only thing that prints, and it only takes fixed strings.

Break one of those and it is a different bot with the same name.
"""

from __future__ import annotations

import asyncio
import datetime
import hmac
import os
import re
import secrets
import sys
import time
from urllib.parse import quote

import discord
from discord import app_commands
from discord.ext import tasks

# --------------------------------------------------------------------------
# User-facing strings
#
# Every string a sender can ever see is collected here so that the form
# handler below stays short enough to read in one pass.
# --------------------------------------------------------------------------

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


def _optional_pairs(name: str) -> dict[str, int]:
    """Parse `name:id,name:id` into a dict. Unset means empty."""
    pairs: dict[str, int] = {}
    for item in filter(None, (s.strip() for s in os.environ.get(name, "").split(","))):
        label, sep, ident = item.rpartition(":")
        if not sep or not label or not ident.isdigit():
            sys.exit(f"[anonbot] {name} entries must look like name:id")
        pairs[label] = int(ident)
    return pairs


TOKEN = _require("DISCORD_TOKEN")
GUILD_ID = _require_id("ANON_GUILD_ID")
CHANNEL_ID = _require_id("ANON_CHANNEL_ID")

# A webhook for the relay channel, made by hand in the channel's settings.
# Posts go out through it with the day's number as the display name, which
# is what puts "Human 042" where an author's name goes and keeps the bot's
# own name off every post. Anyone holding this URL can post to the channel
# under any name, so it is a secret in the same class as the token.
WEBHOOK_URL = _require("ANON_WEBHOOK_URL")

# Avatars. Each Human gets a picture for the day, dealt from the avatars/
# folder the same way numbers are. Only the file names are read, once, here;
# the pictures themselves are fetched by Discord's servers from
# ANON_AVATAR_BASE, which the build sets to this repository's copy of the
# folder at the exact commit the image was built from. Nobody's client ever
# touches that host. Leave ANON_AVATAR_BASE unset and posts simply use the
# webhook's own avatar. Once the folder runs out for the day, everyone else
# gets DEFAULT_AVATAR.
AVATAR_BASE = os.environ.get("ANON_AVATAR_BASE", "").strip().rstrip("/")
try:
    AVATARS = sorted(name for name in os.listdir("avatars") if not name.startswith("."))
except FileNotFoundError:
    AVATARS = []
DEFAULT_AVATAR = "ProfessorDog.jpg"

# Pings. @everyone and @here go through on purpose. Any other @someone is
# sent as plain text and wakes nobody: an anonymous ping aimed at a person is
# the one thing this relay refuses to carry. The exception is the names
# listed here, which exist so a server can make a bot of its own summonable.
# Entries are name:id - the name is what people type after an @, the id is
# what Discord needs. Text from the form carries no mention markup, so the
# bot rewrites @name and @name#0000 into a real mention for these names only.
# A name listed under both goes to the user.
PING_USERS = _optional_pairs("ANON_PING_USERS")
PING_ROLES = _optional_pairs("ANON_PING_ROLES")

MENTIONS = discord.AllowedMentions(
    everyone=True,
    users=[discord.Object(id=i) for i in PING_USERS.values()],
    roles=[discord.Object(id=i) for i in PING_ROLES.values()],
    replied_user=False,
)

_PING_PATTERNS = [
    (re.compile(rf"(?<!\w)@{re.escape(n)}(?:#\d{{4}})?(?![\w#])", re.IGNORECASE), f"<@{i}>")
    for n, i in PING_USERS.items()
] + [
    (re.compile(rf"(?<!\w)@{re.escape(n)}(?![\w#])", re.IGNORECASE), f"<@&{i}>")
    for n, i in PING_ROLES.items()
]


def _link_pings(text: str) -> str:
    """Turn @name for the listed names into real mentions, nothing else."""
    for pattern, markup in _PING_PATTERNS:
        text = pattern.sub(markup, text)
    return text


# Discord caps a message at 2000 characters. The form below refuses anything
# longer, so the limit is enforced by Discord's own client before the text
# ever reaches this program.
MAX_MESSAGE_CHARS = min(_optional_int("MAX_MESSAGE_CHARS", 2000), 2000)
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
# Every post is signed "Human 042", with a picture, so readers can tell one
# sender's messages apart from another's within a day, and nothing more.
# Neither says anything about who you are: both are dealt from shuffled
# decks, so no two people share a number, and neither reveals who wrote
# first.
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
    """The day's secret, the two decks, and what has been dealt so far."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._secret = secrets.token_bytes(32)
        self._numbers = list(range(1000))
        self._avatars = list(AVATARS)
        rng = secrets.SystemRandom()
        rng.shuffle(self._numbers)
        rng.shuffle(self._avatars)
        self._dealt: dict[bytes, tuple[int, str]] = {}

    def deal(self, user_id: int) -> tuple[int, str] | None:
        """Today's number and avatar for this sender, dealt on first sight.

        Returns None only once all thousand numbers are taken, which would
        take a thousand different senders in one day. Refusing beats clashing.
        The avatars run out far sooner; from then on it is DEFAULT_AVATAR.
        """
        digest = hmac.digest(self._secret, str(user_id).encode(), "sha256")
        hand = self._dealt.get(digest)
        if hand is None:
            if not self._numbers:
                return None
            avatar = self._avatars.pop() if self._avatars else DEFAULT_AVATAR
            hand = (self._numbers.pop(), avatar)
            self._dealt[digest] = hand
        return hand


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
_webhook: discord.Webhook | None = None
_button_id: int | None = None
_button_lock = asyncio.Lock()
BUTTON_TIMEOUT = 30  # seconds to wait on Discord before a placement counts as failed
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
    # Two posts landing in the same instant would otherwise both delete the
    # same old button and both post a new one, leaving one orphaned forever.
    # The timeout stops a hung request at Discord's end from holding that
    # lock for good; a placement that fails or times out is forgotten and
    # the keeper below tries again.
    async with _button_lock:
        try:
            async with asyncio.timeout(BUTTON_TIMEOUT):
                if _button_id is not None:
                    try:
                        await _relay_channel.get_partial_message(_button_id).delete()
                    except discord.HTTPException:
                        pass
                posted = await _relay_channel.send(view=AnonButton())
            _button_id = posted.id
        except (discord.HTTPException, TimeoutError):
            _button_id = None
            emit("could not post the button - Discord refused it, timed out, or the bot lacks Send Messages")


@tasks.loop(minutes=2)
async def _keeper() -> None:
    """Put the button back if a placement failed.

    A bad minute at Discord's end during a post would otherwise leave the
    channel with no button, and so no way to post, until midnight or a
    restart. Nothing to do while the button is known to be there.
    """
    if _button_id is None:
        await _place_button()


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

        if _relay_channel is None or _webhook is None:
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

        content = _link_pings(self.field.component.value.strip())
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
        hand = _numbering.deal(interaction.user.id)
        if hand is None:
            _count("rejected_no_numbers")
            await _whisper(interaction, TEXT_NO_NUMBERS)
            return
        number, avatar = hand

        # From here on, only `number`, `avatar` and `content` are in play.
        # Nothing downstream of this point has access to the sender.
        # Posted through the webhook rather than as a reply to the interaction,
        # which is what keeps the sender's name off it. The display name is the
        # number and the picture is the day's avatar. suppress_embeds stops
        # links unfurling into preview cards, so a post appears exactly as it
        # was typed and nothing else.
        #
        # Waiting for confirmation makes Discord say the post exists before the
        # button is moved below it. Without it the request is merely accepted,
        # and the button can land first.
        #
        # MENTIONS decides who a post can wake: everyone, plus the listed
        # names that _link_pings turned into real mentions above. Discord
        # enforces the list server side, so an @someone that is not on it
        # stays plain text no matter what the post contains.
        try:
            await _webhook.send(
                content,
                username=f"Human {number:03d}",
                avatar_url=f"{AVATAR_BASE}/{quote(avatar)}" if AVATAR_BASE else discord.utils.MISSING,
                allowed_mentions=MENTIONS,
                suppress_embeds=True,
                wait=True,
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
    global _relay_channel, _webhook, _started

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

    # Built here rather than at import because it borrows the client's HTTP
    # session, which only exists once logged in. A URL pasted from the wrong
    # channel would quietly relay posts somewhere else, so it is checked
    # against the channel before anything is sent. Fetched with the webhook's
    # own token, which needs no extra bot permission.
    try:
        hook = await discord.Webhook.from_url(WEBHOOK_URL, client=client).fetch(prefer_auth=False)
    except (ValueError, discord.HTTPException):
        emit("startup: ANON_WEBHOOK_URL is not a usable webhook - shutting down")
        await client.close()
        return
    if hook.channel_id != CHANNEL_ID:
        emit("startup: ANON_WEBHOOK_URL does not point at ANON_CHANNEL_ID - shutting down")
        await client.close()
        return

    _relay_channel = channel
    _webhook = hook

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
        _keeper.start()
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
