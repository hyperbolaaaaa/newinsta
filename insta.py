import datetime
import json
import os
import random
import re
import threading
import time
from io import BytesIO
from queue import Queue

import instaloader
import requests
import telebot
from PIL import Image
from playwright.sync_api import sync_playwright
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

# =========================
# CONFIG
# =========================
TOKEN = "8665521420:AAHi0hfMNn3odVDCd9ajMCW_8FwrSz2OQLQ"
if not TOKEN:
    raise RuntimeError("Set TELEGRAM_BOT_TOKEN env var.")

COOKIE_DIR = os.path.join(os.getcwd(), "instacookie")
HEADLESS = True
POST_BATCH = 10
MAX_SCROLL_ROUNDS = 28

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

bot = telebot.TeleBot(TOKEN, threaded=True)
job_queue = Queue()
user_jobs = {}

requests_session = requests.Session()
requests_session.headers.update(
    {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.instagram.com/",
    }
)

request_lock = threading.Lock()
last_request_ts = 0.0
instaloader_lock = threading.Lock()


# =========================
# LOGGING / HELPERS
# =========================
def log(msg):
    t = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{t}] {msg}")


def short_url(url, limit=120):
    if not url:
        return "-"
    if len(url) <= limit:
        return url
    return url[:limit] + "...(truncated)"


def human_pause(min_s=1.0, max_s=3.0, extra_chance=0.18):
    wait = random.uniform(min_s, max_s)
    if random.random() < extra_chance:
        wait += random.uniform(1.5, 4.5)
    time.sleep(wait)


def throttled_get(url, timeout=35, stream=False):
    global last_request_ts
    with request_lock:
        now = time.time()
        min_gap = random.uniform(2.0, 5.5)
        elapsed = now - last_request_ts
        if elapsed < min_gap:
            time.sleep(min_gap - elapsed)
        if random.random() < 0.12:
            time.sleep(random.uniform(1.5, 4.0))
        resp = requests_session.get(url, timeout=timeout, stream=stream)
        last_request_ts = time.time()
    return resp


def extract_username(text):
    text = (text or "").strip().split("?")[0]
    match = re.search(r"instagram\.com/([^/]+)/?", text)
    if match:
        return match.group(1).lower()
    if re.match(r"^[a-zA-Z0-9._]+$", text):
        return text.lower()
    return None


def get_shortcode_from_url(post_url):
    m = re.search(r"/(?:p|reel|tv)/([^/?#]+)/?", post_url)
    if not m:
        return None
    return m.group(1)


# =========================
# COOKIE LOADING
# =========================
def _normalize_cookie(cookie):
    name = str(cookie.get("name", "")).strip()
    value = str(cookie.get("value", "")).strip()
    if not name or not value:
        return None

    domain = str(cookie.get("domain") or ".instagram.com").strip() or ".instagram.com"
    path = str(cookie.get("path") or "/")
    secure = bool(cookie.get("secure", True))
    http_only = bool(cookie.get("httpOnly", False))
    same_site = str(cookie.get("sameSite", "Lax"))

    if same_site not in {"Lax", "None", "Strict"}:
        same_site = "Lax"

    return {
        "name": name,
        "value": value,
        "domain": domain,
        "path": path,
        "secure": secure,
        "httpOnly": http_only,
        "sameSite": same_site,
    }


def _parse_text_cookies(content):
    cookies = []

    # Netscape format
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) == 7:
            domain, _flag, path, secure, _expiry, name, value = parts
            c = _normalize_cookie(
                {
                    "name": name,
                    "value": value,
                    "domain": domain,
                    "path": path,
                    "secure": secure.upper() == "TRUE",
                    "httpOnly": False,
                    "sameSite": "Lax",
                }
            )
            if c:
                cookies.append(c)

    if cookies:
        return cookies

    # key=value style
    if ";" in content:
        parts = [p.strip() for p in content.replace("\n", ";").split(";") if p.strip()]
    else:
        parts = [p.strip() for p in content.splitlines() if p.strip()]

    for part in parts:
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        c = _normalize_cookie(
            {
                "name": name.strip(),
                "value": value.strip(),
                "domain": ".instagram.com",
                "path": "/",
                "secure": True,
                "httpOnly": False,
                "sameSite": "Lax",
            }
        )
        if c:
            cookies.append(c)

    return cookies


