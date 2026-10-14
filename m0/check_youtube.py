"""M0 feasibility test: can this machine read YouTube Posts tabs and /live pages?

Run on the Mac first (baseline), then on the VM. Compare the summaries.

    python3 m0/check_youtube.py m0/channels.txt                 # one round
    python3 m0/check_youtube.py m0/channels.txt --rounds 8 --interval 180

Every check is appended to m0/results.csv so rounds spread over a day can be compared.
Standard library only, so it runs on a fresh VM without installing anything.
"""

import argparse
import csv
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
)
BOT_WALL_MARKERS = ("confirm you’re not a bot", "confirm you're not a bot", "/sorry/index")
RESULTS_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results.csv")
CSV_FIELDS = ["time_utc", "host", "channel", "page", "http_status", "verdict", "posts_found", "images_found", "detail"]


def normalize_url(line):
    """Accept 'youtube.com/@x', 'www.youtube.com/@x' or a full URL; return https://www.youtube.com/@x."""
    url = line.strip().rstrip("/")
    url = re.sub(r"^(https?://)?(www\.|m\.)?youtube\.com", "https://www.youtube.com", url)
    return url


def fetch(url):
    """Return (status, final_url, html). Status 0 means a network error."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.geturl(), resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, url, e.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, socket.timeout) as e:
        return 0, url, str(e)


def extract_initial_data(html):
    match = re.search(r"var ytInitialData = (\{.*?\});</script>", html, re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def find_posts(node, found):
    """Collect every backstagePostRenderer / postRenderer dict in ytInitialData."""
    if isinstance(node, dict):
        for key in ("backstagePostRenderer", "postRenderer"):
            if key in node:
                found.append(node[key])
        for value in node.values():
            find_posts(value, found)
    elif isinstance(node, list):
        for value in node:
            find_posts(value, found)
    return found


def count_images(post):
    return len(set(re.findall(r"https://yt3\.ggpht\.com/[^\"=]+", json.dumps(post))))


def blocked_verdict(status, final_url, html):
    """Return a verdict string if the page is blocked, else None."""
    if status == 0:
        return "network_error"
    if "consent.youtube.com" in final_url:
        return "consent_wall"
    if status == 429:
        return "rate_limited"
    if any(m in html.lower() for m in BOT_WALL_MARKERS):
        return "bot_wall"
    if status != 200:
        return "http_error"
    return None


def check_posts(channel_url):
    status, final_url, html = fetch(channel_url.rstrip("/") + "/posts")
    verdict = blocked_verdict(status, final_url, html)
    if verdict:
        return status, verdict, 0, 0, final_url if verdict == "consent_wall" else ""
    data = extract_initial_data(html)
    if data is None:
        return status, "no_page_data", 0, 0, "ytInitialData missing (layout change?)"
    posts = find_posts(data, [])
    images = sum(count_images(p) for p in posts)
    latest = posts[0].get("postId", "") if posts else ""
    return status, "ok" if posts else "ok_no_posts", len(posts), images, f"latest post {latest}"


def check_live(channel_url):
    status, final_url, html = fetch(channel_url.rstrip("/") + "/live")
    verdict = blocked_verdict(status, final_url, html)
    if verdict:
        return status, verdict, 0, 0, ""
    video = re.search(r'<link rel="canonical" href="https://www\.youtube\.com/watch\?v=([\w-]{11})"', html)
    if not video:
        return status, "ok_not_live", 0, 0, "no live or upcoming stream"
    # "isLive" also appears on waiting-room pages, so only isLiveNow counts as live.
    if '"isLiveNow":true' in html:
        return status, "ok_live", 0, 0, f"video {video.group(1)}"
    start = re.search(r'"scheduledStartTime":"(\d+)"', html)
    when = datetime.fromtimestamp(int(start.group(1)), timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if start else "unknown"
    return status, "ok_upcoming", 0, 0, f"video {video.group(1)} starts {when}"


def run_round(channels, writer, host):
    rows = []
    for channel in channels:
        for page, check in (("posts", check_posts), ("live", check_live)):
            status, verdict, posts, images, detail = check(channel)
            row = {
                "time_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "host": host, "channel": channel, "page": page, "http_status": status,
                "verdict": verdict, "posts_found": posts, "images_found": images, "detail": detail,
            }
            writer.writerow(row)
            rows.append(row)
            print(f"  {page:5}  {verdict:13}  HTTP {status}  posts={posts} images={images}  {channel}  {detail}")
            time.sleep(3)  # stay gentle: a few seconds between requests
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("channels_file", help="text file with one channel URL per line (youtube.com/@handle)")
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--interval", type=int, default=180, help="minutes between rounds")
    args = parser.parse_args()

    with open(args.channels_file) as f:
        channels = [normalize_url(line) for line in f if line.strip() and not line.startswith("#")]
    if not channels:
        sys.exit("No channel URLs found in " + args.channels_file)

    host = socket.gethostname()
    new_file = not os.path.exists(RESULTS_CSV)
    all_rows = []
    with open(RESULTS_CSV, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if new_file:
            writer.writeheader()
        for i in range(args.rounds):
            print(f"Round {i + 1}/{args.rounds} from {host}")
            all_rows += run_round(channels, writer, host)
            f.flush()
            if i < args.rounds - 1:
                time.sleep(args.interval * 60)

    blocked = [r for r in all_rows if not r["verdict"].startswith("ok")]
    print(f"\nSummary: {len(all_rows)} checks, {len(blocked)} blocked or failed.")
    for verdict in sorted({r["verdict"] for r in blocked}):
        print(f"  {verdict}: {sum(r['verdict'] == verdict for r in blocked)}")
    print("PASS: this machine can read YouTube." if not blocked else "FAIL: see verdicts above and m0/results.csv.")


if __name__ == "__main__":
    main()
