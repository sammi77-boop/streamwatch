# PRD: StreamWatch — Personal YouTube Stream Schedule Agent

| | |
|---|---|
| **Owner** | sammi77-boop |
| **Status** | v1.4: M0–M5 complete; M6 in progress (`/timezone` complete, cost decision pending) |
| **Last updated** | 2026-10-14 |
| **Budget** | $0 out of pocket: Google Cloud free trial credit ($300, until 2027-01-12) and free tiers |

---

## 1. Problem

I follow multiple YouTube streamers and spend too much time tracking their schedules, yet still miss streams. Weekly schedules are posted as images at unpredictable times. I have to check each channel, read the schedule, convert time zones, and remember when to tune in.

## 2. Goal

A single-user agent that **finds streams from YouTube waiting rooms and community posts automatically, reads schedule images I submit, shows everything in Discord in my own time, lets me pick streams into Google Calendar with one tap, and pings me when a picked stream goes live.**

### Success metrics
| Metric | Target |
|---|---|
| New waiting rooms appear in Discord | Within 15 min of being created |
| Schedule-image streams correct (date, time, title) | ≥ 95%; anything uncertain goes to review, never silently wrong |
| Go-live alert latency for picked streams | ≤ 5 min after the stream starts |
| Monthly running cost to me | $0 (trial credit and free tiers) |
| Manual effort | ≤ 2 min / week (submitting images, selecting streams) |
| Clarity | From a single card or table line, I can identify the streamer, start time (my time), topic, and collab status within 5 seconds |

## 3. Non-goals (v1)
- Any platform other than YouTube
- Members-only streams
- Multi-user support, accounts, or a web dashboard
- VOD, clip, Shorts, or premiere tracking
- WhatsApp (replaced by Discord)
- Tracking streamers' locations or home timezones: times come from waiting rooms or from the timezone printed on schedules

## 4. User stories
1. **Follow:** I run `/follow <channel URL>` and the agent starts tracking that streamer.
2. **Waiting rooms:** When a followed streamer creates a waiting room, a card appears in `#waiting-rooms` with the time in my timezone and an **Add to calendar** button.
3. **Schedule images:** I `/drop` a schedule image (up to 3 parts), or a streamer posts one on YouTube, and I get ONE table in `#schedule-images`: each stream's time in my timezone, title and subtitle, and collab partners, with a dropdown to add streams to my calendar.
4. **Collabs:** Collabs are marked and list partner names, taken from the image, the waiting room title or its description.
5. **Pick:** Adding a stream creates a Google Calendar event in my Stream Watch calendar.
6. **Go-live (picked):** When a stream I picked goes live, I receive a ping in `#go-live` within 5 min.
7. **Surprise stream:** When a followed streamer goes live without any schedule, I receive a distinctly styled ping in `#surprise-live`.
8. **Changes:** If a picked stream is rescheduled or cancelled, the calendar event moves or is removed and I'm told.
9. **Review:** If the agent isn't confident about an image reading, it shows its best guess in `#needs-review` for Confirm / Edit / Discard.
10. **Fix mistakes:** I can edit or delete any stream that came from an image (time, timezone, title).
11. **Pause:** `/pause [duration]` stops all checks and alerts; `/resume` starts them again.
12. **Weekly digest:** Every Sunday evening, a digest of the coming week in `#weekly-schedule`; `/digest` shows the next 7 days on demand.
14. **Live status:** When any listed stream starts, its card shows it is live (without a ping), and later that it has ended.
13. **Travel:** `/timezone` changes my timezone when I travel (dropdown labels, review placement, digest time).

## 5. Functional requirements

### 5.1 Streamer management (Discord slash commands)
| ID | Requirement |
|---|---|
| F-1 | `/follow <channel URL>` accepts `youtube.com/@handle` and `youtube.com/channel/UC…` links and resolves the channel with `channels.list` (1 quota unit). Autocomplete options identify streamers by channel ID, so unusual characters in display names cannot cause mismatches. |
| F-2 | `/unfollow <name>`, `/list` (marks streamers that have a reading note), `/digest` (next 7 days), `/reading_note`, `/pause [duration]` (e.g. `3d`, resumes automatically) and `/resume`. While paused, no checks, cards or alerts are produced; calendar events are kept; the bot stays online to receive `/resume`. |
| F-3 | `/drop image [image2] [image3] streamer [post_text]` reads a schedule image (or one split across up to 3 images, stitched for display). |
| F-4 | `/reading_note <streamer> [note]` stores how to read a streamer's schedules (e.g. Rosco: "6 PM \| 9 PM" is start and end of one stream). Sent to the image reader; overrides the general rules. Empty note clears it. |

