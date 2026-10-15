"""Discord cards, buttons and alert messages (PRD §5.4)."""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING, Optional

import discord

from zoneinfo import ZoneInfo

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
    "weekly-schedule": "The Sunday digest of the coming week's streams.",
    "waiting-rooms": "Streams found from YouTube waiting rooms. Tap 💜 Add to calendar on the ones you want.",
    "schedule-images": "Streams read from schedule images (/drop). Tap 💜 Add to calendar on the ones you want.",
    "go-live": "💜 Pings when a stream you picked goes live.",
    "surprise-live": "❣️ Pings when a followed streamer goes live without announcing it.",
    "needs-review": "Schedule entries the bot couldn't read confidently. Confirm, fix or discard them.",
    "bot-health": "Problems the bot runs into, and when they're fixed.",
}
SCHEDULE_FILENAME = "schedule.jpg"  # name of the schedule image attached to image cards
SOURCE_LABELS = {"waiting_room": "Waiting room", "image": "Schedule image", "both": "Schedule image + waiting room"}


def card_channel(entry: Entry) -> str:
    """Waiting-room cards and schedule-image cards live in separate channels."""
    return "waiting-rooms" if entry.source == "waiting_room" else "schedule-images"


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
    elif entry.image_path:
        embed.set_thumbnail(url=f"attachment://{SCHEDULE_FILENAME}")  # the card message carries the image
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
    tz, _, problem = resolve_timezone(entry.orig_tz)
    if problem:
        return None
    local = entry.start_utc.astimezone(tz)
    return f"*{local:%a} {local:%I:%M %p}".replace(" 0", " ") + f" {entry.orig_tz} as written*"


# --- #needs-review (X-3) ---

def review_embed(entry: Entry, streamer: Streamer, issues: list[str], state: str = "open") -> discord.Embed:
    """A stream the bot wasn't sure about: shows its best reading so one tap can confirm it.

    The reasons (issues) are logged, not shown: the owner only needs the result to compare with the image."""
    titles = {"open": "🔍 Is this right?",
              "confirmed": "✅ Confirmed: added to #schedule-images (use ✏️ Edit or 🗑 Delete on its card)",
              "discarded": "🗑️ Discarded"}
    stream = f"🤝 COLLAB · {entry.title}" if entry.is_collab else entry.title
    lines = [f"**{stream}**",
             f"📅 **{discord.utils.format_dt(entry.start_utc, 'F')}** ({discord.utils.format_dt(entry.start_utc, 'R')})"]
    written = written_time(entry)
    if written:
        lines.append(written)
    embed = discord.Embed(title=titles[state], description="\n".join(lines),
                          color=AMBER if state == "open" else (GREY if state == "discarded" else PURPLE))
    embed.set_author(name=streamer.name)
    if state == "open":
        embed.set_footer(text="✓ Confirm if it matches the image · ✏️ Edit to fix it · Discard if it isn't a stream")
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
        await interaction.response.send_modal(EditEntryModal(entry, bot.config.owner_tz))


EDIT_TIMEZONES = [  # (label shown, value understood by resolve_timezone)
    ("PT · US Pacific", "America/Los_Angeles"), ("MT · US Mountain", "America/Denver"),
    ("CT · US Central", "America/Chicago"), ("ET · US Eastern", "America/New_York"),
    ("UTC / GMT", "UTC"), ("UK · London", "Europe/London"), ("Central Europe · Berlin, Paris", "Europe/Berlin"),
    ("JST · Japan", "Asia/Tokyo"), ("KST · Korea", "Asia/Seoul"), ("Taiwan · Taipei", "Asia/Taipei"),
    ("China · Shanghai", "Asia/Shanghai"), ("SGT · Singapore", "Asia/Singapore"),
    ("PHT · Philippines", "Asia/Manila"), ("Indonesia · Jakarta", "Asia/Jakarta"),
    ("AEST · Sydney", "Australia/Sydney"), ("NZ · Auckland", "Pacific/Auckland"),
]


def _tz_value(label: str) -> str:
    """The dropdown value matching a stored timezone label like 'EDT' or 'Asia/Tokyo'."""
    tz, _, _ = resolve_timezone(label)
    return getattr(tz, "key", None) or label


