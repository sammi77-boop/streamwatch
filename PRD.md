# PRD: StreamWatch — Personal YouTube Stream Schedule Agent

| | |
|---|---|
| **Owner** | sammi77-boop |
| **Status** | Final v1 (MVP) |
| **Last updated** | 2026-10-13 |
| **Budget** | < $5 / month total (hosting + AI) |

---

## 1. Problem

I follow multiple YouTube streamers and spend too much time tracking their schedules, yet still miss streams. Weekly schedules are posted as images in YouTube community posts at unpredictable times. I have to check each channel, read the schedule, convert time zones, and remember when to tune in.

## 2. Goal

A single-user agent that **finds schedules automatically, sends the weekly schedule on Sunday in Discord, sends a notice whenever a new schedule is posted, lets me pick streams into Google Calendar with one tap, and pings me when a picked stream goes live.**

### Success metrics
| Metric | Target |
|---|---|
| Schedule posts detected within 3 h of being posted | ≥ 90% |
| Extracted stream entries correct (date, time, title) after cross-check | ≥ 95%; every uncertain entry goes to review |
| Go-live alert latency for picked streams | ≤ 5 min after the stream starts |
| Monthly running cost | < $5 |
| My manual effort | ≤ 2 min / week (tapping buttons) |
| Card clarity | From a card alone, I can tell who, when (my time), what, and whether it's a collab in ≤ 5 seconds, without opening YouTube |

## 3. Non-goals (v1)
- Twitch, X ingestion, or any platform other than YouTube
- Members-only streams
- Multi-user support, accounts, or a web dashboard
- VOD, clip, Shorts, or premiere tracking
- WhatsApp (replaced by Discord)

## 4. User stories
1. **Follow:** I run `/follow <channel URL>` and the agent starts tracking that streamer.
2. **Weekly digest:** Every Sunday evening I get a Discord digest of next week's streams, one card per stream with an **Add to calendar** button.
3. **Incremental updates:** If a streamer posts a schedule midweek, I get a short follow-up with just the new cards.
4. **Stream info:** Each card shows the title or topic exactly as the streamer wrote it. **Collabs are marked specially** and list collab partners when known.
5. **Pick:** I tap **Add to calendar** and a Google Calendar event is created in my timezone.
6. **Go-live (picked):** When a stream I picked goes live, I get a 💜 alert with the watch link on Discord, within 5 min.
7. **Surprise stream:** When a followed streamer goes live with something that wasn't on any schedule, I get a **visually distinct** alert (with a ping) in a separate channel.
8. **Changes:** If a picked stream is rescheduled or cancelled, the calendar event is updated or removed automatically and I'm told what changed.
9. **Review:** If the agent isn't confident it read a schedule image correctly, it asks me to confirm or fix the entries instead of guessing.
10. **Travel:** If my timezone changes, the agent asks permission before converting times to the new zone.
11. **Pause:** I run `/pause` to stop the agent (no checks, digests or alerts) and `/resume` to start it again.

## 5. Functional requirements

### 5.1 Streamer management (Discord slash commands)
| ID | Requirement |
|---|---|
| F-1 | `/follow <channel URL> [home_tz]` accepts `youtube.com/@handle` and `youtube.com/channel/UC…` links. It resolves the channel ID with `channels.list` (1 quota unit) and stores it with the streamer's home timezone, which is used when an image shows no timezone. Anything that isn't a valid channel URL is rejected with an example of the right format. |
| F-2 | `/unfollow <name>`, `/list`, `/digest` (send the digest now), `/timezone <IANA tz>` (manual override), `/pause [duration]` and `/resume`. While paused, the agent stops all scraping, polling, digests and alerts. Existing calendar events stay as they are. The bot keeps running so it can receive `/resume`, and `/pause 3d` resumes automatically. |
| F-3 | `/drop <image>` or posting an image in `#schedule-drop` runs it through the extraction pipeline. This is the manual fallback when scraping fails, and also the way to add X schedules by hand. |

