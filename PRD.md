# PRD: StreamWatch — Personal YouTube Stream Schedule Agent

| | |
|---|---|
| **Owner** | sammi77-boop |
| **Status** | v1.8: M0–M7 complete |
| **Last updated** | 2026-10-15 |
| **Budget** | $0 out of pocket: Google Cloud free trial credit ($300, until 2027-01-12) and free tiers |

---

## 1. Problem

I follow many YouTube streamers. Whenever I want to watch something, I have to click through their channels one by one to see who is live. Planning ahead is even harder: schedules are posted as images, at random times, in other timezones, so I have to read each one, convert the times and remember when to tune in. I spend more time checking than watching, and still miss streams I care about.

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
- Messaging apps other than Discord
- Tracking streamers' locations or home timezones: times come from waiting rooms or from the timezone printed on schedules

## 4. User stories
1. **Follow:** I run `/follow <channel URL>` and the agent starts tracking that streamer.
2. **Waiting rooms:** When a followed streamer creates a waiting room, a card appears in `#waiting-rooms` with the time in my timezone and an **Add to calendar** button.
3. **Schedule images:** I `/drop` a schedule image (up to 3 parts), or a streamer posts one on YouTube, and I get ONE table in `#schedule-images`: each stream's time in my timezone, title and subtitle, and collab partners, with a dropdown to add streams to my calendar.
4. **Collabs:** Collabs are marked and list partner names, taken from the image, the waiting room title or its description.
5. **Pick:** Adding a stream creates a Google Calendar event in my Stream Watch calendar.
6. **Go-live (picked):** When a stream I picked goes live, I receive a ping in `#go-live` within 5 min.
7. **Surprise stream:** When a followed streamer goes live without any schedule, or with a waiting room opened less than an hour ahead, I receive a distinctly styled ping in `#surprise-live`.
8. **Changes:** If a picked stream is rescheduled or cancelled, the calendar event moves or is removed and I'm told.
9. **Tentative streams:** If the agent isn't confident about an image reading, it lists its best guess marked ⏳ tentative instead of asking me. A waiting room, a newer schedule or my edit confirms it, and I'm told if the time was wrong. I'm only asked in `#needs-review` about a tentative stream I put on my calendar that is still unconfirmed shortly before it starts.
10. **Fix mistakes:** I can edit or delete any stream that came from an image (time, timezone, title).
11. **Pause:** `/pause [duration]` stops all checks and alerts; `/resume` starts them again.
12. **Weekly digest:** Every Sunday evening, a digest of the coming week in `#weekly-schedule`; `/digest` shows the next 7 days on demand.
13. **Travel:** `/timezone` changes my timezone when I travel (dropdown labels, review placement, digest time).
14. **Live status:** When any listed stream starts, its card shows it is live (without a ping), and later that it has ended.

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
| I-6 | **Community posts:** every 3 h, read each followed channel's Posts tab page (no API exists for posts). A new post with images, not members-only, whose text has a schedule word (schedule, this week, スケジュール, 予定…), four weekday names, or a short caption of 15 words or fewer (decision 24) is read like `/drop` (all its images as one schedule); its table links to the post. Posts are read once; a streamer's first scan only reads posts from the last 7 days. Failures are reported once, after two consecutive failed reads. |