class EditEntryModal(discord.ui.Modal, title="Fix this stream"):
    """Date and title are typed; hour, minute and timezone are picked from dropdowns."""

    def __init__(self, entry: Entry, owner_tz: str):
        """The timezone dropdown starts on the one written on the image, or the owner's if none was printed."""
        super().__init__(timeout=600)
        self.entry_id = entry.id
        current_tz = _tz_value(entry.orig_tz or owner_tz)
        tz, _, _ = resolve_timezone(current_tz)
        local = entry.start_utc.astimezone(tz)

        self.day = discord.ui.TextInput(default=f"{local:%Y-%m-%d}", min_length=10, max_length=10)
        self.hour = discord.ui.Select(options=[
            discord.SelectOption(label=f"{h:02d}  ({(h % 12) or 12} {'AM' if h < 12 else 'PM'})", value=f"{h:02d}",
                                 default=(h == local.hour)) for h in range(24)])
        minutes = sorted({0, 15, 30, 45, local.minute})
        self.minute = discord.ui.Select(options=[
            discord.SelectOption(label=f":{m:02d}", value=f"{m:02d}", default=(m == local.minute)) for m in minutes])
        zones = list(EDIT_TIMEZONES)
        if current_tz not in [v for _, v in zones]:
            zones.insert(0, (f"As written · {current_tz}", current_tz))
        self.tz = discord.ui.Select(options=[
            discord.SelectOption(label=label[:100], value=value, default=(value == current_tz))
            for label, value in zones[:25]])
        self.name = discord.ui.TextInput(default=entry.title[:200], max_length=200)

        self.add_item(discord.ui.Label(text="Date (YYYY-MM-DD)", component=self.day))
        self.add_item(discord.ui.Label(text="Start hour", component=self.hour))
        self.add_item(discord.ui.Label(text="Minute", component=self.minute))
        self.add_item(discord.ui.Label(text="Timezone the time is in", component=self.tz))
        self.add_item(discord.ui.Label(text="Title", component=self.name))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        bot: StreamWatch = interaction.client  # type: ignore[assignment]
        fixed = ReadEntry(self.day.value.strip(), f"{self.hour.values[0]}:{self.minute.values[0]}",
                          self.tz.values[0], self.name.value.strip())
        start, label, problems = to_utc(fixed)
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
        await interaction.response.send_modal(EditEntryModal(entry, bot.config.owner_tz))


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


# --- #schedule-images: one table message per dropped schedule ---

def _schedule_line(entry: Entry, partner_names: list[str]) -> str:
    title = f"**{entry.title}**" + (f" · *{entry.subtitle}*" if entry.subtitle else "")
    when_text = f"**{discord.utils.format_dt(entry.start_utc, 'F')}**"
    if entry.status == "cancelled":
        return f"~~{when_text}: {title}~~ ✖ cancelled"
    line = f"{when_text}: {title}"
    if entry.picked:
        line += " 💜"
    if entry.status == "pending_review":
        line += " ⏳ *check in #needs-review*"
    if entry.is_collab:
        line += "\n　🤝 " + (f"*with {', '.join(partner_names)}*" if partner_names else "*collab*")
    return line


def schedule_embed(streamer: Streamer, entries: list[Entry], names: dict[int, list[str]],
                   has_image: bool, source_url: Optional[str] = None) -> discord.Embed:
    """The summary table: one line per stream in the viewer's local time, title and subtitle, collab partners."""
    lines = [_schedule_line(e, names.get(e.id, [])) for e in entries] or ["*No upcoming streams from this schedule.*"]
    embed = discord.Embed(title=f"📅 {streamer.name} · schedule", description="\n\n".join(lines)[:4096],
                          color=PURPLE if any(e.picked for e in entries) else GREY, url=source_url)
    if has_image:
        embed.set_image(url=f"attachment://{SCHEDULE_FILENAME}")
    footer = "Times are in your timezone · 💜 = on your calendar"
    if source_url:
        footer = "From their YouTube post (title links to it) · " + footer
    embed.set_footer(text=footer)
    return embed