### 5.2 Schedule ingestion
| ID | Requirement |
|---|---|
| I-1 | **Community tab scrape:** Every 2–3 h, fetch each followed channel's Posts tab, parse new posts (text + images) and dedupe by post ID. |
| I-2 | **Schedule triage:** A cheap model call classifies each new post as "schedule" or "not schedule". Only schedule posts go on to extraction. |
| I-3 | **Waiting rooms:** Every 15 min, read each channel's RSS feed (free) to find new video IDs, then call `videos.list` (1 quota unit per 50 videos) for `liveBroadcastContent`, `scheduledStartTime`, title and description. |
| I-3a | **Instant waiting rooms (WebSub push):** the bot subscribes each followed channel at YouTube's hub; when a streamer creates or updates a video, the hub pushes it to the bot within about a minute and it's looked up right away. Signed with a shared secret; subscriptions renewed every 2 days. RSS polling every 15 min stays as the backup. (Added 2026-10-14.) |
| I-4 | **Reconciliation:** Match image entries to waiting rooms by date, start time within ±90 min, and title similarity. When a waiting room exists, **its exact start time and title win**. Waiting rooms with no matching image entry are added as entries in their own right. |
| I-4a | **Ignore free-chat rooms:** skip waiting rooms scheduled more than 30 days ahead, or titled "free chat" / "フリーチャット". Many VTubers keep one permanently, with a fake far-future date, and it must never become an entry or match a picked stream. |
| I-5 | Scraping failures (layout change, blocked) raise a `#bot-health` alert. The system keeps running on waiting rooms plus manual drops. |

### 5.3 High-accuracy image extraction (two independent readers)
| ID | Requirement |
|---|---|
| X-1 | **Reader A (vision LLM):** A Claude vision model returns structured JSON: `[{date, start_time, timezone_as_written, title_as_written, is_collab, collab_partners[], confidence}]`. It gets the post text and post date as context, so it can work out "Mon/Tue" to real dates. |
| X-2 | **Reader B (free OCR):** Tesseract OCR reads the same image (upscaled and contrast-normalized first) into raw text. |
| X-3 | **Cross-check:** Every date and time the LLM extracts must also appear in the OCR text, in some normalized form. If any field disagrees, the LLM reports low confidence, or the timezone is ambiguous, the entry goes to **`#needs-review`**. That card shows the image plus the parsed fields, with **Confirm / Edit / Discard** buttons. |
| X-4 | Timezone resolution order: timezone written in the image, then the streamer's `home_tz`, then ask in review. All times are stored in UTC. |
| X-5 | Collab detection uses keywords (collab, ft., w/, x, @mentions) plus the LLM flag. Partners come from @mentions and names in the post text or image. |

### 5.4 Discord delivery
Private server, single member (me). Channels:

| Channel | What it's for |
|---|---|
| `#weekly-schedule` | The Sunday 20:00 digest of the coming Mon–Sun. |
| `#waiting-rooms` | A card for each stream found from a YouTube waiting room. Tapping **Add to calendar** puts it on my calendar. |
| `#schedule-images` | A card for each stream read from a schedule image (`/drop`, later community posts), with the image, Edit and Delete. |
| `#go-live` | 💜 Pings me when a stream I picked goes live |
| `#surprise-live` | ❣️ Pings me when a followed streamer goes live without announcing it |
| `#needs-review` | Schedule entries the bot couldn't read confidently. Confirm, fix or discard them. |
| `#schedule-drop` | Post a schedule image here to have it read, for schedules the bot missed or ones posted only on X |
| `#bot-health` | Problems the bot hits (scraping broke, quota running low), plus timezone-change prompts |

**Stream card contents:** streamer name, local date and time (plus the original timezone if it differs), title or topic as written, **🤝 COLLAB badge plus partner names** when applicable, source (image / waiting room / both), and buttons: **Add to calendar** · **Open** (waiting room link, if any). After a tap, the button changes to **✓ On calendar · Remove**.