### 5.2 Stream discovery (waiting rooms and community posts)
| ID | Requirement |
|---|---|
| I-3 | **Every 15 min**, read each followed channel's RSS feed (free) for new video IDs; if a channel's feed fails, use the API uploads list instead (1 quota unit, includes upcoming waiting rooms). Then `videos.list` (1 quota unit per 50 videos) for status, scheduled start, title and description. Premieres and old VODs are ignored. A streamer's first scan never fires alerts for streams already live. |
| I-4 | **Matching:** image entries and waiting rooms for the same streamer within ±90 min (plus title similarity, or a lone candidate within ±30 min) are the same stream; the waiting room's exact time and title win. |
| I-4a | **Ignore free-chat rooms:** waiting rooms scheduled more than 30 days ahead or titled "free chat" / "フリーチャット". |
| I-5 | Each channel is checked on its own, so one failing doesn't stop the others. A channel that can't be read either way for an hour is reported once in `#bot-health`, and once when it recovers. |
| I-6 | **Community posts:** every 3 h, read each followed channel's Posts tab page (no API exists for posts). A new post with images, not members-only, whose text has a schedule word (schedule, this week, スケジュール, 予定…) or four weekday names is read like `/drop` (all its images as one schedule); its table links to the post. Posts are read once; a streamer's first scan only reads posts from the last 7 days. Failures are reported once, after two consecutive failed reads. |

### 5.3 Reading schedule images
| ID | Requirement |
|---|---|
| X-1 | **Reader:** Gemini 3.8 Flash on Gemini Enterprise Agent Platform (formerly Vertex AI; paid from the Google Cloud credit) returns structured JSON per stream: column date, date, start time, timezone as written, title, subtitle, collab flag and partners, confidence, and one check row. Claude remains a fallback (`IMAGE_READER=claude`). |
| X-2 | **Timezone row:** when the image prints JST, use the JST row with its own date (apply markers like "(FRI)"); otherwise the row matching the column. Read only one other row, to confirm the date. Two titles with "time \| time" are two streams; one title means use the first time; per-streamer reading notes override. |
| X-3 | **Checks (no OCR):** the main row and the check row must be the same moment; the date must match its column unless the check row confirms it; confidence ≥ 80%; timezone recognised; not more than 3 weeks away. A failed check gets one more read; per stream, the reading that passes is kept. Anything still failing goes to `#needs-review`, showing the bot's best reading. |
| X-4 | **Timezone:** only what's printed on the image, converted to my timezone; stored in UTC. If none is printed, the stream goes to review (placed provisionally in my timezone) and Edit's timezone dropdown sets it. |
| X-5 | **Collabs:** for images, the reader's partner list; for waiting rooms, @handles following "Collab w/", "w/", "ft." or "コラボ" in the description, or names following "w/" in the title ("with" alone and "w/o" are not treated as collab markers). @handles are displayed as channel names. |
| X-6 | Past streams (more than 3 h ago) are skipped; a stream already listed is not duplicated; a newer schedule moves an image-only stream only when its reading passes the checks. |

### 5.4 Discord delivery
Private server, single member (me). Channels:

| Channel | What it's for |
|---|---|
| `#waiting-rooms` | One card per stream found from a YouTube waiting room: start time in my timezone, title, 🤝 collab partners, thumbnail, and **💜 Add to calendar** / **▶ Open** buttons. Once added, the card is marked **💜 ON YOUR CALENDAR** and offers **Remove from calendar**. When the stream starts, the card turns red with **🔴 LIVE NOW** and a **▶ Watch** button; afterwards it shows **Ended** with a replay link. |
| `#schedule-images` | One table message per schedule (submitted or from a community post): each stream's start time in my timezone, title and subtitle, 🤝 collab partners, 💜 if on my calendar, ⏳ if awaiting review; the schedule image; and three dropdowns: **💜 Add to calendar** (select to add, deselect to remove), **✏️ Edit a stream** (date, hour, minute, timezone, title) and **🗑 Delete a stream**. |
| `#needs-review` | 🔍 Uncertain readings: the bot's best interpretation, the image, and **✓ Confirm / ✏️ Edit / Discard**. Confirmed streams are added to their table. |
| `#go-live` | 💜 Ping when a stream I picked goes live |
| `#surprise-live` | ❣️ Ping when a followed streamer goes live without a schedule |
| `#weekly-schedule` | 📅 Weekly digest every Sunday 20:00 (`OWNER_TZ`) covering the coming Monday–Sunday: one section per day with time, streamer, title and subtitle, 🤝 partners, 💜 if on my calendar, and a link to each stream's card or table. A missed Sunday run is sent Monday morning; each week is sent once. |
| `#bot-health` | ⚠️ Operational problems (YouTube access, calendar access) and ✅ their recovery |

