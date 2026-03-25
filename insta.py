
import datetime
import random
import re
import threading
import time
import os
from io import BytesIO
from queue import Queue

import instaloader
import requests
import telebot
from PIL import Image
from playwright.sync_api import sync_playwright
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

# =========================
# BOT TOKEN / IG SESSION (ENV)
# =========================
IG_SESSIONID = "80454330558%3A6xxAHoQjGqIUDT%3A27%3AAYgZk2reSBLrtyuCPKl9Dhkky7hhxhF3Dn5txcVLww" 
TOKEN = "8665521420:AAHi0hfMNn3odVDCd9ajMCW_8FwrSz2OQLQ"
if not TOKEN:
    raise RuntimeError("Set TELEGRAM_BOT_TOKEN env var.")
if not IG_SESSIONID:
    raise RuntimeError("Set IG_SESSIONID env var.")

bot = telebot.TeleBot(TOKEN, threaded=True)
job_queue = Queue()


def log(msg):
    t = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{t}] {msg}")


L = instaloader.Instaloader(
    download_pictures=False,
    download_videos=False,
    download_video_thumbnails=False,
    save_metadata=False,
)
L.context._session.cookies.set("sessionid", IG_SESSIONID, domain=".instagram.com")
L.context.max_connection_attempts = 1


def _ig_headers():
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.instagram.com/",
        "Cookie": f"sessionid={IG_SESSIONID}",
    }


def _decode_ig_url(url):
    if not url:
        return ""
    cleaned = url.replace("\\u0026", "&").replace("\\/", "/")
    try:
        cleaned = bytes(cleaned, "utf-8").decode("unicode_escape")
    except Exception:
        pass
    return cleaned


def _extract_meta(html, attr, key):
    m = re.search(rf'<meta[^>]*{attr}="{re.escape(key)}"[^>]*content="([^"]+)"', html, flags=re.IGNORECASE)
    return _decode_ig_url(m.group(1).strip()) if m else ""


def get_profile_info(username):
    try:
        url = f"https://www.instagram.com/{username}/"
        response = requests.get(url, headers=_ig_headers(), timeout=25)
        if response.status_code != 200:
            raise RuntimeError(f"Profile page HTTP {response.status_code}")

        html = response.text
        og_title = _extract_meta(html, "property", "og:title")
        og_desc = _extract_meta(html, "property", "og:description")
        pfp = _extract_meta(html, "property", "og:image")

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
    response = requests.get(post_url, headers=_ig_headers(), timeout=25)
    if response.status_code != 200:
        raise RuntimeError(f"Post page HTTP {response.status_code}")

    html = response.text
    items = []
    seen = set()

    for raw in re.findall(r'"video_url":"([^"]+)"', html):
        u = _decode_ig_url(raw)
        if u and u not in seen:
            seen.add(u)
            items.append(("video", u))

    for raw in re.findall(r'"display_url":"([^"]+)"', html):
        u = _decode_ig_url(raw)
        if u and u not in seen:
            seen.add(u)
            items.append(("photo", u))

    if not items:
        og_video = _extract_meta(html, "property", "og:video")
        og_image = _extract_meta(html, "property", "og:image")
        if og_video:
            items.append(("video", og_video))
        elif og_image:
            items.append(("photo", og_image))

    return items


def extract_media(post):
    items = []
    if post.typename == "GraphSidecar":
        for node in post.get_sidecar_nodes():
            if node.is_video:
                items.append(("video", node.video_url))
            else:
                items.append(("photo", node.display_url))
    elif post.is_video:
        items.append(("video", post.video_url))
    else:
        items.append(("photo", post.url))
    return items


def get_post_from_url(post_url):
    try:
        shortcode = re.search(r"(?:p|reel|tv)/([^/?]+)", post_url).group(1)
        return instaloader.Post.from_shortcode(L.context, shortcode)
    except Exception as e:
        log(f"Instaloader error: {e}")
        return None


def scrape_background(job, context):
    username = job.username
    log(f"Scraping started for {username}")
    page = None
    try:
        page = context.new_page()
        page.goto(f"https://www.instagram.com/{username}/", wait_until="domcontentloaded")
        time.sleep(5)

        log(f"Current URL: {page.url}")
        if "challenge" in page.url:
            log("Instagram triggered a security challenge. Session is blocked.")
            return
        if "accounts/login" in page.url:
            log("Session expired. Instagram requires login.")
            return

        page.wait_for_load_state("networkidle")
        time.sleep(3)

        page.evaluate(
            """
            window.scrollBy({ top: 800, left: 0, behavior: 'smooth' });
            """
        )
        time.sleep(random.uniform(4, 6))

        for _ in range(20):
            if not job.running:
                break
            links = page.evaluate(
                """
                Array.from(document.querySelectorAll('a'))
                    .map(a => a.href)
                    .filter(h => h.includes('/p/') || h.includes('/reel/'))
                """
            )
            for link in links:
                link = link.split("?")[0]
                if link not in job.posts:
                    job.posts.append(link)
            page.evaluate(
                """
                window.scrollBy({ top: 1200, left: 0, behavior: 'smooth' });
                """
            )
            time.sleep(3)
    except Exception as e:
        log(f"Scraper error: {e}")
    finally:
        try:
            if page:
                page.close()
        except Exception:
            pass