def _options(entries: list[Entry], tz: str, picked_default: bool = False) -> list[discord.SelectOption]:
    """Dropdown labels can't use Discord's local-time stamps, so they're written in the owner's timezone."""
    zone = ZoneInfo(tz)

    def label(e: Entry) -> str:
        local = e.start_utc.astimezone(zone)
        return f"{local:%a %d %b} {local:%I:%M %p}".replace(" 0", " ") + f" · {e.title}"
    return [discord.SelectOption(label=label(e)[:100], value=str(e.id), default=picked_default and e.picked)
            for e in entries][:25]


def schedule_view(schedule_id: int, entries: list[Entry], tz: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    pickable = [e for e in entries if e.status == "confirmed"]
    if pickable:
        view.add_item(ScheduleCalendarSelect(schedule_id, pickable, tz))
        view.add_item(ScheduleEditSelect(schedule_id, pickable, tz))
        view.add_item(ScheduleDeleteSelect(schedule_id, pickable, tz))
    return view


class _OwnerSelect(_OwnerButton):
    """Same owner check and restart survival as the buttons; entry_id holds the schedule id here."""

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Select, match: re.Match):
        bot: StreamWatch = interaction.client  # type: ignore[assignment]
        schedule_id = int(match["id"])
        return cls(schedule_id, [e for e in bot.db.schedule_entries(schedule_id) if e.status == "confirmed"],
                   bot.config.owner_tz)

    @staticmethod
    def chosen(interaction: discord.Interaction) -> list[int]:
        return [int(v) for v in interaction.data.get("values", [])]


class ScheduleCalendarSelect(_OwnerSelect, discord.ui.DynamicItem[discord.ui.Select],
                             template=r"sw:sched-cal:(?P<id>\d+)"):
    def __init__(self, schedule_id: int, entries: list[Entry], tz: str):
        options = _options(entries, tz, picked_default=True)
        super().__init__(schedule_id, discord.ui.Select(
            custom_id=f"sw:sched-cal:{schedule_id}", placeholder="💜 Add to calendar… (tick streams, untick to remove)",
            min_values=0, max_values=max(1, len(options)), options=options or [discord.SelectOption(label="-")]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: StreamWatch = interaction.client  # type: ignore[assignment]
        await interaction.response.defer()
        problems = await bot.set_schedule_picks(self.entry_id, set(self.chosen(interaction)))
        if problems:
            await interaction.followup.send("\n".join(problems), ephemeral=True)


class ScheduleEditSelect(_OwnerSelect, discord.ui.DynamicItem[discord.ui.Select],
                         template=r"sw:sched-edit:(?P<id>\d+)"):
    def __init__(self, schedule_id: int, entries: list[Entry], tz: str):
        super().__init__(schedule_id, discord.ui.Select(
            custom_id=f"sw:sched-edit:{schedule_id}", placeholder="✏️ Edit a stream…",
            options=_options(entries, tz) or [discord.SelectOption(label="-")]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: StreamWatch = interaction.client  # type: ignore[assignment]
        entry = bot.db.get_entry(self.chosen(interaction)[0])
        if entry is None or entry.status != "confirmed":
            await interaction.response.send_message("That stream can't be edited any more.", ephemeral=True)
            return
        await interaction.response.send_modal(EditEntryModal(entry, bot.config.owner_tz))


class ScheduleDeleteSelect(_OwnerSelect, discord.ui.DynamicItem[discord.ui.Select],
                           template=r"sw:sched-del:(?P<id>\d+)"):
    def __init__(self, schedule_id: int, entries: list[Entry], tz: str):
        super().__init__(schedule_id, discord.ui.Select(
            custom_id=f"sw:sched-del:{schedule_id}", placeholder="🗑 Delete a stream…",
            options=_options(entries, tz) or [discord.SelectOption(label="-")]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: StreamWatch = interaction.client  # type: ignore[assignment]
        await interaction.response.defer()
        problem = await bot.delete_entry(self.chosen(interaction)[0])
        await bot.refresh_schedule(self.entry_id)
        if problem:
            await interaction.followup.send(problem, ephemeral=True)