All times use Discord timestamps, which render in each viewer's own timezone. Dropdown labels cannot, and use `OWNER_TZ` instead.

### 5.5 Google Calendar
| ID | Requirement |
|---|---|
| C-1 | Adding a stream creates an event titled `💜 {Streamer}: {title}` (prefixed `🤝` for collabs), a fixed **2-hour block** from the start time, with the watch link and "Collab with: …" in the description. |
| C-2 | The event ID and calendar are stored, so the event can be moved or deleted later. |
| C-3 | **Changes:** reschedules move the event; cancellations delete it; edits from Discord move it; removing from calendar deletes it. Picked streams that change get a notice in their own channel. |
| C-4 | Events go to **one specific calendar** ("Stream Watch"), set once in the server config (`GOOGLE_CALENDAR_ID`). |
| C-5 | **Access via a service account** (`streamwatch-calendar@your-project.iam.gserviceaccount.com`) that the calendar is shared with ("Make changes to events"). It can't see anything else, and access never expires (decision 10). Calendar failures don't block go-live alerts and are reported once in `#bot-health`. |

### 5.6 Go-live detection
| ID | Requirement |
|---|---|
| L-1 | Every 5 min, `videos.list` for tracked upcoming/live videos. When a video turns live, decide the alert (L-2). |
| L-2 | **Picked:** alert with a ping in `#go-live`. **Scheduled but not picked:** no alert. **Never scheduled:** alert with a ping in `#surprise-live`. |
| L-2a | **Picked image stream with no waiting room:** if the streamer goes live within ±90 min of it, it's treated as that stream (alert in `#go-live`, not `#surprise-live`). |
| L-2b | **Live-page watch:** from 10 min before a picked image-only stream until 90 min after, check the streamer's `/live` page every 2 min (free, no quota). |
| L-3 | Each alert fires only once per video. |
| L-5 | **Live status without a ping:** when any listed stream starts or ends, its card (or table line) is redrawn as live or ended, whether or not it was picked. |
| L-4 | Quota stays well under 10,000 units/day (≈300 units/day expected, up to ≈1,200 while RSS is down). Never `search.list` (100 units per call). |

### 5.7 My timezone
| ID | Requirement |
|---|---|
| T-1 | Most times are Discord timestamps, so they follow my device's timezone automatically, including when I travel. |
| T-2 | My timezone (`OWNER_TZ`, default `America/Los_Angeles`, or the value set with `/timezone`) is used for dropdown labels, for placing timezone-less image streams in review, and for the Sunday digest time. |
| T-3 | `/timezone [tz]` shows or changes my timezone (stored by the bot, overriding `OWNER_TZ`), with city autocomplete. Changing it redraws recent schedule tables. Automatic detection from my Google Calendar setting isn't possible: the service account can't read my account's settings. |

## 6. Architecture

```
 YouTube RSS + videos.list ──(15 min / 5 min)──┐
 YouTube /live page (picked image streams) ────┤
                                               ▼
 /drop image ──► Gemini 3.8 Flash (Agent Platform) ──► checks + time conversion (code)
                                               │
                                     SQLite (one file)
                                               │
                     ┌──────────── Discord bot (discord.py) ────────────┐
                     │ cards · tables · dropdowns · commands · alerts    │──► Google Calendar API
                     └────────────────────────────────────────────────────┘
```