### 5.3 Reading schedule images
| ID | Requirement |
|---|---|
| X-1 | **Reader:** Gemini 3.8 Flash on Gemini Enterprise Agent Platform (formerly Vertex AI; paid from the Google Cloud credit) returns structured JSON per stream: column date, date, start time, timezone as written, title, subtitle, collab flag and partners, confidence, and one check row. Claude remains a fallback (`IMAGE_READER=claude`). |
| X-2 | **Timezone row:** when the image prints JST, use the JST row with its own date (apply markers like "(FRI)"); otherwise the row matching the column. Read only one other row, to confirm the date. Two titles with "time \| time" are two streams; one title means use the first time; per-streamer reading notes override. |
| X-3 | **Checks (no OCR):** the main row and the check row must be the same moment; the date must match its column unless the check row confirms it; confidence ≥ 80%; timezone recognised; not more than 3 weeks away. A failed check gets one more read; per stream, the reading that passes is kept. Anything still failing is listed as **tentative** (X-7). |
| X-4 | **Timezone:** only what's printed on the image, converted to my timezone; stored in UTC. If none is printed, the stream is tentative, placed provisionally in my timezone, until a waiting room or Edit's timezone dropdown sets it. |
| X-5 | **Collabs:** for images, the reader's partner list; for waiting rooms, @handles following "Collab w/", "w/", "ft." or "コラボ" in the description, or names following "w/" in the title ("with" alone and "w/o" are not treated as collab markers). @handles are displayed as channel names. |
| X-6 | Past streams (more than 3 h ago) are skipped; a stream already listed is not duplicated; a newer schedule moves an image-only stream only when its reading passes the checks. |
| X-7 | **Tentative streams (decision 23):** shown in the table with ⏳ and the best reading; pickable, never added to my calendar unless I pick it (event titled "(tentative)"). Confirmed by a matching waiting room, a newer reading that passes the checks, the stream going live, or my Edit or Confirm. If the confirmed time differs, a 🔧 Fixed notice is posted even if it isn't picked. A tentative stream on my calendar still unconfirmed 3 h before it starts is asked about once in `#needs-review`. Images repeated within one post are read once; images over 4 MB are shrunk to a JPEG of at most 2560 px before reading, so `/drop` accepts up to Discord's 25 MB. |

### 5.4 Discord delivery
Private server, single member (me). Channels:

| Channel | What it's for |
|---|---|
| `#waiting-rooms` | One card per stream found from a YouTube waiting room: start time in my timezone, title, 🤝 collab partners, thumbnail, and **💜 Add to calendar** / **▶ Open** buttons. Once added, the card is marked **💜 ON YOUR CALENDAR** and offers **Remove from calendar**. When the stream starts, the card turns red with **🔴 LIVE NOW** and a **▶ Watch** button; afterwards it shows **Ended** with a replay link. |
| `#schedule-images` | One table message per schedule (submitted or from a community post): each stream's start time in my timezone, title and subtitle, 🤝 collab partners, 💜 if on my calendar, ⏳ if tentative; the schedule image; and three dropdowns: **💜 Add to calendar** (select to add, deselect to remove), **✏️ Edit a stream** (date, hour, minute, timezone, title) and **🗑 Delete a stream**. |
| `#needs-review` | 🔍 Tentative streams on my calendar that are still unconfirmed 3 h before they start: the bot's best reading, the image, and **✓ Confirm / ✏️ Edit / Discard**. |
| `#go-live` | 💜 Ping when a stream I picked goes live |
| `#surprise-live` | ❣️ Ping when a followed streamer goes live without a schedule, or with less than an hour's notice |
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
| L-2c | **Short notice counts as a surprise:** a waiting room first seen less than 1 hour before its start, not on any schedule image and not picked, pings in `#surprise-live` when it goes live ("Announced less than an hour before it started"). A streamer's first scan doesn't count. |
| L-3 | Each alert fires only once per video. |
| L-4 | Quota stays well under 10,000 units/day (≈300 units/day expected, up to ≈1,200 while RSS is down). Never `search.list` (100 units per call). |
| L-5 | **Live status without a ping:** when any listed stream starts or ends, its card (or table line) is redrawn as live or ended, whether or not it was picked. |

### 5.7 My timezone
| ID | Requirement |
|---|---|
| T-1 | Most times are Discord timestamps, so they follow my device's timezone automatically, including when I travel. |
| T-2 | My timezone (`OWNER_TZ`, default `America/Los_Angeles`, or the value set with `/timezone`) is used for dropdown labels, for placing timezone-less image streams in review, and for the Sunday digest time. |
| T-3 | `/timezone [tz]` shows or changes my timezone (stored by the bot, overriding `OWNER_TZ`), with city autocomplete. Changing it redraws recent schedule tables. Automatic detection from my Google Calendar setting isn't possible: the service account can't read my account's settings. |

## 6. Architecture

