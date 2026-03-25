import datetime
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
# BOT TOKEN
# =========================
TOKEN = "8665521420:AAHi0hfMNn3odVDCd9ajMCW_8FwrSz2OQLQ"
if not TOKEN:
    raise RuntimeError("Set TELEGRAM_BOT_TOKEN environment variable.")
bot = telebot.TeleBot(TOKEN, threaded=True)

job_queue = Queue()

# =========================
# INSTAGRAM SESSION
# =========================
IG_SESSIONID = "80454330558%3A5e12tyYRkvWdAh%3A1%3AAYjeFHAV6_xhi-7RLbWt2pFrfMiilvL80sysNuRNPQ"
if not IG_SESSIONID:
    raise RuntimeError("Set IG_SESSIONID environment variable.")


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
print("Instaloader session active")
print("Starting browser...")


def _extract_meta_content(html: str, attr: str, key: str) -> str:
    pattern = rf'<meta[^>]*{attr}="{re.escape(key)}"[^>]*content="([^"]*)"'
    m = re.search(pattern, html, flags=re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _get_profile_info_from_html(username: str):
    url = f"https://www.instagram.com/{username}/"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }
    r = requests.get(url, headers=headers, timeout=25)
    if r.status_code != 200:
        raise RuntimeError(f"Profile page HTTP {r.status_code}")

    html = r.text
    title = _extract_meta_content(html, "property", "og:title") or _extract_meta_content(html, "name", "title")
    desc = _extract_meta_content(html, "property", "og:description") or _extract_meta_content(
        html, "name", "description"
    )
    pfp = _extract_meta_content(html, "property", "og:image")

    full_name = "-"
    followers = "-"
    following = "-"
    posts = "-"

    # Example: "12.3M Followers, 50 Following, 120 Posts - See Instagram photos..."
    m = re.search(r"([0-9.,MKmk]+)\s+Followers,\s*([0-9.,MKmk]+)\s+Following,\s*([0-9.,MKmk]+)\s+Posts", desc)
    if m:
        followers = m.group(1)
        following = m.group(2)
        posts = m.group(3)

    # Example title: "username (@username) • Instagram photos and videos"
    t = re.search(r"^(.*?)\s*\(@", title)
    if t:
        full_name = t.group(1).strip() or "-"

    return {
        "username": username,
        "fullname": full_name,
        "followers": followers,
        "following": following,
        "posts": posts,
        "bio": "-",
        "pfp": pfp,
    }


def get_profile_info(username):
    try:
        profile = instaloader.Profile.from_username(L.context, username)
        return {
            "username": profile.username or username,
            "fullname": profile.full_name or "-",
            "followers": f"{profile.followers:,}",
            "following": f"{profile.followees:,}",
            "posts": f"{profile.mediacount:,}",
            "bio": (profile.biography or "-").strip(),
            "pfp": profile.profile_pic_url or "",
        }
    except Exception as e:
        log(f"Profile info via Instaloader failed: {e}")
        try:
            return _get_profile_info_from_html(username)
        except Exception as e2:
            log(f"Profile info fallback failed: {e2}")
            return None


def get_profile_posts(username, limit=100):
    posts = []
    profile = instaloader.Profile.from_username(L.context, username)
    for post in profile.get_posts():
        posts.append(post)
        if len(posts) >= limit:
            break
    log(f"Collected {len(posts)} posts using Instaloader")
    return posts


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
        post = instaloader.Post.from_shortcode(L.context, shortcode)
        return post
    except Exception as e:
        log(f"Instaloader error: {e}")
        return None


def _decode_ig_escaped_url(url):
    if not url:
        return ""
    cleaned = url.replace("\\u0026", "&").replace("\\/", "/")
    try:
        cleaned = bytes(cleaned, "utf-8").decode("unicode_escape")
    except Exception:
        pass
    return cleaned