- **Runtime:** one always-on Python process (discord.py with its task loops), systemd service `streamwatch`, SQLite as the only store.
- **Hosting:** Google Cloud e2-micro VM (`streamwatch`, us-west1-b, Ubuntu 24.04 Minimal, 30 GB standard disk), free tier plus trial credit. Polling (no inbound ports).
- **AI:** Gemini 3.8 Flash via Gemini Enterprise Agent Platform (formerly Vertex AI; API "Agent Platform API", `aiplatform.googleapis.com`), paid from the Google Cloud credit (decision 11).
- **Secrets (on the VM only, readable by its user):** Discord bot token, YouTube API key, Google service account key (Calendar + Agent Platform), Anthropic API key (fallback only, unused).

### Data model
- `streamers(id, channel_id, handle, name, active, bootstrapped, reading_notes)` (old `home_tz` column unused)
- `videos(video_id, streamer_id, title, broadcast, scheduled_start, actual_start, ignored, …)`: what YouTube says
- `entries(id, streamer_id, start_utc, title, subtitle, is_collab, partners_json, source[waiting_room|image|both], video_id, status[pending_review|confirmed|cancelled|discarded], orig_tz, schedule_id, image_path, card/review message ids, …)`
- `schedules(id, streamer_id, image_path, channel_id, message_id)`: one table message per drop
- `picks(entry_id, calendar_id, gcal_event_id, picked_at)`
- `alerts(video_id, kind[picked|surprise], sent_at)`
- `handle_names(handle, name)`: cached channel names for collab partners
- `posts(post_id, streamer_id, is_schedule, seen_at)`: community posts already looked at
- `settings(key, value)`: pause state and similar

## 7. Risks & mitigations
| Risk | Impact | Mitigation |
|---|---|---|
| The reader misreads a schedule (e.g. a day off) | Wrong time on a card or calendar | JST row vs. check row must agree; doubtful streams go to review; Edit/Delete on every image stream; waiting rooms correct times automatically |
| Calendar access stops (key deleted, calendar unshared) | No new events | Go-live alerts still work; reported once in `#bot-health` |
| YouTube blocks the VM's IP | No waiting-room updates | M0 showed no blocking; errors reported in `#bot-health` |
| Schedule prints no timezone | Times unknown | Goes to review; timezone picked in Edit |
| Members-only streams show up in RSS | Noise | Ignored (non-goal) |
| Public IPv4 cost after the trial (up to ≈$3.65/month) | Real cost from 2027 | Covered by the trial credit now; confirm by SKU; IPv6-only is free (§10) |

## 8. Milestones
| Milestone | Scope | Status |
|---|---|---|
| M0 – Feasibility | Confirm YouTube serves Posts tabs and `/live` pages to the VM | Complete |
| M1 – Bot foundation | `/follow`, `/unfollow`, `/list`, `/pause`, `/resume`; SQLite; systemd service on the VM | Complete |
| M2 – Waiting rooms and go-live | RSS with API fallback, `videos.list`, waiting-room cards, picked and surprise alerts, reschedule and cancellation handling, free-chat filter | Complete |
| M3 – Calendar | Service account with one shared calendar; add, remove, move and delete events; collab details on events | Complete |
| M4 – Schedule images | `/drop` (up to 3 parts), Gemini 3.8 Flash, reading checks, `#needs-review`, schedule tables with calendar/edit/delete dropdowns, reading notes, waiting-room matching, live-page watch | Complete |
| M5 – Community posts and weekly digest | Community post reading (I-6); weekly digest and `/digest`; live status on cards (L-5) | Complete |
| M6 – Polish | `/timezone` to change my timezone when travelling | Complete |
| | Cost decision for the public IPv4 before the trial ends (§10) | Pending: confirm the IP charge by SKU; IPv6-only test in December if charged |

