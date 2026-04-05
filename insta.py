import datetime
import json
import os
import random
import re
import threading
import time
from io import BytesIO
from queue import Queue

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


def log(msg):
    t = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{t}] {msg}")


def human_pause(min_s=1.0, max_s=3.0, extra_chance=0.18):
    wait = random.uniform(min_s, max_s)
    if random.random() < extra_chance:
        wait += random.uniform(1.5, 4.5)
    time.sleep(wait)


def throttled_get(url, timeout=35, stream=False):
    global last_request_ts
    with request_lock:
        now = time.time()
        min_gap = random.uniform(2.2, 5.8)
        elapsed = now - last_request_ts
        if elapsed < min_gap:
            time.sleep(min_gap - elapsed)
        if random.random() < 0.12:
            time.sleep(random.uniform(2.0, 6.0))
        resp = requests_session.get(url, timeout=timeout, stream=stream)
        last_request_ts = time.time()
    return resp


def _domain_or_default(domain):
    domain = (domain or "").strip()
    return domain if domain else ".instagram.com"


def _normalize_cookie(cookie):
    name = str(cookie.get("name", "")).strip()
    value = str(cookie.get("value", "")).strip()
    if not name or not value:
        return None

    domain = _domain_or_default(cookie.get("domain"))
    path = cookie.get("path") or "/"
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