```
 YouTube (outside, free)
   RSS feed, or API uploads list   every 15 min
   videos.list (status, start)     every 5 min
   Posts tab (schedule images)     every 3 h
   /live page (picked image only)  every 2 min
                     │
                     ▼
 ┌─ Google Cloud e2-micro VM (always free) ─┐
 │ StreamWatch bot (Python, discord.py)     │◄──► Gemini 3.8 Flash
 │ checks, matching, time conversion        │     (Agent Platform)
 │ SQLite database (one file)               │
 └──────────────────────────────────────────┘
           ▲ │                     │
           │ ▼                     ▼
 Discord: commands, /drop     Google Calendar
 cards, tables, alerts        (Stream Watch calendar)
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
| The reader misreads a schedule (e.g. a day off) | Wrong time on a card or calendar | JST row vs. check row must agree; doubtful streams are marked tentative and corrected by their waiting room (with a notice); Edit/Delete on every image stream; evaluation found 0 silent errors in 122 streams (§11.4) |
| Calendar access stops (key deleted, calendar unshared) | No new events | Go-live alerts still work; reported once in `#bot-health` |
| YouTube blocks the VM's IP | No waiting-room updates | M0 showed no blocking; errors reported in `#bot-health` |
| Schedule prints no timezone | Times unknown | Tentative in my timezone; the waiting room or Edit's timezone dropdown fixes it |
| Members-only streams show up in RSS | Noise | Ignored (non-goal) |
| Gemini usage after the trial (≈$0.10–$1/month) | Small real cost from 2027 | Covered by the trial credit now; in January, keep it or switch automatic image reading off (§10) |

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
| | Cost review: per-SKU costs confirmed; the VM and its IP stay free after the trial (§10) | Complete |
| M7 – Evaluation and tentative streams | Image-reader evaluation (§11); uncertain readings become tentative instead of review (X-7); repeated post images read once | Complete |

## 9. Decisions log
| # | Question | Decision |
|---|---|---|
| 1 | How does the agent know my timezone? | Discord timestamps show every time in my device's timezone; `OWNER_TZ` only for dropdown labels, review placement and the digest time; `/timezone` changes it (M6, T-3). Reading my Google Calendar setting isn't possible with a service account. (Updated 2026-10-14) |
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
| 19 | Is the public IP charged? | No: Billing by SKU shows "External IP Charge on a Standard VM" at $0.00 for the free-tier e2-micro, so the IPv4 stays and no IPv6 change is needed. (2026-10-14) |
| 20 | Is the 2-minute live-page watch too frequent? | Kept at 2 min (L-2b). It only runs for picked image-only streams, from 10 min before to 90 min after their start, so it is usually idle; it is free and uses no API quota. The general go-live check stays at 5 min (L-1). (2026-10-14) |
| 21 | Should short-notice streams count as surprises? | Yes: a waiting room that appears less than an hour before it starts pings in `#surprise-live` when it goes live, unless I picked it or it was on a schedule image (L-2c). Prompted by a stream whose waiting room opened about 20 minutes ahead and went live without a ping. (2026-10-15) |
| 22 | How is the image reader evaluated, and what does it cost? | An on-demand exam against an answer key built from real schedule posts and checked against YouTube (§11). It runs the same model on the Gemini API free tier, so it costs $0; readings are saved, so re-scoring is free. (2026-10-16) |
| 23 | What happens to uncertain readings instead of asking me? | They become **tentative**: shown in the table with ⏳ and the best reading, never added to my calendar unless I pick them (then titled "(tentative)"). A matching waiting room confirms or corrects them automatically, with a notice only if the time changed; my Edit also confirms them. I'm asked in `#needs-review` only if a tentative stream on my calendar is still unconfirmed a few hours before it starts. (2026-10-16) |
| 24 | Which community posts are read? | Option A plus short captions: image posts with 15 words or fewer are read too. Vantacrow posts schedules with one-line jokes ("I'm your Huckleberry") and no schedule words, so option A skipped them. Short selfie or art posts cost a read each (about $0.02) and come back as "not a schedule". (2026-10-20) |

## 10. Notes

### 10.1 Costs
Confirmed 2026-10-14 in Billing → Reports, by SKU.

