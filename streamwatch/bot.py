"""StreamWatch Discord bot. M1: /follow /unfollow /list /pause /resume. M2: waiting rooms and go-live alerts.

Run with:  python -m streamwatch.bot
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

from .config import Config, load_config
from .db import PAUSED_FOREVER, Database, Entry, Streamer
from .parsing import CHANNEL_URL_EXAMPLE, parse_channel_url, parse_duration, suggest_timezones, valid_timezone
from .tracker import Cancelled, Event, NewWaitingRoom, Rescheduled, SurpriseLive, WentLive, apply_update
from .ui import CHANNELS, PickButton, UnpickButton, card_embed, card_view, change_notice, live_alert
from .youtube import YouTubeError, fetch_rss_video_ids, fetch_videos, resolve_channel

log = logging.getLogger("streamwatch")


class OwnerOnlyTree(app_commands.CommandTree):
    """Single-user bot: only the owner may run commands."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.client.config.owner_id:
            return True
        await interaction.response.send_message("This bot only takes commands from its owner.", ephemeral=True)
        return False

    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CheckFailure):
            return
        log.exception("Command failed", exc_info=error)
        message = "⚠️ Something went wrong. Check the bot logs."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


class StreamWatch(discord.Client):
    def __init__(self, config: Config):
        super().__init__(intents=discord.Intents.default())
        self.config = config
        self.db = Database(config.db_path)
        self.tree = OwnerOnlyTree(self)
        self.guild = discord.Object(id=config.guild_id)
        self.http_session: Optional[aiohttp.ClientSession] = None
        self._was_paused = self.db.is_paused()
        self.channels: dict[str, discord.TextChannel] = {}
        self.poll_lock = asyncio.Lock()
        self.channels_lock = asyncio.Lock()
        self.poll_count = 0
        self.youtube_ok = True

    async def setup_hook(self) -> None:
        self.http_session = aiohttp.ClientSession()
        self.add_dynamic_items(PickButton, UnpickButton)
        register_commands(self)
        # Guild-scoped sync makes commands appear instantly in the private server.
        self.tree.copy_global_to(guild=self.guild)
        await self.tree.sync(guild=self.guild)
        self.watch_pause.start()
        self.poll.start()

    async def close(self) -> None:
        if self.http_session:
            await self.http_session.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Logged in as %s; %d streamers followed", self.user, len(self.db.list_streamers()))
        await self.ensure_channels()

    async def ensure_channels(self) -> None:
        """Find the bot's channels by name, creating any that are missing (needs Manage Channels)."""
        async with self.channels_lock:  # on_ready and the poll loop both call this at startup
            await self._ensure_channels()

    async def _ensure_channels(self) -> None:
        guild = self.get_guild(self.config.guild_id)
        if guild is None:
            log.error("Bot isn't in server %s", self.config.guild_id)
            return
        for name, topic in CHANNELS.items():
            channel = discord.utils.get(guild.text_channels, name=name)
            if channel is None:
                try:
                    channel = await guild.create_text_channel(name, topic=topic)
                    log.info("Created #%s", name)
                except discord.Forbidden:
                    log.warning("Missing #%s and no Manage Channels permission to create it", name)
                    continue
            self.channels[name] = channel

    async def send(self, channel_name: str, **kwargs) -> Optional[discord.Message]:
        channel = self.channels.get(channel_name)
        if channel is None:
            log.warning("No #%s channel; dropped message: %s", channel_name, kwargs.get("content"))
            return None
        return await channel.send(**kwargs)

    # --- M2: waiting rooms and go-live (I-3, L-1, L-2) ---

    @tasks.loop(minutes=5)
    async def poll(self) -> None:
        """Every 5 min: check upcoming/live videos. Every 15 min (3rd run): also read RSS for new videos."""
        if self.db.is_paused():
            return
        if len(self.channels) < len(CHANNELS):
            await self.ensure_channels()
            if len(self.channels) < len(CHANNELS):
                log.warning("Skipping checks until all channels exist: %s", ", ".join(CHANNELS))
                return
        await self.run_checks(read_rss=self.poll_count % 3 == 0)
        self.poll_count += 1

    @poll.before_loop
    async def _before_poll(self) -> None:
        await self.wait_until_ready()

    async def run_checks(self, read_rss: bool) -> None:
        async with self.poll_lock:
            try:
                await self._run_checks(read_rss)
            except (YouTubeError, aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.warning("YouTube check failed: %s", e)
                if self.youtube_ok:
                    self.youtube_ok = False
                    await self.send("bot-health", content=f"⚠️ Can't reach YouTube: {e}. I'll keep retrying.")
                return
            if not self.youtube_ok:
                self.youtube_ok = True
                await self.send("bot-health", content="✅ YouTube checks are working again.")

    async def _run_checks(self, read_rss: bool) -> None:
        now = datetime.now(timezone.utc)
        streamers = {s.id: s for s in self.db.list_streamers()}
        owner: dict[str, Streamer] = {}       # video_id -> streamer, for videos new in RSS
        scanned: list[Streamer] = []
        if read_rss:
            for streamer in streamers.values():
                ids = await fetch_rss_video_ids(self.http_session, streamer.channel_id)
                for vid in set(ids) - self.db.known_video_ids(ids):
                    owner[vid] = streamer
                scanned.append(streamer)

        to_check = list(dict.fromkeys(self.db.video_ids_to_poll() + list(owner)))
        infos = await fetch_videos(self.http_session, self.config.youtube_api_key, to_check) if to_check else {}

        events: list[tuple[Streamer, Event]] = []
        for vid in to_check:
            streamer = owner.get(vid)
            if streamer is None:
                known = self.db.get_video(vid)
                streamer = streamers.get(known.streamer_id) if known else None
            if streamer is None:
                continue
            for event in apply_update(self.db, streamer, vid, infos.get(vid), now,
                                      alerts_enabled=streamer.bootstrapped):
                events.append((streamer, event))
        for streamer in scanned:
            if not streamer.bootstrapped:
                self.db.mark_bootstrapped(streamer.id)

        # Post new cards in start-time order so the schedule reads top to bottom.
        events.sort(key=lambda se: se[1].entry.start_utc if isinstance(se[1], NewWaitingRoom) else now)
        for streamer, event in events:
            await self.handle_event(streamer, event)
        log.info("Checked %d videos (rss=%s): %d events", len(to_check), read_rss, len(events))

    async def handle_event(self, streamer: Streamer, event: Event) -> None:
        if isinstance(event, NewWaitingRoom):
            message = await self.send("weekly-schedule", embed=card_embed(event.entry, streamer),
                                      view=card_view(event.entry))
            if message:
                self.db.update_entry(event.entry.id, card_channel_id=message.channel.id, card_message_id=message.id)
        elif isinstance(event, (Rescheduled, Cancelled)):
            await self.refresh_card(event.entry, streamer)
            if event.entry.picked:
                old = event.old_start if isinstance(event, Rescheduled) else None
                await self.send("weekly-schedule", content=change_notice(event.entry, streamer, old))
        elif isinstance(event, WentLive):
            await self.send("go-live", **live_alert(streamer, event.video, self.config.owner_id, surprise=False))
        elif isinstance(event, SurpriseLive):
            await self.send("surprise-live", **live_alert(streamer, event.video, self.config.owner_id, surprise=True))

    async def refresh_card(self, entry: Entry, streamer: Streamer) -> None:
        if not (entry.card_channel_id and entry.card_message_id):
            return
        channel = self.get_channel(entry.card_channel_id)
        if channel is None:
            return
        try:
            await channel.get_partial_message(entry.card_message_id).edit(embed=card_embed(entry, streamer),
                                                                          view=card_view(entry))
        except discord.NotFound:
            log.info("Card for entry %s was deleted; not refreshing", entry.id)

    @tasks.loop(minutes=1)
    async def watch_pause(self) -> None:
        """Announce when a timed /pause runs out (F-2)."""
        paused = self.db.is_paused()
        if self._was_paused and not paused:
            channel_id = self.db.get_setting("pause_channel_id")
            channel = self.get_channel(int(channel_id)) if channel_id else None
            if channel:
                await channel.send("▶️ Pause ended. StreamWatch is running again.")
        self._was_paused = paused

    @watch_pause.before_loop
    async def _wait_ready(self) -> None:
        await self.wait_until_ready()


def register_commands(bot: StreamWatch) -> None:
    tree = bot.tree

    @tree.command(description="Start tracking a YouTube streamer")
    @app_commands.describe(
        url="Channel link, e.g. https://www.youtube.com/@handle",
        home_tz="Streamer's timezone. Start typing a city, e.g. tokyo or new_york",
    )
    async def follow(interaction: discord.Interaction, url: str, home_tz: Optional[str] = None) -> None:
        target = parse_channel_url(url)
        if not target:
            await interaction.response.send_message(
                f"That isn't a YouTube channel link. Use: `{CHANNEL_URL_EXAMPLE}`", ephemeral=True)
            return
        tz = home_tz or bot.config.default_tz
        if not valid_timezone(tz):
            await interaction.response.send_message(
                f"`{tz}` isn't a timezone I know. Use a name like `America/New_York` or `Asia/Tokyo`.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)
        try:
            info = await resolve_channel(bot.http_session, bot.config.youtube_api_key, **target)
        except (YouTubeError, aiohttp.ClientError, TimeoutError) as e:
            log.warning("channels.list failed: %s", e)
            await interaction.followup.send(f"⚠️ Couldn't reach YouTube right now ({e}). Try again in a minute.")
            return
        if not info:
            await interaction.followup.send(f"No YouTube channel found for `{url}`.")
            return

        streamer, created = bot.db.add_streamer(info.channel_id, info.handle, info.title, tz)
        if not created:
            await interaction.followup.send(f"Already following **{streamer.name}**.")
            return
        await interaction.followup.send(
            f"✅ Now following **{streamer.name}** ({streamer.handle or streamer.channel_id}), home timezone `{tz}`. "
            "Checking their upcoming streams now…")
        if not bot.db.is_paused() and len(bot.channels) == len(CHANNELS):
            asyncio.create_task(bot.run_checks(read_rss=True))

    @follow.autocomplete("home_tz")
    async def follow_tz_autocomplete(interaction: discord.Interaction, current: str):
        return [app_commands.Choice(name=tz, value=tz) for tz in suggest_timezones(current)]

    @tree.command(description="Stop tracking a streamer")
    @app_commands.describe(name="Streamer name or @handle")
    async def unfollow(interaction: discord.Interaction, name: str) -> None:
        streamer = bot.db.find_active_streamer(name)
        if not streamer:
            await interaction.response.send_message(f"Not following anyone called `{name}`. Check `/list`.",
                                                    ephemeral=True)
            return
        bot.db.deactivate_streamer(streamer.id)
        await interaction.response.send_message(f"👋 Stopped following **{streamer.name}**.")

    @unfollow.autocomplete("name")
    async def unfollow_autocomplete(interaction: discord.Interaction, current: str):
        current = current.lower()
        return [app_commands.Choice(name=s.name, value=s.name)
                for s in bot.db.list_streamers() if current in s.name.lower()][:25]

    @tree.command(name="list", description="Show followed streamers and whether the bot is paused")
    async def list_cmd(interaction: discord.Interaction) -> None:
        streamers = bot.db.list_streamers()
        embed = discord.Embed(title=f"Following {len(streamers)} streamer{'s' if len(streamers) != 1 else ''}",
                              color=discord.Color.purple())
        if streamers:
            embed.description = "\n".join(
                f"• **{s.name}** · {s.handle or s.channel_id} · `{s.home_tz}`" for s in streamers)
        else:
            embed.description = "Nobody yet. Add one with `/follow`."
        embed.add_field(name="Status", value=pause_status(bot.db), inline=False)
        await interaction.response.send_message(embed=embed)

    @tree.command(description="Pause all checks, digests and alerts")
    @app_commands.describe(duration="Optional, e.g. 30m, 12h, 3d. Leave empty to pause until /resume")
    async def pause(interaction: discord.Interaction, duration: Optional[str] = None) -> None:
        until = None
        if duration:
            delta = parse_duration(duration)
            if not delta:
                await interaction.response.send_message(
                    f"`{duration}` isn't a duration I understand. Try `30m`, `12h`, `3d` or `1w`.", ephemeral=True)
                return
            until = datetime.now(timezone.utc) + delta
        bot.db.pause(until)
        bot.db.set_setting("pause_channel_id", str(interaction.channel_id))
        bot._was_paused = True
        if until:
            await interaction.response.send_message(
                f"⏸️ Paused until {discord.utils.format_dt(until, 'F')} ({discord.utils.format_dt(until, 'R')}). "
                "Calendar events stay as they are.")
        else:
            await interaction.response.send_message(
                "⏸️ Paused until you run `/resume`. Calendar events stay as they are.")

    @tree.command(description="Resume checks, digests and alerts")
    async def resume(interaction: discord.Interaction) -> None:
        if not bot.db.is_paused():
            await interaction.response.send_message("StreamWatch isn't paused.", ephemeral=True)
            return
        bot.db.resume()
        bot._was_paused = False
        await interaction.response.send_message("▶️ Resumed. StreamWatch is running again.")


def pause_status(db: Database) -> str:
    value = db.paused_until()
    if value is None:
        return "▶️ Running"
    if value == PAUSED_FOREVER:
        return "⏸️ Paused until `/resume`"
    until = datetime.fromisoformat(value)
    # Discord renders these timestamps in each viewer's own timezone.
    return f"⏸️ Paused until {discord.utils.format_dt(until, 'f')} ({discord.utils.format_dt(until, 'R')})"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config()
    StreamWatch(config).run(config.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
