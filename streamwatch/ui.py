"""Discord cards, buttons and alert messages (PRD §5.4)."""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING, Optional

import discord

from .db import Entry, Streamer
from .youtube import VideoInfo

if TYPE_CHECKING:
    from .bot import StreamWatch

GREY = discord.Color.from_rgb(128, 132, 142)
PURPLE = discord.Color.from_rgb(155, 89, 182)   # 💜 picked
PINK = discord.Color.from_rgb(233, 30, 99)      # ❣️ surprise
RED = discord.Color.from_rgb(237, 66, 69)
DARK = discord.Color.from_rgb(64, 68, 75)

CHANNELS = {
    "weekly-schedule": "The week's streams. Tap 💜 Add to calendar on the ones you want to watch.",
    "go-live": "💜 Pings when a stream you picked goes live.",
    "surprise-live": "❣️ Pings when a followed streamer goes live without announcing it.",
    "bot-health": "Problems the bot runs into, and when they're fixed.",
}


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
        embed.add_field(name="📅 When", value=value)
    embed.add_field(name="📍 Source", value="Waiting room" if entry.source == "waiting_room" else entry.source)
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
            view.add_item(UnpickButton(entry.id))
        else:
            view.add_item(PickButton(entry.id))
    if entry.video_id:
        view.add_item(discord.ui.Button(label="▶ Open", style=discord.ButtonStyle.link, url=watch_url(entry.video_id)))
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
    def __init__(self, entry_id: int):
        super().__init__(entry_id, discord.ui.Button(label="Remove", style=discord.ButtonStyle.secondary,
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