def playwright_worker():
    with sync_playwright() as play:
        browser = play.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context = browser.new_context()
        context.add_cookies(
            [
                {
                    "name": "sessionid",
                    "value": IG_SESSIONID,
                    "domain": ".instagram.com",
                    "path": "/",
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "None",
                }
            ]
        )
        page = context.new_page()
        page.goto("https://www.instagram.com/")
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


def extract_username(text):
    text = (text or "").strip().split("?")[0]
    match = re.search(r"instagram\.com/([^/]+)/?", text)
    if match:
        return match.group(1).lower()
    if re.match(r"^[a-zA-Z0-9._]+$", text):
        return text.lower()
    return None


@bot.message_handler(commands=["start"])
def start(message):
    bot.send_message(message.chat.id, "Send Instagram username")


class Job:
    def __init__(self, username):
        self.username = username
        self.posts = []
        self.sent = 0
        self.running = True


user_jobs = {}


@bot.message_handler(func=lambda m: True)
def profile_handler(message):
    username = extract_username(message.text)
    if not username:
        bot.send_message(
            message.chat.id,
            "❌ Invalid input.\n\nSend:\n• Instagram username\n• Instagram profile link",
        )
        return

    # profile info first
    info = get_profile_info(username)
    if not info:
        bot.send_message(message.chat.id, "❌ Could not load Instagram profile.")
        return

    caption = (
        f"👤 Username: {info['username']}\n"
        f"📛 Name: {info['fullname']}\n\n"
        f"📊 Followers: {info['followers']}\n"
        f"📊 Following: {info['following']}\n"
        f"📸 Total Posts: {info['posts']}\n\n"
        f"📝 Bio:\n{info['bio']}"
    )

    try:
        bot.send_photo(message.chat.id, info["pfp"], caption=caption[:1024])
    except Exception:
        bot.send_message(message.chat.id, caption)

    job = Job(username)
    user_jobs[message.chat.id] = job

    bot.send_message(message.chat.id, "Collecting posts from profile....\nPlease wait...")
    job_queue.put(job)

    wait_time = 0
    while len(job.posts) == 0 and wait_time < 40:
        time.sleep(2)
        wait_time += 2

    if len(job.posts) == 0:
        bot.send_message(
            message.chat.id,
            "❌ Failed to collect posts.\nInstagram may have blocked the request.",
        )
        return

    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("Download 10 Posts", callback_data="next"),
        InlineKeyboardButton("Cancel", callback_data="cancel"),
    )
    bot.send_message(
        message.chat.id,
        f"✅ {len(job.posts)} posts ready.\nPress download.",
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

    start = job.sent
    end = start + 10
    posts = job.posts[start:end]
    bot.send_message(call.message.chat.id, "Downloading media...")

    for post_url in posts:
        try:
            medias = get_media_from_post_url(post_url)
            if not medias:
                bot.send_message(call.message.chat.id, f"⚠️ No media found\n{post_url}")
                continue

            for media_type, media_url in medias:
                try:
                    if not media_url:
                        bot.send_message(call.message.chat.id, f"⚠️ Empty media URL\n{post_url}")
                        continue
                    media_url = media_url.replace("&amp;", "&").replace(".heic", ".jpg")
                    response = requests.get(media_url, timeout=30, stream=True)
                    if response.status_code != 200:
                        raise Exception(f"Media download failed (HTTP {response.status_code})")

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
                        img.save(jpeg, format="JPEG")
                        jpeg.seek(0)
                        bot.send_photo(call.message.chat.id, jpeg)
                    time.sleep(random.uniform(1.5, 3))
                except Exception as e:
                    bot.send_message(
                        call.message.chat.id,
                        f"❌ Failed to send media\n\nPost:\n{post_url}\n\nReason:\n{e}",
                    )
        except Exception as e:
            bot.send_message(
                call.message.chat.id,
                f"⚠️ Error processing post\n\nPost:\n{post_url}\n\nReason:\n{e}",
            )
            time.sleep(random.uniform(1.5, 3))

    job.sent += len(posts)
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("Next 10", callback_data="next"),
        InlineKeyboardButton("Cancel", callback_data="cancel"),
    )
    bot.send_message(call.message.chat.id, f"Sent {job.sent} posts", reply_markup=markup)


print("Bot started")
threading.Thread(target=playwright_worker, daemon=True).start()
bot.infinity_polling()
