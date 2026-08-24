"""
audit this by just reading it, its like 400 lines and most of them are comments.
or paste it into chatgpt and ask it if it keeps any logs. who cares nerds idgaf

---

If you are editing this, three rules keep it honest:

  1. No sender identifier gets stored, hashed, counted or logged. Ever.
  2. Nothing is written to disk. No database, no queue, no filesystem imports.
  3. emit() is the only thing that prints, and it only takes fixed strings.

Break one of those and it is a different bot with the same name.
"""

from __future__ import annotations

import os
import sys
import time

import discord
from discord import app_commands

# --------------------------------------------------------------------------
# User-facing strings
#
# Every string a sender can ever see is collected here so that the message
# handler below stays short enough to read in one pass.
# --------------------------------------------------------------------------

TEXT_DELIVERED = (
    "Posted anonymously. Nothing linking you to it exists on the server side.\n"
    "This DM is the only record that you sent it - delete it if that matters to you."
)
TEXT_EMPTY = "Nothing to post - send some text."
TEXT_NO_ATTACHMENTS = (
    "This bot relays text only. Images and files carry metadata that can identify "
    "you, so they are refused rather than forwarded."
)
TEXT_TOO_LONG = "Too long: {actual} characters, and the limit is {limit}. Split it up and send again."
TEXT_RATE_LIMITED = "The relay is busy right now. Wait a moment and send it again."
TEXT_PAUSED = "The relay is paused by the moderators. Your message was not posted."
TEXT_NOT_MEMBER = "You are not currently in the server this bot posts to."
TEXT_FAILED = "Discord rejected the post, so it did not go through. Try again."
TEXT_UNAVAILABLE = "The relay channel is not reachable right now. Your message was not posted."

RELAY_FOOTER = "anonymous"
RELAY_COLOUR = 0x2B2D31


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


def _optional_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


TOKEN = _require("DISCORD_TOKEN")
GUILD_ID = _require_id("ANON_GUILD_ID")
CHANNEL_ID = _require_id("ANON_CHANNEL_ID")

MAX_MESSAGE_CHARS = _optional_int("MAX_MESSAGE_CHARS", 4000)
RATE_LIMIT_BURST = _optional_int("RATE_LIMIT_BURST", 5)
RATE_LIMIT_PER_MINUTE = _optional_int("RATE_LIMIT_PER_MINUTE", 12)
REQUIRE_MEMBERSHIP = _optional_flag("REQUIRE_GUILD_MEMBERSHIP", True)

# Injected at build time so the running container can name the source it was
# built from. Meaningless on its own - see the verification section of the
# README for what makes it worth anything.
BUILD_SHA = os.environ.get("GIT_SHA", "unknown")
BUILD_SOURCE = os.environ.get("SOURCE_URL", "unknown")


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
    for each person who has recently sent something, which is exactly the
    state this bot promises not to keep. The trade is that one person sending
    a flood consumes everyone's budget until it refills; the moderator kill
    switch exists for that case.

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
# Client
#
# Intents are the mechanism by which Discord decides what this bot is allowed
# to receive. What is requested here, and what is not, is the load-bearing
# privacy claim of the whole project:
#
#   guilds       - channel topology, so the relay channel can be resolved.
#                  Grants no access to messages or to member lists.
#   dm_messages  - the DMs people send to this bot. Not a privileged intent.
#
#   message_content is NOT requested, and must stay switched off in the
#   Discord developer portal. It is the only mechanism by which a bot can read
#   messages in server channels. Without it, this bot cannot read anything
#   anyone says in the server - not by policy, by API. Discord supplies
#   message content for DMs sent to an app regardless of this intent, which is
#   why the relay still works.
# --------------------------------------------------------------------------

intents = discord.Intents.none()
intents.guilds = True
intents.dm_messages = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

_relay_channel: discord.TextChannel | None = None
_guild: discord.Guild | None = None
_commands_synced = False
_paused = False


def _build_relay(content: str) -> discord.Embed:
    """Wrap the message for posting.

    An embed is used rather than plain text for one specific reason: mentions
    inside an embed description are never resolved into pings, so an
    @everyone in a submission cannot notify the server even if the
    allowed_mentions guard on send() were ever removed. Markdown still renders
    exactly as the sender typed it.
    """
    embed = discord.Embed(description=content, colour=RELAY_COLOUR)
    embed.set_footer(text=RELAY_FOOTER)
    return embed


async def _receipt(message: discord.Message, text: str) -> None:
    """Acknowledge a submission in the sender's own DM channel.

    Failures are swallowed. Some privacy settings block the bot from replying,
    and a sender whose message was relayed but whose receipt bounced is not a
    condition worth recording anywhere.
    """
    try:
        await message.reply(text, mention_author=False)
    except discord.HTTPException:
        pass


