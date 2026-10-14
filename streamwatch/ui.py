"""Discord cards, buttons and alert messages (PRD §5.4)."""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING, Optional

import discord

from .db import Entry, Streamer
from .schedule import ReadEntry, resolve_timezone, to_utc
from .youtube import VideoInfo

if TYPE_CHECKING:
    from .bot import StreamWatch

GREY = discord.Color.from_rgb(128, 132, 142)
PURPLE = discord.Color.from_rgb(155, 89, 182)   # 💜 picked
PINK = discord.Color.from_rgb(233, 30, 99)      # ❣️ surprise
RED = discord.Color.from_rgb(237, 66, 69)
DARK = discord.Color.from_rgb(64, 68, 75)
AMBER = discord.Color.from_rgb(250, 166, 26)   # needs review

CHANNELS = {
    "weekly-schedule": "The week's streams. Tap 💜 Add to calendar on the ones you want to watch.",
    "go-live": "💜 Pings when a stream you picked goes live.",
    "surprise-live": "❣️ Pings when a followed streamer goes live without announcing it.",
    "needs-review": "Schedule entries the bot couldn't read confidently. Confirm, fix or discard them.",
    "bot-health": "Problems the bot runs into, and when they're fixed.",
}
SOURCE_LABELS = {"waiting_room": "Waiting room", "image": "Schedule image", "both": "Schedule image + waiting room"}


def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def thumbnail_url(video_id: str) -> str:
    return f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"


def when(dt: datetime) -> str:
    # Discord renders these in each viewer's own timezone.
    return f"{discord.utils.format_dt(dt, 'F')}\n{discord.utils.format_dt(dt, 'R')}"


def card_embed(entry: Entry, streamer: Streamer, partner_names: list[str] = ()) -> discord.Embed:
    title = f"🤝 COLLAB · {entry.title}" if entry.is_collab else entry.title
    if entry.status == "cancelled":
        color, title = DARK, f"✖ Cancelled · {title}"
    elif entry.previous_start:
        color, title = (PURPLE if entry.picked else GREY), f"⚠ Rescheduled · {title}"
    else:
        color = PURPLE if entry.picked else GREY
    embed = discord.Embed(title=title[:256], color=color, url=watch_url(entry.video_id) if entry.video_id else None)
    # Embeds only colour a thin side stripe, so the picked state also gets a banner line.
    if entry.picked and entry.status != "cancelled":
        banner = "💜 ON YOUR CALENDAR" if entry.gcal_event_id else "💜 PICKED"
        embed.set_author(name=f"{banner} · {streamer.name}")
    else:
        embed.set_author(name=streamer.name)
    if entry.status == "cancelled":
        embed.add_field(name="📅 Was", value=f"~~{discord.utils.format_dt(entry.start_utc, 'F')}~~")
    else:
        value = when(entry.start_utc)
        if entry.previous_start:
            value += f"\n~~{discord.utils.format_dt(entry.previous_start, 'f')}~~"
        written = written_time(entry)
        if written:
            value += f"\n{written}"
        embed.add_field(name="📅 When", value=value)
    embed.add_field(name="📍 Source", value=SOURCE_LABELS.get(entry.source, entry.source))
    if partner_names:
        embed.add_field(name="🤝 With", value=", ".join(partner_names)[:1024], inline=False)
    if entry.video_id:
        embed.set_thumbnail(url=thumbnail_url(entry.video_id))
    if entry.picked and entry.status != "cancelled":
        if entry.gcal_event_id:
            embed.set_footer(text="💜 On your calendar · you'll get a ping when it goes live")
        else:
            embed.set_footer(text="💜 Picked · you'll get a ping when it goes live (not on calendar)")
    return embed


def card_view(entry: Entry) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    if entry.status != "cancelled":
        if entry.picked:
            label = "✓ On calendar" if entry.gcal_event_id else "✓ Picked"
            view.add_item(discord.ui.Button(label=label, style=discord.ButtonStyle.success, disabled=True))
            view.add_item(UnpickButton(entry.id, on_calendar=bool(entry.gcal_event_id)))
        else:
            view.add_item(PickButton(entry.id))
    if entry.video_id:
        view.add_item(discord.ui.Button(label="▶ Open", style=discord.ButtonStyle.link, url=watch_url(entry.video_id)))
    if entry.source == "image" and entry.status == "confirmed":
        # Image readings can be wrong; YouTube's waiting-room time is authoritative, so only these are editable.
        view.add_item(EditCardButton(entry.id))
        view.add_item(DeleteCardButton(entry.id))
    return view