def _extract_media_from_post_html(post_url):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.instagram.com/",
        "Cookie": f"sessionid={IG_SESSIONID}",
    }

    response = requests.get(post_url, headers=headers, timeout=25)
    if response.status_code != 200:
        raise RuntimeError(f"Post page HTTP {response.status_code}")

    html = response.text
    video_urls = re.findall(r'"video_url":"([^"]+)"', html)
    image_urls = re.findall(r'"display_url":"([^"]+)"', html)

    if not video_urls and not image_urls:
        og_video = _extract_meta_content(html, "property", "og:video")
        og_image = _extract_meta_content(html, "property", "og:image")
        if og_video:
            video_urls.append(og_video)
        if og_image:
            image_urls.append(og_image)

    media = []
    seen = set()
    for raw in video_urls:
        decoded = _decode_ig_escaped_url(raw)
        if decoded and decoded not in seen:
            seen.add(decoded)
            media.append(("video", decoded))
    for raw in image_urls:
        decoded = _decode_ig_escaped_url(raw)
        if decoded and decoded not in seen:
            seen.add(decoded)
            media.append(("photo", decoded))
    return media


def get_media_from_post_url(post_url):
    try:
        media = _extract_media_from_post_html(post_url)
        if media:
            return media
    except Exception as e:
        log(f"HTML media extraction failed: {e}")

    post = get_post_from_url(post_url)
    if post is not None:
        try:
            media = extract_media(post)
            if media:
                return media
        except Exception as e:
            log(f"Instaloader media extraction failed: {e}")
    return []


class Job:
    def __init__(self, username):
        self.username = username
        self.posts = []
        self.sent = 0
        self.running = True


user_jobs = {}


def scrape_background(job, context):
    username = job.username
    log(f"Scraping started for {username}")
    page = None

    try:
        page = context.new_page()
        url = f"https://www.instagram.com/{username}/"
        time.sleep(random.uniform(4, 7))
        page.goto(url, wait_until="domcontentloaded")
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
            window.scrollBy({
                top: 800,
                left: 0,
                behavior: 'smooth'
            });
            """
        )
        time.sleep(random.uniform(4, 6))

        for _ in range(20):
            if not job.running:
                break

            log("Scanning page for posts...")
            links = page.evaluate(
                """
                Array.from(document.querySelectorAll('a'))
                    .map(a => a.href)
                    .filter(h => h.includes('/p/') || h.includes('/reel/'))
                """
            )

            new_posts = 0
            for link in links:
                link = link.split("?")[0]
                if link not in job.posts:
                    job.posts.append(link)
                    new_posts += 1

            log(f"Collected posts: {len(job.posts)} (+{new_posts})")
            page.evaluate(
                """
                window.scrollBy({
                    top: 1200,
                    left: 0,
                    behavior: 'smooth'
                });
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
    log("Starting browser in worker thread...")
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


@bot.message_handler(func=lambda m: True)
def profile_handler(message):
    username = extract_username(message.text)
    if not username:
        bot.send_message(
            message.chat.id,
            "❌ Invalid input.\n\nSend:\n• Instagram username\n• Instagram profile link",
        )
        return

    info = get_profile_info(username)
    if not info:
        bot.send_message(message.chat.id, "❌ Could not load Instagram profile.")
        return

    caption = (
        f"ðŸ‘¤ Username: {info['username']}\n"
        f"ðŸ“› Name: {info['fullname']}\n\n"
        f"ðŸ“Š Followers: {info['followers']}\n"
        f"ðŸ“Š Following: {info['following']}\n"
        f"ðŸ“¸ Total Posts: {info['posts']}\n\n"
        f"ðŸ“ Bio:\n{(info['bio'] or '-')[:500]}"
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
            log(f"Processing: {post_url}")
            medias = get_media_from_post_url(post_url)
            if not medias:
                bot.send_message(call.message.chat.id, f"No media found\n{post_url}")
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
                    error_text = str(e)
                    log(f"Media error: {error_text}")
                    bot.send_message(
                        call.message.chat.id,
                        f"❌ Failed to send media\n\nPost:\n{post_url}\n\nReason:\n{error_text}",
                    )
        except Exception as e:
            error_text = str(e)
            log(f"Post processing error: {error_text}")
            bot.send_message(
                call.message.chat.id,
                f"⚠️ Error processing post\n\nPost:\n{post_url}\n\nReason:\n{error_text}",
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