def load_instagram_cookies(cookie_dir):
    if not os.path.isdir(cookie_dir):
        raise RuntimeError(f"Missing cookie folder: {cookie_dir}")

    files = []
    for name in os.listdir(cookie_dir):
        path = os.path.join(cookie_dir, name)
        if os.path.isfile(path) and name.lower().endswith((".json", ".txt", ".cookie", ".cookies")):
            files.append(path)

    if not files:
        raise RuntimeError(
            "No cookie file found in instacookie folder. Upload one .json/.txt cookie file first."
        )

    cookie_file = max(files, key=os.path.getmtime)
    log(f"[COOKIE] Loading from: {cookie_file}")

    with open(cookie_file, "r", encoding="utf-8", errors="ignore") as f:
        raw = f.read().strip()

    cookies = []
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and isinstance(data.get("cookies"), list):
            data = data["cookies"]

        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    c = _normalize_cookie(item)
                    if c:
                        cookies.append(c)
        elif isinstance(data, dict):
            for k, v in data.items():
                c = _normalize_cookie(
                    {
                        "name": str(k),
                        "value": str(v),
                        "domain": ".instagram.com",
                        "path": "/",
                        "secure": True,
                    }
                )
                if c:
                    cookies.append(c)
    except Exception:
        cookies = _parse_text_cookies(raw)

    out = []
    seen = set()
    for c in cookies:
        domain = c["domain"].lower()
        if "instagram.com" not in domain:
            continue
        key = (c["name"], domain, c["path"])
        if key in seen:
            continue
        seen.add(key)
        out.append(c)

    if not out:
        raise RuntimeError("No valid Instagram cookies found.")
    if not any(c["name"] == "sessionid" for c in out):
        raise RuntimeError("Cookie file missing sessionid.")

    log(f"[COOKIE] Loaded {len(out)} Instagram cookies")
    return out


def apply_cookies_to_requests(cookies):
    for c in cookies:
        requests_session.cookies.set(c["name"], c["value"], domain=c["domain"], path=c["path"])


def apply_cookies_to_instaloader(loader, cookies):
    # Ensure instaloader and requests share the same session auth
    for c in cookies:
        loader.context._session.cookies.set(c["name"], c["value"], domain=c["domain"], path=c["path"])

    loader.context.max_connection_attempts = 1
    loader.context.request_timeout = 30.0


# =========================
# INSTALOADER MEDIA
# =========================
def get_media_from_post_url_with_instaloader(loader, post_url):
    shortcode = get_shortcode_from_url(post_url)
    if not shortcode:
        raise RuntimeError("Invalid post URL (no shortcode).")

    with instaloader_lock:
        log(f"[IL] Loading shortcode: {shortcode}")
        post = instaloader.Post.from_shortcode(loader.context, shortcode)

    items = []

    if post.typename == "GraphSidecar":
        for node in post.get_sidecar_nodes():
            if node.is_video:
                if node.video_url:
                    items.append(("video", node.video_url))
            else:
                if node.display_url:
                    items.append(("photo", node.display_url))
    elif post.is_video:
        if post.video_url:
            items.append(("video", post.video_url))
    else:
        if post.url:
            items.append(("photo", post.url))

    photos = sum(1 for t, _ in items if t == "photo")
    videos = sum(1 for t, _ in items if t == "video")
    log(f"[IL] Extracted from {shortcode}: total={len(items)} photos={photos} videos={videos}")

    return items


# =========================
# PLAYWRIGHT SCRAPER
# =========================
def collect_post_links(page):
    return page.evaluate(
        """
        Array.from(document.querySelectorAll('a'))
            .map(a => a.href)
            .filter(h => h && (h.includes('/p/') || h.includes('/reel/')))
            .map(h => h.split('?')[0])
        """
    )


def human_scroll_and_collect(page, job):
    seen = set(job.posts)
    idle_rounds = 0
    rounds = random.randint(16, MAX_SCROLL_ROUNDS)

    for i in range(rounds):
        if not job.running:
            break

        if random.random() < 0.2:
            step = -random.randint(100, 350)
        else:
            step = random.randint(450, 1550)

        smooth = "smooth" if random.random() < 0.7 else "auto"
        page.evaluate(
            """
            ([distance, behavior]) => {
                window.scrollBy({ top: distance, left: 0, behavior });
            }
            """,
            [step, smooth],
        )

        human_pause(1.0, 3.8, extra_chance=0.23)

        links = collect_post_links(page)
        log(f"[PW] Round {i+1}/{rounds} links found: {len(links)}")

        added = 0
        for link in links:
            if link not in seen:
                seen.add(link)
                job.posts.append(link)
                added += 1

        log(f"[PW] Collected posts: {len(job.posts)} (+{added})")

        if added == 0:
            idle_rounds += 1
        else:
            idle_rounds = 0

        if idle_rounds >= random.randint(4, 7):
            break