class _OwnerButton:
    """Shared behaviour for card buttons: survive restarts (DynamicItem) and only respond to the owner."""

    def __init__(self, entry_id: int, button: discord.ui.Button):
        super().__init__(button)
        self.entry_id = entry_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match):
        return cls(int(match["id"]))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        bot: StreamWatch = interaction.client  # type: ignore[assignment]
        if interaction.user.id != bot.config.owner_id:
            await interaction.response.send_message("Only the owner can pick streams.", ephemeral=True)
            return False
        return True

    async def set_picked(self, interaction: discord.Interaction, picked: bool) -> None:
        bot: StreamWatch = interaction.client  # type: ignore[assignment]
        entry = bot.db.get_entry(self.entry_id)
        if entry is None:
            await interaction.response.send_message("That stream is no longer tracked.", ephemeral=True)
            return
        await interaction.response.defer()  # calendar calls can take a few seconds
        problem = await (bot.pick(entry) if picked else bot.unpick(entry))
        entry = bot.db.get_entry(entry.id)
        embed = card_embed(entry, bot.db.get_streamer(entry.streamer_id), await bot.partner_names(entry))
        await interaction.edit_original_response(embed=embed, view=card_view(entry))
        if problem:
            await interaction.followup.send(problem, ephemeral=True)


class PickButton(_OwnerButton, discord.ui.DynamicItem[discord.ui.Button], template=r"sw:pick:(?P<id>\d+)"):
    def __init__(self, entry_id: int):
        super().__init__(entry_id, discord.ui.Button(label="💜 Add to calendar", style=discord.ButtonStyle.primary,
                                                     custom_id=f"sw:pick:{entry_id}"))

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.set_picked(interaction, True)


class UnpickButton(_OwnerButton, discord.ui.DynamicItem[discord.ui.Button], template=r"sw:unpick:(?P<id>\d+)"):
    def __init__(self, entry_id: int, on_calendar: bool = True):
        super().__init__(entry_id, discord.ui.Button(label="Remove from calendar" if on_calendar else "Unpick",
                                                     style=discord.ButtonStyle.secondary,
                                                     custom_id=f"sw:unpick:{entry_id}"))

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.set_picked(interaction, False)


def live_alert(streamer: Streamer, video: VideoInfo, owner_id: int, surprise: bool) -> dict:
    """Message kwargs for #go-live (💜 picked) or #surprise-live (❣️ unannounced)."""
    if surprise:
        content = f"❣️ <@{owner_id}> Surprise stream! **{streamer.name}** just went live"
        embed = discord.Embed(title=video.title[:256], url=watch_url(video.video_id), color=PINK,
                              description="Not on any schedule.")
    else:
        content = f"💜 <@{owner_id}> **{streamer.name}** is live now"
        embed = discord.Embed(title=video.title[:256], url=watch_url(video.video_id), color=PURPLE)
    embed.set_author(name=f"🔴 LIVE · {streamer.name}")
    embed.set_image(url=thumbnail_url(video.video_id))
    if video.actual_start:
        embed.add_field(name="Started", value=discord.utils.format_dt(video.actual_start, "R"))
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="▶ Watch", style=discord.ButtonStyle.link, url=watch_url(video.video_id)))
    return {"content": content, "embed": embed, "view": view,
            "allowed_mentions": discord.AllowedMentions(users=[discord.Object(owner_id)])}


def change_notice(entry: Entry, streamer: Streamer, old_start: Optional[datetime]) -> str:
    """C-3 notice for a picked stream that moved or was cancelled."""
    if entry.status == "cancelled":
        return f"✖ **{streamer.name}** cancelled a stream you picked: {entry.title}"
    return (f"⚠ **{streamer.name}** rescheduled a stream you picked: {entry.title}\n"
            f"{discord.utils.format_dt(old_start, 'f')} → **{discord.utils.format_dt(entry.start_utc, 'f')}**")


def written_time(entry: Entry) -> Optional[str]:
    """The time as printed on the schedule image, e.g. 'Fri 9:00 PM PT (as written)'."""
    if not entry.orig_tz or entry.source == "waiting_room":
        return None
    tz, _, problem = resolve_timezone(entry.orig_tz, "UTC")
    if problem:
        return None
    local = entry.start_utc.astimezone(tz)
    return f"*{local:%a} {local:%I:%M %p}".replace(" 0", " ") + f" {entry.orig_tz} as written*"