| Item | Cost after the free trial | Note |
|---|---|---|
| e2-micro VM, 30 GB standard disk, Ubuntu Minimal licence | $0 | Always Free; billed with "free tier discount" |
| External IP on the VM | $0 | $0.00 for 8.62 hours of use (list price would be $0.04) |
| Network Intelligence Center resource hours | $0 | Shown in billing, fully discounted |
| Gemini 3.8 Flash (Agent Platform) | ≈ $0.10–$1 / month (≈ $0.20–$2 from 2027) | About $0.01–$0.02 per schedule read; the only real cost. Google's listed Gemini 3.8 Flash price doubles on 2027-01-01 |
| Gemini API free tier (AI Studio key) | $0 | Same model; used only by the evaluation (§11). Inputs may be used by Google to improve its products; limits are unpublished and can change |
| Anthropic API (fallback reader) | $0 | Unused; prepaid credit only, auto-reload off |

- **Budget alert:** $1/month, alert only (promotional credits excluded), so any real charge sends an email.
- **Free-tier limit:** Always Free covers one e2-micro per billing account, across all projects. Other projects can
  share this VM if they are light (1 GB of memory) or use free serverless tiers; a second VM costs about $7–8/month.

### 10.2 Upcoming actions
| When | Action |
|---|---|
| Before 2027-01-12 (trial ends) | Upgrade the billing account to keep the VM running. Without the upgrade the VM is stopped and nothing is charged. |
| January 2027 | Decide how schedule images are read after the trial: keep the paid route, move the bot to the Gemini API free tier (same model, $0, with the paid route as fallback), or switch automatic reading off and use `/drop` only. |

### 10.3 Parked features
**Instant waiting-room alerts (WebSub push).** Built, tested and verified on 2026-10-14, then disabled by decision 15;
waiting rooms are found by polling every 15 minutes (I-3).
- **How it works:** the bot subscribes each followed channel at YouTube's WebSub hub; when a streamer creates or
  updates a video, the hub pushes it to the bot within about a minute. Pushes are verified with a shared secret;
  subscriptions renew every 2 days. Free (no quota until a pushed video is looked up).
- **To turn it back on:** add `WEBSUB_ENABLED=1` to the server's `.env`, open TCP port 8080 in the Google Cloud
  firewall (rule `allow-youtube-websub`, ingress, 0.0.0.0/0), and restart the bot.

### 10.4 Rejected options
Options considered and turned down, grouped by area. Each links to its decision (§9) or requirement where one exists.

**Hosting and cost**
| Option | Why not |
|---|---|
| Oracle Cloud free VM | Both free machine types were out of capacity in San Jose |
| Cloud NAT (to drop the public IP) | Not cheaper: about $4.60/month, while the IP is free (decision 19) |
| IPv6-only VM | Unnecessary, since the IPv4 address is free (decision 19) |
| Spend cap enforcement | Can't cover Compute Engine, and would pause services when reached; a $1 alert-only budget is used instead (§10.1) |

**Reading schedule images**
| Option | Why not |
|---|---|
| Claude as the main image reader | Paid from prepaid Anthropic credit (real money); Gemini runs on the Google Cloud credit (decision 11) |
| Gemini 3.5 Flash-Lite | Cheaper, but put a stream a day early on a real schedule (decision 11) |
| OCR cross-check (Tesseract) | Mostly flagged correctly read stylised text; the reader's own timezone rows are the check now (decision 13) |
| Streamer home timezones | Not needed: waiting rooms give exact times and schedules print their timezone (decision 12) |
| Evaluating with a different free model | It would grade that model, not the one the bot uses; the eval uses the same Gemini 3.8 Flash on its free tier instead (decision 22) |
| One card per stream from an image | Too many messages; one table per schedule instead (decision 14) |