def scrape_profile_links(job, context):
    username = job.username
    page = None

    try:
        log(f"[PW] Scrape started for @{username}")
        page = context.new_page()

        human_pause(2.0, 4.8)
        page.goto(f"https://www.instagram.com/{username}/", wait_until="domcontentloaded")
        human_pause(6.0, 10.0)

        log(f"[PW] Current URL: {page.url}")

        if "accounts/login" in page.url:
            job.error = "Instagram redirected to login. Cookie/session is not valid in browser."
            return

        if "challenge" in page.url:
            job.error = "Instagram challenge page detected. Account/IP blocked temporarily."
            return

        page.wait_for_load_state("networkidle")
        page.mouse.move(random.randint(80, 220), random.randint(100, 260))
        page.click("body")
        page.mouse.wheel(0, 1400)
        time.sleep(3)

        human_scroll_and_collect(page, job)

        if not job.posts:
            job.error = "No post links collected. Possibly blocked, private profile, or page not fully rendered."

    except Exception as e:
        job.error = f"Playwright scrape error: {type(e).__name__}: {e}"
    finally:
        job.ready_event.set()
        try:
            if page:
                page.close()
        except Exception:
            pass


def playwright_worker(playwright_cookies):
    with sync_playwright() as play:
        browser = play.chromium.launch(
            headless=HEADLESS,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--start-maximized",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="en-US",
            timezone_id="Asia/Kolkata",
            viewport={
                "width": random.randint(1220, 1380),
                "height": random.randint(780, 950),
            },
        )

        context.add_cookies(playwright_cookies)
        log(f"[PW] Cookies loaded into browser: {len(playwright_cookies)}")

        warm = context.new_page()
        warm.goto("https://www.instagram.com/", wait_until="domcontentloaded")
        human_pause(2.0, 4.0)
        log(f"[PW] Warmup URL: {warm.url}")
        warm.close()

        while True:
            job = job_queue.get()
            if job is None:
                break
            try:
                scrape_profile_links(job, context)
            except Exception as e:
                job.error = f"Worker error: {type(e).__name__}: {e}"
                job.ready_event.set()
            finally:
                job_queue.task_done()


# =========================
# JOB MODEL
# =========================
class Job:
    def __init__(self, username):
        self.username = username
        self.posts = []
        self.sent = 0
        self.running = True
        self.error = None
        self.ready_event = threading.Event()


# =========================
# TELEGRAM BOT HANDLERS
# =========================
@bot.message_handler(commands=["start"])
def start(message):
    bot.send_message(message.chat.id, "Send Instagram username or profile link")


@bot.message_handler(func=lambda m: True)
def profile_handler(message):
    username = extract_username(message.text)
    if not username:
        bot.send_message(
            message.chat.id,
            "Invalid input. Send Instagram username or profile link.",
        )
        return

    job = Job(username)
    user_jobs[message.chat.id] = job

    bot.send_message(
        message.chat.id,
        f"Collecting post links for @{username}... Please wait...",
    )

    job_queue.put(job)

    # Wait for first scraping pass
    completed = job.ready_event.wait(timeout=70)
    if not completed:
        bot.send_message(
            message.chat.id,
            "Scraper timeout. Instagram is slow/blocked. Try again in a few minutes.",
        )
        return

    if job.error:
        bot.send_message(message.chat.id, f"Scrape failed: {job.error}")
        return

    if not job.posts:
        bot.send_message(
            message.chat.id,
            "No posts found. Profile may be private or Instagram blocked access.",
        )
        return

    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton(f"Download {POST_BATCH} Posts", callback_data="next"),
        InlineKeyboardButton("Cancel", callback_data="cancel"),
    )

    bot.send_message(
        message.chat.id,
        f"Ready. Collected {len(job.posts)} post links for @{username}.",
        reply_markup=markup,
    )


