"""StreamWatch Discord bot. M1: commands. M2: waiting rooms and go-live alerts. M3: calendar. M4: schedule images.

Run with:  python -m streamwatch.bot
"""

from __future__ import annotations

import asyncio
import io
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

from .config import Config, load_config
from .db import PAUSED_FOREVER, Database, Entry, Streamer
from .collab import short_channel_name
from .gcal import CalendarAuthError, CalendarError, GoogleCalendar, event_description, event_title
from .parsing import CHANNEL_URL_EXAMPLE, parse_channel_url, parse_duration, suggest_timezones, valid_timezone
from .pipeline import NeedsReview, NewImageEntry, TimezoneSuggestion, process_image
from .tracker import Cancelled, Event, NewWaitingRoom, Rescheduled, SurpriseLive, Updated, WentLive, apply_update
from .ui import (CHANNELS, SCHEDULE_FILENAME, ConfirmReviewButton, DeleteCardButton, DiscardReviewButton, EditCardButton,
                 EditReviewButton, KeepTimezoneButton, PickButton, SetTimezoneButton, UnpickButton, card_embed, card_view, change_notice, live_alert,
                 review_embed, review_view, timezone_suggestion)
from .youtube import (YouTubeError, fetch_live_video_id, fetch_rss_video_ids, fetch_videos, resolve_channel,
                      resolve_handle_names)