def _parse_cookie_lines(content):
    cookies = []

    # Netscape format support
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) == 7:
            domain, _flag, path, secure, _expiry, name, value = parts
            parsed = _normalize_cookie(
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
            if parsed:
                cookies.append(parsed)

    if cookies:
        return cookies

    # Cookie header or key=value lines
    chunks = []
    if ";" in content and "=" in content:
        chunks = [p.strip() for p in content.replace("\n", ";").split(";") if p.strip()]
    else:
        chunks = [ln.strip() for ln in content.splitlines() if ln.strip()]

    for part in chunks:
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        parsed = _normalize_cookie(
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
        if parsed:
            cookies.append(parsed)

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
    log(f"Loading cookies from: {cookie_file}")

    with open(cookie_file, "r", encoding="utf-8", errors="ignore") as f:
        raw = f.read().strip()

    cookies = []
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and isinstance(data.get("cookies"), list):
            data = data["cookies"]

        if isinstance(data, list):
            for entry in data:
                if isinstance(entry, dict):
                    c = _normalize_cookie(entry)
                    if c:
                        cookies.append(c)
        elif isinstance(data, dict):
            # support simple dict like {"sessionid":"..."}
            for name, value in data.items():
                c = _normalize_cookie(
                    {
                        "name": str(name),
                        "value": str(value),
                        "domain": ".instagram.com",
                        "path": "/",
                        "secure": True,
                        "httpOnly": False,
                        "sameSite": "Lax",
                    }
                )
                if c:
                    cookies.append(c)
    except Exception:
        cookies = _parse_cookie_lines(raw)

    # keep only instagram cookies and de-duplicate by (name, domain, path)
    filtered = []
    seen = set()
    for c in cookies:
        domain = c["domain"].lower()
        if "instagram.com" not in domain:
            continue
        key = (c["name"], domain, c["path"])
        if key in seen:
            continue
        seen.add(key)
        filtered.append(c)

    if not filtered:
        raise RuntimeError("No valid Instagram cookies found in cookie file.")

    if not any(c["name"] == "sessionid" for c in filtered):
        raise RuntimeError("Cookie file is missing sessionid.")

    return filtered


def apply_cookies_to_requests(cookies):
    for c in cookies:
        requests_session.cookies.set(c["name"], c["value"], domain=c["domain"], path=c["path"])


def decode_ig_url(url):
    if not url:
        return ""
    cleaned = url.replace("\\u0026", "&").replace("\\/", "/")
    try:
        cleaned = bytes(cleaned, "utf-8").decode("unicode_escape")
    except Exception:
        pass
    return cleaned


def extract_meta(html, attr, key):
    m = re.search(
        rf'<meta[^>]*{attr}="{re.escape(key)}"[^>]*content="([^"]+)"',
        html,
        flags=re.IGNORECASE,
    )
    return decode_ig_url(m.group(1).strip()) if m else ""


def get_profile_info(username):
    try:
        url = f"https://www.instagram.com/{username}/"
        response = throttled_get(url, timeout=30)
        if response.status_code != 200:
            raise RuntimeError(f"Profile page HTTP {response.status_code}")

        html = response.text
        og_title = extract_meta(html, "property", "og:title")
        og_desc = extract_meta(html, "property", "og:description")
        pfp = extract_meta(html, "property", "og:image")

        fullname = "-"
        followers = "-"
        following = "-"
        posts = "-"
        bio = "-"

        t = re.search(r"^(.*?)\s*\(@", og_title or "")
        if t:
            fullname = t.group(1).strip() or "-"

        d = re.search(
            r"([0-9.,MKmk]+)\s+Followers,\s*([0-9.,MKmk]+)\s+Following,\s*([0-9.,MKmk]+)\s+Posts",
            og_desc or "",
        )
        if d:
            followers, following, posts = d.group(1), d.group(2), d.group(3)

        b = re.search(r"on Instagram:\s*[\"“](.*?)[\"”]", og_desc or "")
        if b and b.group(1).strip():
            bio = b.group(1).strip()

        return {
            "username": username,
            "fullname": fullname,
            "followers": followers,
            "following": following,
            "posts": posts,
            "bio": bio,
            "pfp": pfp,
        }
    except Exception as e:
        log(f"Profile info error: {e}")
        return None


def get_media_from_post_url(post_url):
    response = throttled_get(post_url, timeout=35)
    if response.status_code != 200:
        raise RuntimeError(f"Post page HTTP {response.status_code}")

    html = response.text
    items = []
    seen = set()

    for raw in re.findall(r'"video_url":"([^"]+)"', html):
        u = decode_ig_url(raw)
        if u and u not in seen:
            seen.add(u)
            items.append(("video", u))

    for raw in re.findall(r'"display_url":"([^"]+)"', html):
        u = decode_ig_url(raw)
        if u and u not in seen:
            seen.add(u)
            items.append(("photo", u))

    if not items:
        og_video = extract_meta(html, "property", "og:video")
        og_image = extract_meta(html, "property", "og:image")
        if og_video:
            items.append(("video", og_video))
        elif og_image:
            items.append(("photo", og_image))

    return items


def extract_username(text):
    text = (text or "").strip().split("?")[0]
    match = re.search(r"instagram\.com/([^/]+)/?", text)
    if match:
        return match.group(1).lower()
    if re.match(r"^[a-zA-Z0-9._]+$", text):
        return text.lower()
    return None


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
    rounds = random.randint(16, 30)

    for _ in range(rounds):
        if not job.running:
            break

        # random scrolling pattern: mostly down, sometimes slight up
        if random.random() < 0.2:
            step = -random.randint(120, 420)
        else:
            step = random.randint(350, 1450)

        smooth = "smooth" if random.random() < 0.65 else "auto"
        page.evaluate(
            """
            ([distance, behavior]) => {
                window.scrollBy({ top: distance, left: 0, behavior });
            }
            """,
            [step, smooth],
        )

        human_pause(0.9, 3.4, extra_chance=0.25)

        links = collect_post_links(page)
        log(f"Links found: {len(links)}")
        added = 0
        for link in links:
            if link not in seen:
                seen.add(link)
                job.posts.append(link)
                added += 1

        log(f"Collected posts: {len(job.posts)} (+{added})")

        if added == 0:
            idle_rounds += 1
        else:
            idle_rounds = 0

        if idle_rounds >= random.randint(4, 7):
            break


def scrape_background(job, context):
    username = job.username
    log(f"Scraping started for {username}")
    page = None
    try:
        page = context.new_page()

        human_pause(2.0, 5.0)
        page.goto(f"https://www.instagram.com/{username}/", wait_until="domcontentloaded")
        human_pause(6.0, 10.0)

        log(f"Current URL: {page.url}")
        if "challenge" in page.url:
            log("Instagram triggered a security challenge. Session is blocked.")
            return
        if "accounts/login" in page.url:
            log("Session expired. Instagram requires login.")
            return

        page.wait_for_load_state("networkidle")
        page.mouse.wheel(0, 1500)
        time.sleep(3)
        human_pause(1.2, 2.7)

        human_scroll_and_collect(page, job)

    except Exception as e:
        log(f"Scraper error: {e}")
    finally:
        try:
            if page:
                page.close()
        except Exception:
            pass


def playwright_worker(playwright_cookies):
    with sync_playwright() as play:
        browser = play.chromium.launch(
            headless=True,
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
            viewport={
                "width": random.randint(1220, 1380),
                "height": random.randint(780, 950),
            },
            locale="en-US",
            timezone_id="Asia/Kolkata",
        )

        context.add_cookies(playwright_cookies)
        print("Cookies loaded into browser:", len(playwright_cookies))

        page = context.new_page()
        page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
        page.mouse.move(100, 200)
        page.click("body")
        human_pause(2.5, 5.5)
        page.close()
        log("Instagram session activated")

        while True:
            job = job_queue.get()
            if job is None:
                break
            try:
                scrape_background(job, context)
            except Exception as e:
                log(f"Worker error: {e}")
            finally:
                job_queue.task_done()


class Job:
    def __init__(self, username):
        self.username = username
        self.posts = []
        self.sent = 0
        self.running = True


@bot.message_handler(commands=["start"])
def start(message):
    bot.send_message(message.chat.id, "Send Instagram username")


@bot.message_handler(func=lambda m: True)
def profile_handler(message):
    username = extract_username(message.text)
    if not username:
        bot.send_message(
            message.chat.id,
            "? Invalid input.\n\nSend:\n• Instagram username\n• Instagram profile link",
        )
        return

    info = get_profile_info(username)
    if not info:
        bot.send_message(message.chat.id, "? Could not load Instagram profile.")
        return

    caption = (
        f"?? Username: {info['username']}\n"
        f"?? Name: {info['fullname']}\n\n"
        f"?? Followers: {info['followers']}\n"
        f"?? Following: {info['following']}\n"
        f"?? Total Posts: {info['posts']}\n\n"
        f"?? Bio:\n{info['bio']}"
    )

    try:
        if info["pfp"]:
            bot.send_photo(message.chat.id, info["pfp"], caption=caption[:1024])
        else:
            bot.send_message(message.chat.id, caption)
    except Exception:
        bot.send_message(message.chat.id, caption)

    job = Job(username)
    user_jobs[message.chat.id] = job

    bot.send_message(message.chat.id, "Collecting posts from profile....\nPlease wait...")
    job_queue.put(job)

    wait_time = 0
    while len(job.posts) == 0 and wait_time < 50:
        time.sleep(2)
        wait_time += 2

    if len(job.posts) == 0:
        bot.send_message(
            message.chat.id,
            "? Failed to collect posts.\nInstagram may have blocked the request.",
        )
        return

    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("Download 10 Posts", callback_data="next"),
        InlineKeyboardButton("Cancel", callback_data="cancel"),
    )
    bot.send_message(
        message.chat.id,
        f"? {len(job.posts)} posts ready.\nPress download.",
        reply_markup=markup,
    )


@bot.callback_query_handler(func=lambda call: call.data == "cancel")
def cancel(call):
    job = user_jobs.get(call.message.chat.id)
    if job:
        job.running = False
    bot.send_message(call.message.chat.id, "Scraping stopped.")


@bot.callback_query_handler(func=lambda call: call.data == "next")
def send_next(call):
    job = user_jobs.get(call.message.chat.id)
    if not job:
        bot.send_message(call.message.chat.id, "No active job")
        return

    start_idx = job.sent
    end_idx = start_idx + 10
    posts = job.posts[start_idx:end_idx]

    if not posts:
        bot.send_message(call.message.chat.id, "No more collected posts yet.")
        return

    bot.send_message(call.message.chat.id, "Downloading media...")

    for post_url in posts:
        if not job.running:
            break

        try:
            medias = get_media_from_post_url(post_url)
            if not medias:
                bot.send_message(call.message.chat.id, f"?? No media found\n{post_url}")
                continue

            for media_type, media_url in medias:
                try:
                    if not media_url:
                        bot.send_message(call.message.chat.id, f"?? Empty media URL\n{post_url}")
                        continue

                    media_url = media_url.replace("&amp;", "&").replace(".heic", ".jpg")
                    response = throttled_get(media_url, timeout=45, stream=True)
                    if response.status_code != 200:
                        raise RuntimeError(f"Media download failed (HTTP {response.status_code})")

                    file = BytesIO(response.content)

                    if media_type == "video":
                        file.name = "video.mp4"
                        bot.send_video(
                            call.message.chat.id,
                            file,
                            width=720,
                            height=1280,
                            supports_streaming=True,
                        )
                    else:
                        img = Image.open(file).convert("RGB")
                        jpeg = BytesIO()
                        img.save(jpeg, format="JPEG", quality=92)
                        jpeg.seek(0)
                        bot.send_photo(call.message.chat.id, jpeg)

                    human_pause(1.8, 4.2, extra_chance=0.2)

                except Exception as e:
                    bot.send_message(
                        call.message.chat.id,
                        f"? Failed to send media\n\nPost:\n{post_url}\n\nReason:\n{e}",
                    )
                    human_pause(1.2, 2.4, extra_chance=0.0)

            human_pause(1.5, 3.5, extra_chance=0.2)

        except Exception as e:
            bot.send_message(
                call.message.chat.id,
                f"?? Error processing post\n\nPost:\n{post_url}\n\nReason:\n{e}",
            )
            human_pause(1.5, 3.0, extra_chance=0.15)

    job.sent += len(posts)

    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("Next 10", callback_data="next"),
        InlineKeyboardButton("Cancel", callback_data="cancel"),
    )
    bot.send_message(call.message.chat.id, f"Sent {job.sent} posts", reply_markup=markup)


def main():
    cookies = load_instagram_cookies(COOKIE_DIR)
    apply_cookies_to_requests(cookies)

    log("Bot started")

    threading.Thread(target=playwright_worker, args=(cookies,), daemon=True).start()
    bot.infinity_polling()


if __name__ == "__main__":
    main()