@bot.callback_query_handler(func=lambda call: call.data == "cancel")
def cancel(call):
    job = user_jobs.get(call.message.chat.id)
    if job:
        job.running = False
    bot.send_message(call.message.chat.id, "Stopped.")


@bot.callback_query_handler(func=lambda call: call.data == "next")
def send_next(call):
    job = user_jobs.get(call.message.chat.id)
    if not job:
        bot.send_message(call.message.chat.id, "No active job.")
        return

    if not job.running:
        bot.send_message(call.message.chat.id, "Job already stopped.")
        return

    start_idx = job.sent
    end_idx = start_idx + POST_BATCH
    batch = job.posts[start_idx:end_idx]

    if not batch:
        bot.send_message(call.message.chat.id, "No more collected posts.")
        return

    bot.send_message(call.message.chat.id, f"Downloading media for posts {start_idx + 1}-{min(end_idx, len(job.posts))}...")
    log(f"[SEND] Chat {call.message.chat.id} processing posts {start_idx}..{end_idx - 1}")

    for post_url in batch:
        if not job.running:
            break

        try:
            log(f"[SEND] Processing {short_url(post_url)}")
            medias = get_media_from_post_url_with_instaloader(LOADER, post_url)
            if not medias:
                log(f"[SEND] No media extracted for {short_url(post_url)}")
                bot.send_message(call.message.chat.id, f"No media found:\n{post_url}")
                continue

            for media_type, media_url in medias:
                if not job.running:
                    break
                try:
                    log(f"[SEND] Downloading {media_type}: {short_url(media_url)}")
                    response = throttled_get(media_url, timeout=45, stream=True)
                    ct = response.headers.get("Content-Type", "-")
                    cl = response.headers.get("Content-Length", "-")
                    log(f"[SEND] Media HTTP {response.status_code}, type={ct}, len={cl}")

                    if response.status_code != 200:
                        raise RuntimeError(f"Media HTTP {response.status_code}")

                    payload = response.content
                    if not payload:
                        raise RuntimeError("Empty media payload")

                    data = BytesIO(payload)

                    if media_type == "video":
                        data.name = "video.mp4"
                        bot.send_video(
                            call.message.chat.id,
                            data,
                            width=720,
                            height=1280,
                            supports_streaming=True,
                        )
                        log(f"[SEND] Video sent for {short_url(post_url)}")
                    else:
                        img = Image.open(data).convert("RGB")
                        out = BytesIO()
                        img.save(out, format="JPEG", quality=92)
                        out.seek(0)
                        bot.send_photo(call.message.chat.id, out)
                        log(f"[SEND] Photo sent for {short_url(post_url)}")

                    human_pause(1.7, 4.0, extra_chance=0.2)

                except Exception as e:
                    log(
                        f"[SEND][ERROR] media_type={media_type} post={short_url(post_url)} "
                        f"err={type(e).__name__}: {e}"
                    )
                    bot.send_message(
                        call.message.chat.id,
                        f"Failed media send:\n{post_url}\nReason: {e}",
                    )
                    human_pause(1.0, 2.0, extra_chance=0.0)

            human_pause(1.2, 2.8, extra_chance=0.12)

        except Exception as e:
            log(f"[SEND][ERROR] post={short_url(post_url)} err={type(e).__name__}: {e}")
            bot.send_message(
                call.message.chat.id,
                f"Post failed:\n{post_url}\nReason: {e}",
            )

    job.sent += len(batch)

    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton(f"Next {POST_BATCH}", callback_data="next"),
        InlineKeyboardButton("Cancel", callback_data="cancel"),
    )

    bot.send_message(
        call.message.chat.id,
        f"Done. Sent up to {job.sent} collected posts.",
        reply_markup=markup,
    )


# =========================
# STARTUP
# =========================
LOADER = instaloader.Instaloader(
    download_pictures=False,
    download_videos=False,
    download_video_thumbnails=False,
    save_metadata=False,
    compress_json=False,
)


def main():
    cookies = load_instagram_cookies(COOKIE_DIR)
    apply_cookies_to_requests(cookies)
    apply_cookies_to_instaloader(LOADER, cookies)

    log("Bot started")
    log(f"Playwright headless mode: {HEADLESS}")

    threading.Thread(target=playwright_worker, args=(cookies,), daemon=True).start()
    bot.infinity_polling(timeout=60, long_polling_timeout=60)


if __name__ == "__main__":
    main()