MAX_IMAGE_BYTES = 5 * 1024 * 1024  # Claude's per-image limit
IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}

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
        self.calendar_ok = True
        self.calendar: Optional[GoogleCalendar] = None

    async def setup_hook(self) -> None:
        self.http_session = aiohttp.ClientSession()
        self.calendar = GoogleCalendar(self.http_session, self.config.google_key_path, self.config.google_calendar_id)
        self.add_dynamic_items(PickButton, UnpickButton, ConfirmReviewButton, EditReviewButton, DiscardReviewButton,
                               SetTimezoneButton, KeepTimezoneButton, EditCardButton, DeleteCardButton)
        self.claude = None
        if self.config.anthropic_api_key:
            import anthropic  # only needed when image reading is turned on
            self.claude = anthropic.AsyncAnthropic(api_key=self.config.anthropic_api_key)
        register_commands(self)
        # Guild-scoped sync makes commands appear instantly in the private server.
        self.tree.copy_global_to(guild=self.guild)
        await self.tree.sync(guild=self.guild)
        self.watch_pause.start()
        self.poll.start()
        self.watch_live_pages.start()

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
        if isinstance(event, (NewWaitingRoom, NewImageEntry)):
            names = await self.partner_names(event.entry)
            message = await self.send("weekly-schedule", embed=card_embed(event.entry, streamer, names),
                                      view=card_view(event.entry), **self.schedule_file(event.entry.image_path))
            if message:
                self.db.update_entry(event.entry.id, card_channel_id=message.channel.id, card_message_id=message.id)
        elif isinstance(event, (Rescheduled, Updated, Cancelled)):
            await self.sync_calendar_change(event.entry)
            await self.refresh_card(self.db.get_entry(event.entry.id), streamer)
            if event.entry.picked and not isinstance(event, Updated):
                old = event.old_start if isinstance(event, Rescheduled) else None
                await self.send("weekly-schedule", content=change_notice(event.entry, streamer, old))
        elif isinstance(event, WentLive):
            await self.send("go-live", **live_alert(streamer, event.video, self.config.owner_id, surprise=False))
        elif isinstance(event, SurpriseLive):
            await self.send("surprise-live", **live_alert(streamer, event.video, self.config.owner_id, surprise=True))

    # --- M4: schedule images (F-3, X-1 to X-5) ---

    async def read_image(self, streamer: Streamer, images: list[tuple[bytes, str, str]], post_text: str = "",
                         post_date: Optional[datetime] = None, post_id: Optional[str] = None):
        """Run the pipeline on one schedule [(bytes, media_type, filename)] and post the results."""
        from . import ocr, reader  # heavy imports, only when an image arrives
        now = datetime.now(timezone.utc)

        async def read(model, imgs, name, tz, pdate, ptext):
            return await reader.read_schedule(self.claude, model, imgs, name, tz, pdate, ptext,
                                              notes=streamer.reading_notes)

        # Keep one picture of the whole schedule (parts stitched together) to show on its cards.
        self.config.images_dir.mkdir(parents=True, exist_ok=True)
        image_path = self.config.images_dir / f"{streamer.id}_{now:%Y%m%d_%H%M%S}.jpg"
        image_path.write_bytes(await asyncio.to_thread(ocr.stitch, [data for data, _, _ in images]))

        result = await process_image(self.db, streamer, [(data, mtype) for data, mtype, _ in images], read,
                                     ocr.read_text,
                                     reader.FIRST_READER, reader.SECOND_OPINION, post_date or now, now,
                                     post_text, post_id, str(image_path))
        cards = sorted((e for e in result.events if not isinstance(e, NeedsReview)),
                       key=lambda e: e.entry.start_utc)
        for event in cards:
            await self.handle_event(streamer, event)
        for event in (e for e in result.events if isinstance(e, NeedsReview)):
            log.info("Needs review: %s at %s (%s)", event.entry.title, event.entry.start_utc, "; ".join(event.issues))
            await self.post_review(event.entry, streamer, event.issues, images)
        if result.suggestion:
            message = timezone_suggestion(result.suggestion.streamer, result.suggestion.written,
                                          result.suggestion.iana)
            embed = discord.Embed(description="From this schedule image:", color=discord.Color.light_grey())
            embed.set_image(url=f"attachment://{SCHEDULE_FILENAME}")
            await self.send("bot-health", embed=embed, **message, **self.schedule_file(str(image_path)))
        return result

    @staticmethod
    def schedule_file(image_path: Optional[str]) -> dict:
        """send() kwargs attaching the saved schedule image, which embeds show as attachment://schedule.jpg."""
        if image_path and Path(image_path).exists():
            return {"file": discord.File(image_path, filename=SCHEDULE_FILENAME)}
        return {}

    async def post_review(self, entry: Entry, streamer: Streamer, issues: list[str],
                          images: list[tuple[bytes, str, str]]) -> None:
        names = [f"part{i + 1}_{filename}" for i, (_, _, filename) in enumerate(images)]
        files = [discord.File(io.BytesIO(data), filename=name) for (data, _, _), name in zip(images, names)]
        embed = review_embed(entry, streamer, issues)
        embed.set_image(url=f"attachment://{names[0]}")  # the other parts show as attachments below
        message = await self.send("needs-review", embed=embed, view=review_view(entry.id), files=files)
        if message:
            self.db.update_entry(entry.id, review_message_id=message.id)

    async def confirm_review(self, entry_id: int, message: Optional[discord.Message]) -> None:
        entry = self.db.get_entry(entry_id)
        if entry is None or entry.status != "pending_review":
            return
        entry = self.db.update_entry(entry_id, status="confirmed")
        streamer = self.db.get_streamer(entry.streamer_id)
        await self.handle_event(streamer, NewImageEntry(entry))
        if message:
            embed = review_embed(entry, streamer, [], state="confirmed")
            embed.set_image(url=message.embeds[0].image.url if message.embeds and message.embeds[0].image else None)
            await message.edit(embed=embed, view=None)

    async def discard_review(self, entry_id: int, message: Optional[discord.Message]) -> None:
        entry = self.db.get_entry(entry_id)
        if entry is None or entry.status != "pending_review":
            return
        entry = self.db.update_entry(entry_id, status="discarded")
        if message:
            await message.edit(embed=review_embed(entry, self.db.get_streamer(entry.streamer_id), [],
                                                  state="discarded"), view=None, attachments=[])

    async def edit_entry(self, entry_id: int, start: datetime, title: str, orig_tz: Optional[str]) -> None:
        """Fix a confirmed image entry from its card: move the calendar event too, then refresh the card."""
        entry = self.db.get_entry(entry_id)
        if entry is None or entry.status != "confirmed":
            return
        moved = abs(entry.start_utc - start) > timedelta(minutes=1)
        entry = self.db.update_entry(entry_id, start_utc=start, title=title, orig_tz=orig_tz,
                                     **({"previous_start": entry.start_utc} if moved else {}))
        await self.sync_calendar_change(entry)
        await self.refresh_card(entry, self.db.get_streamer(entry.streamer_id))

    async def delete_entry(self, entry_id: int) -> Optional[str]:
        """Delete a card's stream: remove its calendar event and stop tracking it. Returns a problem, or None."""
        entry = self.db.get_entry(entry_id)
        if entry is None:
            return None
        problem = await self.unpick(entry) if entry.picked else None
        self.db.update_entry(entry_id, status="discarded")
        return problem

    @tasks.loop(minutes=2)
    async def watch_live_pages(self) -> None:
        """L-2b: picked streams with no waiting room: check the /live page from 10 min before start to 90 min after."""
        if self.db.is_paused() or len(self.channels) < len(CHANNELS):
            return
        now = datetime.now(timezone.utc)
        for entry in self.db.picked_unlinked_entries(now - timedelta(minutes=90), now + timedelta(minutes=10)):
            streamer = self.db.get_streamer(entry.streamer_id)
            try:
                video_id = await fetch_live_video_id(self.http_session, streamer.channel_id)
                if not video_id or self.db.get_video(video_id):
                    continue
                infos = await fetch_videos(self.http_session, self.config.youtube_api_key, [video_id])
            except (YouTubeError, aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.warning("Live page check failed for %s: %s", streamer.name, e)
                continue
            async with self.poll_lock:
                for event in apply_update(self.db, streamer, video_id, infos.get(video_id), now):
                    await self.handle_event(streamer, event)

    @watch_live_pages.before_loop
    async def _before_live_pages(self) -> None:
        await self.wait_until_ready()

    async def partner_names(self, entry: Entry) -> list[str]:
        """Collab partners for display: @handles become channel names (looked up once, then cached)."""
        partners = entry.partners or []
        handles = [p for p in partners if p.startswith("@")]
        known = self.db.handle_names(handles)
        missing = [h for h in handles if h.lower() not in known]
        if missing and self.http_session:
            found = await resolve_handle_names(self.http_session, self.config.youtube_api_key, missing)
            self.db.save_handle_names(found)
            known.update(found)
        return [short_channel_name(known.get(p.lower(), p)) if p.startswith("@") else p for p in partners]

    # --- M3: Google Calendar (C-1 to C-4) ---

    async def _calendar_failed(self, error: CalendarError) -> str:
        log.warning("Calendar call failed: %s", error)
        if isinstance(error, CalendarAuthError):
            message = (f"Google Calendar access stopped working ({error}). Check the service account key "
                       "in Google Cloud and google_service_account.json on the server.")
        else:
            message = f"Google Calendar error: {error}"
        if self.calendar_ok:
            self.calendar_ok = False
            await self.send("bot-health", content=f"⚠️ {message}")
        return message

    async def pick(self, entry: Entry) -> Optional[str]:
        """Pick a stream and add its calendar event. Returns a problem message, or None."""
        self.db.set_picked(entry.id, True)
        if entry.gcal_event_id:
            return None
        if not self.calendar.connected:
            return "Picked for go-live alerts. Google Calendar isn't connected yet, so no event was added."
        streamer = self.db.get_streamer(entry.streamer_id)
        calendar_id = self.calendar.calendar_id
        try:
            event_id = await self.calendar.create_event(
                calendar_id, event_title(streamer.name, entry.title, entry.is_collab), entry.start_utc,
                event_description(entry.video_id, await self.partner_names(entry)))
        except CalendarError as e:
            return "⚠️ Picked for go-live alerts, but couldn't add it to your calendar. " + await self._calendar_failed(e)
        self.db.set_calendar_event(entry.id, calendar_id, event_id)
        self.calendar_ok = True
        return None

    async def unpick(self, entry: Entry) -> Optional[str]:
        problem = None
        if entry.gcal_event_id:
            try:
                await self.calendar.delete_event(entry.calendar_id, entry.gcal_event_id)
            except CalendarError as e:
                problem = "⚠️ Removed the pick, but couldn't delete the calendar event. " + await self._calendar_failed(e)
        self.db.set_picked(entry.id, False)
        return problem

    async def sync_calendar_change(self, entry: Entry) -> None:
        """C-3: move or delete the calendar event when a picked stream changes."""
        if not entry.gcal_event_id:
            return
        streamer = self.db.get_streamer(entry.streamer_id)
        try:
            if entry.status == "cancelled":
                await self.calendar.delete_event(entry.calendar_id, entry.gcal_event_id)
                self.db.set_calendar_event(entry.id, None, None)
            else:
                await self.calendar.move_event(
                    entry.calendar_id, entry.gcal_event_id, entry.start_utc,
                    event_title(streamer.name, entry.title, entry.is_collab),
                    event_description(entry.video_id, await self.partner_names(entry)))
        except CalendarError as e:
            await self._calendar_failed(e)

    async def refresh_card(self, entry: Entry, streamer: Streamer) -> None:
        if not (entry.card_channel_id and entry.card_message_id):
            return
        channel = self.get_channel(entry.card_channel_id)
        if channel is None:
            return
        try:
            embed = card_embed(entry, streamer, await self.partner_names(entry))
            await channel.get_partial_message(entry.card_message_id).edit(embed=embed, view=card_view(entry))
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
        return [app_commands.Choice(name=s.name[:100], value=s.channel_id)
                for s in bot.db.list_streamers() if current in s.name.lower()][:25]

    @tree.command(description="Change a streamer's home timezone (used when a schedule image shows none)")
    @app_commands.describe(name="Streamer name or @handle", home_tz="Start typing a city, e.g. tokyo or new_york")
    async def set_home_tz(interaction: discord.Interaction, name: str, home_tz: str) -> None:
        streamer = bot.db.find_active_streamer(name)
        if not streamer:
            await interaction.response.send_message(f"Not following anyone called `{name}`. Check `/list`.",
                                                    ephemeral=True)
            return
        if not valid_timezone(home_tz):
            await interaction.response.send_message(
                f"`{home_tz}` isn't a timezone I know. Use a name like `Asia/Tokyo`.", ephemeral=True)
            return
        bot.db.set_home_tz(streamer.id, home_tz)
        await interaction.response.send_message(
            f"🕒 **{streamer.name}**'s home timezone is now `{home_tz}` (was `{streamer.home_tz}`).")

    @set_home_tz.autocomplete("name")
    async def set_home_tz_name_autocomplete(interaction: discord.Interaction, current: str):
        current = current.lower()
        return [app_commands.Choice(name=f"{s.name[:80]} ({s.home_tz})", value=s.channel_id)
                for s in bot.db.list_streamers() if current in s.name.lower()][:25]

    @set_home_tz.autocomplete("home_tz")
    async def set_home_tz_tz_autocomplete(interaction: discord.Interaction, current: str):
        return [app_commands.Choice(name=tz, value=tz) for tz in suggest_timezones(current)]

    @tree.command(description="Read a schedule image (screenshot from X, or one the bot missed)")
    @app_commands.describe(image="The schedule image (or its first part)", streamer="Whose schedule it is",
                           image2="Optional: second part, if the schedule is split across images",
                           image3="Optional: third part",
                           post_text="Optional: text that came with the post, helps with dates")
    async def drop(interaction: discord.Interaction, image: discord.Attachment, streamer: str,
                   image2: Optional[discord.Attachment] = None, image3: Optional[discord.Attachment] = None,
                   post_text: Optional[str] = None) -> None:
        if bot.claude is None:
            await interaction.response.send_message(
                "Image reading isn't turned on yet: add ANTHROPIC_API_KEY to the server's .env.", ephemeral=True)
            return
        target = bot.db.find_active_streamer(streamer)
        if not target:
            await interaction.response.send_message(f"Not following anyone called `{streamer}`.", ephemeral=True)
            return
        attachments = [a for a in (image, image2, image3) if a is not None]
        for a in attachments:
            if (a.content_type or "").split(";")[0] not in IMAGE_TYPES:
                await interaction.response.send_message(f"`{a.filename}` isn't a PNG, JPG, WEBP or GIF image.",
                                                        ephemeral=True)
                return
            if a.size > MAX_IMAGE_BYTES:
                await interaction.response.send_message(f"`{a.filename}` is over 5 MB; try a smaller screenshot.",
                                                        ephemeral=True)
                return
        await interaction.response.defer(thinking=True)
        images = [(await a.read(), (a.content_type or "").split(";")[0], a.filename or "schedule.png")
                  for a in attachments]
        try:
            result = await bot.read_image(target, images, post_text or "")
        except Exception as e:
            log.exception("Reading image failed")
            await interaction.followup.send(f"⚠️ Couldn't read that image: {e}")
            return
        await interaction.followup.send(f"**{target.name}** · {result.summary()}")

    @drop.autocomplete("streamer")
    async def drop_streamer_autocomplete(interaction: discord.Interaction, current: str):
        current = current.lower()
        return [app_commands.Choice(name=s.name[:100], value=s.channel_id)
                for s in bot.db.list_streamers() if current in s.name.lower()][:25]

    @tree.command(description="Tell the bot how to read a streamer's schedule images (leave note empty to clear)")
    @app_commands.describe(name="Streamer", note="e.g. 'Two times like 6 PM | 9 PM are start and end of one stream'")
    async def reading_note(interaction: discord.Interaction, name: str, note: Optional[str] = None) -> None:
        streamer = bot.db.find_active_streamer(name)
        if not streamer:
            await interaction.response.send_message(f"Not following anyone called `{name}`.", ephemeral=True)
            return
        bot.db.set_reading_notes(streamer.id, note)
        if note:
            await interaction.response.send_message(f"📝 Reading note for **{streamer.name}**:\n> {note}")
        else:
            await interaction.response.send_message(f"📝 Cleared the reading note for **{streamer.name}**.")

    @reading_note.autocomplete("name")
    async def reading_note_autocomplete(interaction: discord.Interaction, current: str):
        current = current.lower()
        return [app_commands.Choice(name=s.name[:100], value=s.channel_id)
                for s in bot.db.list_streamers() if current in s.name.lower()][:25]

    @tree.command(name="list", description="Show followed streamers and whether the bot is paused")
    async def list_cmd(interaction: discord.Interaction) -> None:
        streamers = bot.db.list_streamers()
        embed = discord.Embed(title=f"Following {len(streamers)} streamer{'s' if len(streamers) != 1 else ''}",
                              color=discord.Color.purple())
        if streamers:
            embed.description = "\n".join(
                f"• **{s.name}** · {s.handle or s.channel_id} · `{s.home_tz}`" + (" · 📝" if s.reading_notes else "")
                for s in streamers)
        else:
            embed.description = "Nobody yet. Add one with `/follow`."
        embed.add_field(name="Status", value=pause_status(bot.db), inline=False)
        calendar = (f"📅 `{bot.calendar.calendar_id}`" if bot.calendar.connected else "Not connected yet")
        embed.add_field(name="Calendar", value=calendar, inline=False)
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