### 5.5 Google Calendar
| ID | Requirement |
|---|---|
| C-1 | Tapping **Add** creates an event in my chosen calendar (see C-4). Title: `💜 {Streamer}: {title}`, plus a 🤝 prefix for collabs. Includes the watch link. Each event is a fixed **2-hour block** from the start time. The real end time isn't tracked. |
| C-2 | The event ID is stored, so the agent can update or delete it later. |
| C-3 | **Changes:** If a picked stream's time changes (waiting room or newer schedule post), the event is updated automatically. If it's cancelled or deleted, the event is removed. Both send a notice to `#weekly-schedule` with old and new values. |
| C-4 | Events go to **one specific calendar** in one Google account, set once in the server config (`GOOGLE_CALENDAR_ID`). The calendar is shared with the bot's service account, which can't see anything else. No in-app chooser. |

### 5.6 Go-live detection
| ID | Requirement |
|---|---|
| L-1 | Every 5 min, call `videos.list` for all tracked upcoming video IDs. When `actualStartTime` appears or the status becomes `live`, fire an alert. |
| L-2 | **Picked stream** goes live: alert in `#go-live`. **Unpicked but scheduled:** no alert. **Unscheduled** live video from a followed channel (found through RSS): alert in `#surprise-live` (with an @mention ping, styled differently from picked alerts). |
| L-2a | **Picked stream with no waiting room:** if a followed streamer goes live within ±90 min of a picked entry's start time, that stream is treated as the picked stream. It alerts in `#go-live`, not `#surprise-live`, and the entry is linked to the video. |
| L-2b | **Live-page watch:** from 10 min before a picked entry's start time until it goes live (or 90 min after), check that streamer's `youtube.com/channel/<id>/live` page every 2 min. This catches streams that never had a waiting room, without waiting for the RSS feed. It's free and uses no API quota. |
| L-3 | Each alert fires only once per video. |
| L-4 | Quota stays well under 10,000 units/day (≈300 units/day expected). Never use `search.list`, which costs 100 units per call. |

### 5.7 Timezone tracking
| ID | Requirement |
|---|---|
| T-1 | Once a day, read my Google Calendar's timezone setting (`settings.get timezone`). If Google updates it when I travel, the agent sees the change. |
| T-2 | On a change, post in Discord: *"Your calendar timezone changed Asia/Taipei → Europe/London. Convert displayed times?"* with **Convert / Keep** buttons. Nothing changes until I tap. |
| T-3 | `/timezone` is a manual override. |

## 6. Architecture (MVP)

```
             ┌──────────── cron jobs ─────────────┐
 YouTube ──► │ community scraper (2–3 h)          │
 Posts tab   │ RSS + videos.list (15 min / 5 min) │
             └──────────────┬─────────────────────┘
                            ▼
                 ┌─────────────────────┐   Claude vision (Reader A)
 #schedule-drop ►│  extraction pipeline│◄─ Tesseract OCR (Reader B)
                 └─────────┬───────────┘
                           ▼
                    SQLite (single file)
                           ▼
           ┌────────── Discord bot ──────────┐
           │ digest · cards · buttons · cmds │──► Google Calendar API
           └─────────────────────────────────┘
```

- **Runtime:** one always-on Python process (discord.py + APScheduler), with SQLite as the only store.
- **Hosting:** a small always-on VM, either an Oracle Cloud Always-Free VM ($0) or a ~$4/mo VPS. It must stay up for the Discord gateway connection and the 5-min polling.
- **AI:** a small, cheap Claude model for triage, and a stronger vision model for extraction. About 10 streamers means roughly 10–30 images a week, well under $1/month.
- **Secrets:** Discord bot token, YouTube API key, Anthropic API key, and Google OAuth refresh token, kept in environment variables on the VM.

### Data model (sketch)
- `streamers(id, channel_id, handle, name, home_tz, active)`
- `posts(post_id, streamer_id, posted_at, text, image_url, is_schedule, processed_at)`
- `entries(id, streamer_id, start_utc, title, is_collab, partners_json, source, post_id, video_id, confidence, status[pending_review|confirmed|cancelled])`
- `picks(entry_id, gcal_event_id, picked_at)`
- `alerts(video_id, kind[picked|surprise], sent_at)`
- `settings(key, value)`, which holds the user timezone, last digest time, `paused_until`, and so on