# --- #needs-review (X-3) ---

def review_embed(entry: Entry, streamer: Streamer, issues: list[str], state: str = "open") -> discord.Embed:
    titles = {"open": "🔍 Please check this stream",
              "confirmed": "✅ Confirmed: added to #weekly-schedule (use ✏️ Edit or 🗑 Delete on its card)",
              "discarded": "🗑️ Discarded"}
    embed = discord.Embed(title=titles[state], color=AMBER if state == "open" else (GREY if state == "discarded" else
                                                                                     PURPLE))
    embed.set_author(name=streamer.name)
    embed.add_field(name="Stream", value=(f"🤝 COLLAB · {entry.title}" if entry.is_collab else entry.title)[:1024],
                    inline=False)
    value = when(entry.start_utc)
    written = written_time(entry)
    if written:
        value += f"\n{written}"
    embed.add_field(name="📅 Read as", value=value)
    if entry.confidence is not None:
        embed.add_field(name="Confidence", value=f"{entry.confidence:.0%}")
    if issues and state == "open":
        embed.add_field(name="Why it needs a check", value="\n".join(f"• {i}" for i in issues)[:1024], inline=False)
    embed.set_footer(text="Compare with the schedule image below. Edit fixes the date, time, timezone or title.")
    return embed


def review_view(entry_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(ConfirmReviewButton(entry_id))
    view.add_item(EditReviewButton(entry_id))
    view.add_item(DiscardReviewButton(entry_id))
    return view


class ConfirmReviewButton(_OwnerButton, discord.ui.DynamicItem[discord.ui.Button], template=r"sw:rv-ok:(?P<id>\d+)"):
    def __init__(self, entry_id: int):
        super().__init__(entry_id, discord.ui.Button(label="✓ Confirm", style=discord.ButtonStyle.success,
                                                     custom_id=f"sw:rv-ok:{entry_id}"))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: StreamWatch = interaction.client  # type: ignore[assignment]
        await interaction.response.defer()
        await bot.confirm_review(self.entry_id, interaction.message)


class DiscardReviewButton(_OwnerButton, discord.ui.DynamicItem[discord.ui.Button], template=r"sw:rv-no:(?P<id>\d+)"):
    def __init__(self, entry_id: int):
        super().__init__(entry_id, discord.ui.Button(label="Discard", style=discord.ButtonStyle.secondary,
                                                     custom_id=f"sw:rv-no:{entry_id}"))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: StreamWatch = interaction.client  # type: ignore[assignment]
        await interaction.response.defer()
        await bot.discard_review(self.entry_id, interaction.message)


class EditReviewButton(_OwnerButton, discord.ui.DynamicItem[discord.ui.Button], template=r"sw:rv-edit:(?P<id>\d+)"):
    def __init__(self, entry_id: int):
        super().__init__(entry_id, discord.ui.Button(label="✏️ Edit", style=discord.ButtonStyle.primary,
                                                     custom_id=f"sw:rv-edit:{entry_id}"))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: StreamWatch = interaction.client  # type: ignore[assignment]
        entry = bot.db.get_entry(self.entry_id)
        if entry is None or entry.status != "pending_review":
            await interaction.response.send_message("This one was already handled.", ephemeral=True)
            return
        await interaction.response.send_modal(EditEntryModal(entry, bot.db.get_streamer(entry.streamer_id)))


class EditEntryModal(discord.ui.Modal, title="Fix this stream"):
    def __init__(self, entry: Entry, streamer: Streamer):
        super().__init__(timeout=600)
        self.entry_id = entry.id
        self.home_tz = streamer.home_tz
        tz_label = entry.orig_tz or streamer.home_tz
        tz, _, _ = resolve_timezone(tz_label, streamer.home_tz)
        local = entry.start_utc.astimezone(tz)
        self.day = discord.ui.TextInput(label="Date (YYYY-MM-DD)", default=f"{local:%Y-%m-%d}", max_length=10)
        self.time = discord.ui.TextInput(label="Start time (24h HH:MM)", default=f"{local:%H:%M}", max_length=5)
        self.tz = discord.ui.TextInput(label="Timezone (e.g. PT, JST, Asia/Tokyo)", default=tz_label, max_length=40)
        self.name = discord.ui.TextInput(label="Title", default=entry.title[:200], max_length=200)
        for item in (self.day, self.time, self.tz, self.name):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        bot: StreamWatch = interaction.client  # type: ignore[assignment]
        fixed = ReadEntry(self.day.value.strip(), self.time.value.strip(), self.tz.value.strip(),
                          self.name.value.strip())
        start, label, problems = to_utc(fixed, self.home_tz)
        if problems:
            await interaction.response.send_message(f"⚠️ {problems[0]}. Nothing was changed; try Edit again.",
                                                    ephemeral=True)
            return
        await interaction.response.defer()
        if bot.db.get_entry(self.entry_id).status == "pending_review":
            bot.db.update_entry(self.entry_id, start_utc=start, title=fixed.title, orig_tz=label)
            await bot.confirm_review(self.entry_id, interaction.message)
        else:
            await bot.edit_entry(self.entry_id, start, fixed.title, label)


class EditCardButton(_OwnerButton, discord.ui.DynamicItem[discord.ui.Button], template=r"sw:card-edit:(?P<id>\d+)"):
    def __init__(self, entry_id: int):
        super().__init__(entry_id, discord.ui.Button(label="✏️ Edit", style=discord.ButtonStyle.secondary,
                                                     custom_id=f"sw:card-edit:{entry_id}"))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: StreamWatch = interaction.client  # type: ignore[assignment]
        entry = bot.db.get_entry(self.entry_id)
        if entry is None or entry.status != "confirmed":
            await interaction.response.send_message("This stream can't be edited any more.", ephemeral=True)
            return
        await interaction.response.send_modal(EditEntryModal(entry, bot.db.get_streamer(entry.streamer_id)))


class DeleteCardButton(_OwnerButton, discord.ui.DynamicItem[discord.ui.Button], template=r"sw:card-del:(?P<id>\d+)"):
    def __init__(self, entry_id: int):
        super().__init__(entry_id, discord.ui.Button(label="🗑 Delete", style=discord.ButtonStyle.danger,
                                                     custom_id=f"sw:card-del:{entry_id}"))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: StreamWatch = interaction.client  # type: ignore[assignment]
        await interaction.response.defer()
        problem = await bot.delete_entry(self.entry_id)
        try:
            await interaction.message.delete()
        except discord.NotFound:
            pass
        if problem:
            await interaction.followup.send(problem, ephemeral=True)


# --- timezone suggestion (asked once per streamer and timezone) ---

class SetTimezoneButton(_OwnerButton, discord.ui.DynamicItem[discord.ui.Button],
                        template=r"sw:tz:(?P<id>\d+):(?P<tz>[A-Za-z_/+\-0-9]+)"):
    def __init__(self, streamer_id: int, iana: str):
        super().__init__(streamer_id, discord.ui.Button(label=f"Set to {iana}", style=discord.ButtonStyle.primary,
                                                        custom_id=f"sw:tz:{streamer_id}:{iana}"))
        self.iana = iana

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match):
        return cls(int(match["id"]), match["tz"])

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: StreamWatch = interaction.client  # type: ignore[assignment]
        streamer = bot.db.get_streamer(self.entry_id)
        bot.db.set_home_tz(streamer.id, self.iana)
        await interaction.response.edit_message(
            content=f"🕒 **{streamer.name}**'s home timezone is now `{self.iana}` (was `{streamer.home_tz}`).",
            view=None)


class KeepTimezoneButton(_OwnerButton, discord.ui.DynamicItem[discord.ui.Button], template=r"sw:tz-keep:(?P<id>\d+)"):
    def __init__(self, streamer_id: int):
        super().__init__(streamer_id, discord.ui.Button(label="Keep", style=discord.ButtonStyle.secondary,
                                                        custom_id=f"sw:tz-keep:{streamer_id}"))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: StreamWatch = interaction.client  # type: ignore[assignment]
        streamer = bot.db.get_streamer(self.entry_id)
        await interaction.response.edit_message(
            content=f"🕒 Kept **{streamer.name}**'s home timezone as `{streamer.home_tz}`.", view=None)


def timezone_suggestion(streamer: Streamer, written: str, iana: str) -> dict:
    view = discord.ui.View(timeout=None)
    view.add_item(SetTimezoneButton(streamer.id, iana))
    view.add_item(KeepTimezoneButton(streamer.id))
    return {"content": (f"🕒 **{streamer.name}**'s schedule uses **{written}**, but their home timezone is "
                        f"`{streamer.home_tz}`. The home timezone is only used for images that show no timezone."),
            "view": view}