**Finding streams and alerts**
| Option | Why not |
|---|---|
| `/follow` by channel name | Name lookup needs `search.list` at 100 quota units per call; channel URLs cost 1 (F-1) |
| Sending every community post image to Gemini to ask "is this a schedule?" (option B) | A paid model call per post; schedule words in the post text are enough (option A, decision 17) |
| ±30 min matching window | Would miss streams whose waiting room time differs more from the schedule; ±90 is kept, with title similarity to separate close streams (I-4) |
| Checking feeds and go-live every 2 min | 15 min for waiting rooms and 5 min for go-live are enough; 2 min would use about 720 quota units a day on the shared key (L-1, I-3) |
| Pings for every stream that goes live | Too noisy; unpicked streams turn red on their card without a ping (decision 18) |
| Proxy rotation, bot-check spoofing or logged-in cookies if YouTube blocks the VM | Against YouTube's rules and risks the account; fallbacks are a home machine or a paid hosted API (decision 6) |

**Discord and calendar**
| Option | Why not |
|---|---|
| Google OAuth app for Calendar | Publishing needs an owned domain; testing tokens expire every 7 days (decision 10) |
| Choosing between several calendars | One dedicated "Stream Watch" calendar is enough (decision 4) |
| Tracking real stream end times | Fixed 2-hour events are enough (decision 3) |
| Reading my timezone from Google Calendar | Not possible with a service account; `/timezone` instead (T-3) |

## 11. Evaluation (extra procedure)

An on-demand check of the schedule-image reader, separate from the running bot. Its purpose is to measure whether readings can be trusted without human review, and which review triggers (X-3) can safely be relaxed.

### 11.1 Procedure
1. **Collect** (`eval/collect.py`): saves the schedule posts on each followed streamer's Posts tab (same filter as I-6) with their images and text, plus YouTube's recent live streams for that channel. The data stays on the VM (`eval/data/`, not in git).
2. **Answer key** (`eval/answers.json`): every stream as printed on each image, in UTC, written by reading the images. It is checked against YouTube's actual start times: a match within 30 minutes confirms a stream; mismatches are explained by real changes (cancellations, collabs on another channel, members-only or future streams).
3. **Exam** (`eval/run.py`): reads each schedule exactly as the bot does (same model, prompt, reading notes, checks and second read) on the Gemini API free tier. Readings are saved, so re-scoring after a rule change costs nothing; `--fresh` reads again after a prompt or model change.
4. **Report** (`eval/report.md`): the metrics below, with the details of every silent error and unnecessary review.

### 11.2 Metrics
| Metric | Meaning | Target |
|---|---|---|
| Silent errors | Readings that pass every check but have the wrong time, or streams that don't exist | 0 |
| Right time overall | Streams read with the right start time (within 5 min), accepted or reviewed | ≥ 95% (success metric) |
| Unnecessary reviews | Correct readings that were still sent to `#needs-review` | As low as possible |
| Caught errors | Wrong readings that the checks sent to review | All wrong readings |
| Missed | Streams in the key that weren't read at all | 0 |

### 11.3 When to run
- After changing the prompt, the model, or a check.
- Before removing or relaxing a review trigger.
- Before the January 2027 decision on how images are read (§10.2).

### 11.4 Results
**First run, 2026-10-16** (Gemini 3.8 Flash, paid route because the free tier was too busy; about $0.50 of trial credit). 22 schedules from 6 streamers, 122 streams.

| Metric | Result |
|---|---|
| Right time overall | 122 of 122 (100%) |
| Silent errors | 0 |
| Unnecessary reviews | 0 |
| Caught errors | 0 (one schedule failed a check on its first read; the automatic second read fixed it) |
| Missed | 0 |
| Titles read correctly | 121 of 122 |

The sample included start/end time pairs (with a reading note), "times are one day after" notes, misprinted day and timezone labels, Japanese and Korean titles, and two streams on one day. Limits: no schedule without a printed timezone was in the sample, and the answer key was written by reading the images (81 of its streams were confirmed exactly by YouTube; the rest were real schedule changes or streams on other channels).

**Conclusion:** for these formats, readings that pass the checks can be trusted without review. Remaining uncertainty is mostly images without a timezone, which tentative streams (decision 23, X-7) handle.

**Also found:** the Posts tab lists each image of a two-image post twice, so those schedules are sent to the reader twice. Identical images are now read and shown once (X-7), which halves the cost of those reads.