## 7. Risks & mitigations
| Risk | Impact | Mitigation |
|---|---|---|
| Scraping the YouTube Posts tab breaks or gets blocked (it isn't an official API) | No new schedules from images | Health alert in `#bot-health`, waiting-room fallback, manual `#schedule-drop`. Scraping code kept in one replaceable module. |
| Stylized schedule graphics fool the OCR | False disagreements | Disagreements go to review instead of being dropped. Tune the image preprocessing. Track the review rate. |
| Calendar access stops working (service account key deleted, or calendar unshared) | Calendar events stop being added | Alerts still work; the bot posts the problem in `#bot-health` once. (Replaced the OAuth approach, whose tokens expire every 7 days without a published app; see decision 10.) |
| Streamer posts a schedule with no timezone | Times off by hours | `home_tz` set when the streamer is followed, otherwise review |
| YouTube blocks requests from the cloud VM's IP ("confirm you're not a bot") | Community scraping (and possibly the live-page watch) fails completely | **M0 spike** tests this before any other work. If blocked: run the scraper from a home machine, or use a paid hosted scraping API. See §9, decision 6. |
| Members-only streams show up in RSS | Noise | Filter where detectable, otherwise ignore (non-goal) |
| Google Calendar's timezone only changes after I accept its "update time zone?" prompt | Travel goes undetected | `/timezone` manual override (T-3) |
| Free VM gets reclaimed for being idle (Oracle) | Downtime | Health ping to Discord, and an easy redeploy script |

## 8. Milestones
0. **M0 – Feasibility test (1 day):** from the chosen VM, fetch the Posts tab and `/live` page of each followed streamer several times over a day. Record whether YouTube blocks the requests. The result decides where the scraper runs.
1. **M1 – Skeleton:** Discord bot, `/follow` `/list` `/pause` `/resume`, SQLite, deployed to the VM.
2. **M2 – Waiting rooms + go-live:** RSS + `videos.list`, picked and surprise alerts. Already useful at this stage.
3. **M3 – Calendar:** Add/Remove buttons, Google OAuth, automatic updates on change.
4. **M4 – Image pipeline:** manual drop first, then the two readers, the cross-check, and `#needs-review`.
5. **M5 – Community scraper:** automatic ingestion, triage, reconciliation, weekly digest plus follow-ups.
6. **M6 – Timezone tracking** and polish.

## 9. Decisions log
| # | Question | Decision |
|---|---|---|
| 1 | How does the agent detect my timezone? | Read the Google Calendar timezone setting (§5.7), with `/timezone` as the manual fallback |
| 2 | What does the Sunday digest cover? | Sunday 20:00 local, covering the coming Monday–Sunday |
| 3 | How long are calendar events? | Fixed 2-hour block from the start time, with no end-time tracking (C-1) |
| 4 | Which calendar? | One specific calendar in one Google account, set once in config (C-4, changed 2026-10-14). Added `/pause` and `/resume` (F-2). |
| 5 | Should surprise streams ping me? | Yes, with an @mention ping, styled differently from picked alerts |
| 6 | What if YouTube blocks the cloud VM? | Decided by the M0 test. Fallbacks in order: (a) run only the scraper on a home machine (residential IP); (b) a paid hosted scraping API, within budget. No proxy rotation, bot-check spoofing, or logged-in cookies. |
| 7 | Picked streams with no waiting room | Matched to the live stream by time (L-2a), with the live-page watch for fast alerts (L-2b) |
| 8 | When do streamers post schedules? Do they create waiting rooms? | No fixed pattern for either. Keep the Sunday digest plus midweek updates, and don't assume waiting rooms exist. |
| 9 | Permanent free-chat waiting rooms | Found during M0 (@kugaleo_Q's room is dated 2028). Ignored by rule I-4a. |
| 10 | How does the bot get calendar access? | A **service account** (robot Google account) that I share one calendar with. Publishing an OAuth app needs a website and privacy policy on an owned domain, and testing-mode tokens expire every 7 days. (2026-10-14) |