## 9. Decisions log
| # | Question | Decision |
|---|---|---|
| 1 | How does the agent know my timezone? | Discord timestamps show every time in my device's timezone; `OWNER_TZ` only for dropdown labels and review placement; `/timezone` planned (M6). Reading my Google Calendar setting isn't possible with a service account. (Updated 2026-10-14) |
| 2 | What does the Sunday digest cover? | Sunday 20:00 local, covering the coming Monday–Sunday |
| 3 | How long are calendar events? | Fixed 2-hour block from the start time, with no end-time tracking (C-1) |
| 4 | Which calendar? | One specific calendar ("Stream Watch") in one Google account, set once in config (C-4). Added `/pause` and `/resume` (F-2). |
| 5 | Should surprise streams ping me? | Yes, with an @mention ping, styled differently from picked alerts |
| 6 | What if YouTube blocks the cloud VM? | M0 showed it doesn't. Fallbacks if that changes: run the checks from a home machine, or a paid hosted API. No proxy rotation, bot-check spoofing, or logged-in cookies. |
| 7 | Picked streams with no waiting room | Matched to the live stream by time (L-2a), with the live-page watch for fast alerts (L-2b) |
| 8 | When do streamers post schedules? Do they create waiting rooms? | No fixed pattern for either; don't assume waiting rooms exist. |
| 9 | Permanent free-chat waiting rooms | Found during M0 (@kugaleo_Q's room is dated 2028). Ignored by rule I-4a. |
| 10 | How does the bot get calendar access? | A **service account** that I share one calendar with. Publishing an OAuth app needs a website and privacy policy on an owned domain, and testing-mode tokens expire every 7 days. (2026-10-14) |
| 11 | Which model reads schedule images? | **Gemini 3.8 Flash** via Gemini Enterprise Agent Platform (formerly Vertex AI), billed to the Google Cloud trial credit instead of prepaid Anthropic credit. In a side-by-side test on Rosco's real schedule, 3.8 Flash read every stream and subtitle correctly; 3.5 Flash-Lite put LOVEBYTE.EXE a day early (dropped as "over") and mixed up subtitles. (2026-10-14) |
| 12 | Streamer home timezones? | Removed. Waiting rooms give exact times and schedule images print their timezone (JST first), converted to mine. Images with no timezone go to review. (2026-10-14) |
| 13 | OCR cross-check? | Removed. With Gemini 3.8 Flash, Tesseract mostly flagged correct stylised text; the reader's own timezone rows are the check now. (2026-10-14) |
| 14 | How are schedule images shown? | One table message per schedule with a calendar dropdown, instead of one card per stream: times, title and subtitle, collab partners (English). (2026-10-14) |
| 15 | How fast must waiting rooms appear? | RSS every 15 min is enough; the instant push version was built and parked (§10). (2026-10-14) |
| 16 | What if YouTube's RSS feeds fail? | Fall back to the API uploads list per channel (seen 2026-10-14: every channel's RSS returned 404 for hours). Report a channel only after an hour of failing, once. |
| 17 | How are community posts filtered? | Option A: only posts whose text contains schedule words (or four weekday names) are sent to the image reader; no model call is used just to classify a post. (2026-10-14) |
| 18 | Unpicked streams that go live? | No ping (L-2), but the card turns red and shows it is live (L-5), so live streams are visible without extra notifications. (2026-10-14) |

## 10. Notes (deferred items, not part of the current product)

**Instant waiting-room alerts (WebSub push).** Built, tested and verified on 2026-10-14, then disabled by decision;
waiting rooms are found by RSS polling every 15 minutes (I-3).
- How it works: the bot subscribes each followed channel at YouTube's WebSub hub; when a streamer creates or updates
  a video, the hub pushes it to the bot within about a minute. Pushes are verified with a shared secret;
  subscriptions renew every 2 days. Free (no quota until a pushed video is looked up).
- To turn it back on: add `WEBSUB_ENABLED=1` to the server's `.env`, open TCP port 8080 in the Google Cloud
  firewall (rule `allow-youtube-websub`, ingress, 0.0.0.0/0), and restart the bot.

**Cost decision before 12 Jan 2027.** The Google Cloud free trial ends then; keeping the VM requires upgrading the
billing account. The e2-micro and 30 GB standard disk are always-free. Options for the public IPv4 address
(reviewed 2026-10-14):
- Keep it: $0.005/hour list price (about $3.65/month); whether the free-tier e2-micro is exempt is not stated in
  Google's free-tier documentation. To be confirmed in Billing → Reports grouped by SKU ("External IP Charge").
- Cloud NAT: not cheaper (about $4.60/month: $0.0014/hour per VM plus the gateway's own IPv4 and $0.045/GB).
- IPv6 only: external IPv6 addresses on VMs are free. Requires verifying that Discord, YouTube, Google Calendar and
  the Agent Platform are reachable over IPv6, and SSH access over IPv6 or through the browser. Plan: add IPv6
  alongside IPv4 in December, verify, then remove IPv4.
Anthropic usage is prepaid credit only (keep auto-reload off).