async def _is_current_member(user_id: int) -> bool:
    """Confirm the sender is still in the server.

    The identifier is handed straight to the Discord API and is not retained
    past this call. Without the check, anyone who has ever opened a DM with
    the bot could keep posting after leaving or being banned.

    Fails open on transient API errors: an outage at Discord should not
    silently swallow submissions from legitimate members.
    """
    if _guild is None:
        return False
    try:
        await _guild.fetch_member(user_id)
    except discord.NotFound:
        return False
    except discord.HTTPException:
        return True
    return True


@client.event
async def on_message(message: discord.Message) -> None:
    # Guild messages cannot be read without the message content intent, which
    # this bot does not request. Checking anyway means the guarantee does not
    # rest on the portal toggle alone.
    if message.guild is not None or message.author.bot:
        return

    if _relay_channel is None:
        await _receipt(message, TEXT_UNAVAILABLE)
        return

    if _paused:
        _count("rejected_paused")
        await _receipt(message, TEXT_PAUSED)
        return

    if message.attachments or message.stickers:
        _count("rejected_attachment")
        await _receipt(message, TEXT_NO_ATTACHMENTS)
        return

    content = message.content.strip()
    if not content:
        _count("rejected_empty")
        await _receipt(message, TEXT_EMPTY)
        return

    if len(content) > MAX_MESSAGE_CHARS:
        _count("rejected_too_long")
        await _receipt(message, TEXT_TOO_LONG.format(actual=len(content), limit=MAX_MESSAGE_CHARS))
        return

    if REQUIRE_MEMBERSHIP and not await _is_current_member(message.author.id):
        _count("rejected_not_member")
        await _receipt(message, TEXT_NOT_MEMBER)
        return

    if not _rate_limiter.take():
        _count("rejected_rate_limited")
        await _receipt(message, TEXT_RATE_LIMITED)
        return

    # From here on, only `content` is in play. Nothing downstream of this
    # point has access to the sender.
    try:
        await _relay_channel.send(
            embed=_build_relay(content),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException:
        _count("relay_failed")
        await _receipt(message, TEXT_FAILED)
        return

    _count("relayed")
    await _receipt(message, TEXT_DELIVERED)


@client.event
async def on_ready() -> None:
    global _relay_channel, _guild, _commands_synced

    try:
        _guild = await client.fetch_guild(GUILD_ID)
        channel = await client.fetch_channel(CHANNEL_ID)
    except discord.HTTPException:
        emit("startup: could not resolve the guild or the relay channel")
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

    if not _commands_synced:
        guild = discord.Object(id=GUILD_ID)
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)
        _commands_synced = True

    emit(f"ready, build {BUILD_SHA}")


# --------------------------------------------------------------------------
# Moderator commands
# --------------------------------------------------------------------------


def _is_moderator(interaction: discord.Interaction) -> bool:
    user = interaction.user
    return isinstance(user, discord.Member) and user.guild_permissions.manage_messages


@tree.command(name="pause", description="Stop relaying anonymous messages until resumed.")
@app_commands.default_permissions(manage_messages=True)
async def pause(interaction: discord.Interaction) -> None:
    global _paused
    if not _is_moderator(interaction):
        await interaction.response.send_message("Moderators only.", ephemeral=True)
        return
    _paused = True
    emit("relay paused")
    await interaction.response.send_message("Relay paused. Incoming messages are refused, not queued.")


@tree.command(name="resume", description="Resume relaying anonymous messages.")
@app_commands.default_permissions(manage_messages=True)
async def resume(interaction: discord.Interaction) -> None:
    global _paused
    if not _is_moderator(interaction):
        await interaction.response.send_message("Moderators only.", ephemeral=True)
        return
    _paused = False
    emit("relay resumed")
    await interaction.response.send_message("Relay resumed.")


@tree.command(name="status", description="Show relay throughput since the last restart.")
async def status(interaction: discord.Interaction) -> None:
    state = "paused" if _paused else "running"
    tally = ", ".join(f"{k}: {v}" for k, v in sorted(COUNTS.items())) or "nothing yet"
    await interaction.response.send_message(
        f"Relay is **{state}**.\nSince the last restart - {tally}.\n"
        "These are totals only. No per-person figures exist to report.",
        ephemeral=True,
    )


@tree.command(name="attest", description="Show which build of the source this bot is running.")
async def attest(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        f"Source: {BUILD_SOURCE}\n"
        f"Commit: `{BUILD_SHA}`\n\n"
        "This is self-reported, so it proves nothing on its own. Check the "
        "source at that link if you actually care."
    )


if __name__ == "__main__":
    # log_handler=None stops discord.py installing its own logging handler.
    # Without this the library would emit gateway diagnostics of its own and
    # emit() would no longer be the only thing writing to stdout.
    client.run(TOKEN, log_handler=None)
