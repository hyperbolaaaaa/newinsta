"""
Username -> media Instagram Telegram bot (OG style, fixed version).

Flow:
1) User sends Instagram username/profile link.
2) Playwright (with IG session) opens profile and collects post links.
3) Instaloader resolves each post and extracts media URLs.
4) Bot sends media to Telegram.
"""

import datetime
import os
import random
import re
import threading
import time
from dataclasses import dataclass, field
from io import BytesIO
from queue import Queue
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import instaloader
import requests
import telebot
from PIL import Image
from playwright.sync_api import sync_playwright
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup


# =========================
# CONFIG (ENV ONLY)
# =========================

TOKEN = "8628280617:AAEHHRQZ2dxsxoFWvmLs1PVO_wSCRn0rHPc"
IG_SESSIONID = "80454330558%3A5e12tyYRkvWdAh%3A1%3AAYju-n_Ua5LGQXkCBU-Rm-NG_gJT8c9wXM2OBAN37w"
IG_STORAGE_STATE_PATH = os.getenv("IG_STORAGE_STATE_PATH", "").strip()
MAX_SCRAPED_POSTS = int(os.getenv("MAX_SCRAPED_POSTS", "120"))
SEND_BATCH_SIZE = int(os.getenv("SEND_BATCH_SIZE", "10"))

if not TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN env var.")
if not IG_SESSIONID and not IG_STORAGE_STATE_PATH:
    raise RuntimeError("Set IG_SESSIONID or IG_STORAGE_STATE_PATH.")

bot = telebot.TeleBot(TOKEN, threaded=True)


def log(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")


def extract_username(text: str) -> Optional[str]:
    raw = (text or "").strip().split("?")[0].rstrip("/")
    if not raw:
        return None
    if USERNAME_RE.fullmatch(raw):
        return raw.lower()

    if not raw.startswith(("http://", "https://")):
        raw = f"https://{raw}"
    try:
        parsed = urlparse(raw)
    except Exception:
        return None

    if "instagram.com" not in parsed.netloc.lower():
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if not parts:
        return None
    username = parts[0]
    return username.lower() if USERNAME_RE.fullmatch(username) else None


# =========================
# INSTALOADER
# =========================

L = instaloader.Instaloader(
    download_pictures=False,
    download_videos=False,
    download_video_thumbnails=False,
    save_metadata=False,
    download_comments=False,
)

if IG_SESSIONID:
    L.context._session.cookies.set("sessionid", IG_SESSIONID, domain=".instagram.com")
    log("Instaloader session cookie loaded")


def get_post_from_url(post_url: str) -> Optional[instaloader.Post]:
    try:
        m = re.search(r"(?:p|reel|tv)/([^/?#]+)", post_url)
        if not m:
            return None
        shortcode = m.group(1)
        return instaloader.Post.from_shortcode(L.context, shortcode)
    except Exception as exc:
        log(f"Instaloader post load failed: {exc}")
        return None


def extract_media(post: instaloader.Post) -> List[Tuple[str, str]]:
    items: List[Tuple[str, str]] = []
    if post.typename == "GraphSidecar":
        for node in post.get_sidecar_nodes():
            if node.is_video and node.video_url:
                items.append(("video", node.video_url))
            elif node.display_url:
                items.append(("photo", node.display_url))
    elif post.is_video and post.video_url:
        items.append(("video", post.video_url))
    elif post.url:
        items.append(("photo", post.url))
    return items


# =========================
# JOB MODEL
# =========================


@dataclass
class Job:
    chat_id: int
    username: str
    posts: List[str] = field(default_factory=list)
    sent: int = 0
    running: bool = True
    ready: bool = False
    failed: bool = False
    fail_reason: str = ""


jobs_by_chat: Dict[int, Job] = {}
job_queue: "Queue[Optional[Job]]" = Queue()
job_lock = threading.Lock()


def set_job(chat_id: int, job: Job) -> None:
    with job_lock:
        jobs_by_chat[chat_id] = job


def get_job(chat_id: int) -> Optional[Job]:
    with job_lock:
        return jobs_by_chat.get(chat_id)


# =========================
# SCRAPER
# =========================


def scrape_profile_posts(job: Job, context) -> None:
    page = None
    try:
        page = context.new_page()
        url = f"https://www.instagram.com/{job.username}/"

        time.sleep(random.uniform(2.0, 3.5))
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_load_state("networkidle", timeout=45000)
        time.sleep(random.uniform(1.5, 2.5))

        if "challenge" in page.url:
            raise RuntimeError("Instagram challenge page detected.")
        if "accounts/login" in page.url:
            raise RuntimeError("Instagram redirected to login (session expired).")

        for _ in range(25):
            if not job.running:
                break

            hrefs = page.evaluate(
                """
                Array.from(document.querySelectorAll('a[href]'))
                  .map(a => a.getAttribute('href'))
                  .filter(Boolean)
                """
            )
            new_count = 0
            for href in hrefs:
                if "/p/" not in href and "/reel/" not in href and "/tv/" not in href:
                    continue
                link = f"https://www.instagram.com{href.split('?')[0]}".rstrip("/")
                if link not in job.posts:
                    job.posts.append(link)
                    new_count += 1

            log(f"@{job.username}: total={len(job.posts)} (+{new_count})")
            if len(job.posts) >= MAX_SCRAPED_POSTS:
                break

            page.evaluate("window.scrollBy(0, 1400)")
            time.sleep(random.uniform(1.5, 2.7))

        if len(job.posts) == 0:
            raise RuntimeError("No posts discovered.")

        job.ready = True
    except Exception as exc:
        job.failed = True
        job.fail_reason = str(exc)
    finally:
        if page:
            try:
                page.close()
            except Exception:
                pass


def send_download_keyboard(chat_id: int, total_posts: int) -> None:
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton(f"Download {SEND_BATCH_SIZE} Posts", callback_data="next"),
        InlineKeyboardButton("Cancel", callback_data="cancel"),
    )
    bot.send_message(chat_id, f"Collected {total_posts} posts. Press download.", reply_markup=markup)


def playwright_worker() -> None:
    log("Starting Playwright worker...")
    with sync_playwright() as play:
        browser = play.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        if IG_STORAGE_STATE_PATH and os.path.exists(IG_STORAGE_STATE_PATH):
            context = browser.new_context(storage_state=IG_STORAGE_STATE_PATH)
            log("Playwright context started with storage state")
        else:
            context = browser.new_context()
            if IG_SESSIONID:
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
                log("Playwright context started with sessionid cookie")

        while True:
            job = job_queue.get()
            if job is None:
                break
            try:
                log(f"Scraping started for @{job.username}")
                scrape_profile_posts(job, context)
                if job.failed:
                    bot.send_message(job.chat_id, f"Failed to collect posts: {job.fail_reason}")
                elif job.running:
                    send_download_keyboard(job.chat_id, len(job.posts))
            except Exception as exc:
                log(f"Worker error: {exc}")
                bot.send_message(job.chat_id, f"Scrape error: {exc}")
            finally:
                job_queue.task_done()

        context.close()
        browser.close()


# =========================
# TELEGRAM HANDLERS
# =========================


@bot.message_handler(commands=["start"])
def start(message):
    bot.send_message(
        message.chat.id,
        "Send Instagram username or profile URL.\nExample: natgeo or https://www.instagram.com/natgeo/",
    )


@bot.message_handler(func=lambda m: True)
def profile_handler(message):
    username = extract_username(message.text)
    if not username:
        bot.send_message(
            message.chat.id,
            "Invalid input. Send Instagram username or profile link.",
        )
        return

    old_job = get_job(message.chat.id)
    if old_job and old_job.running and not old_job.ready:
        bot.send_message(message.chat.id, "A scrape is already running. Please wait.")
        return

    job = Job(chat_id=message.chat.id, username=username)
    set_job(message.chat.id, job)
    bot.send_message(message.chat.id, f"Collecting posts from @{username} ...")
    job_queue.put(job)


@bot.callback_query_handler(func=lambda call: call.data == "cancel")
def cancel(call):
    job = get_job(call.message.chat.id)
    if job:
        job.running = False
    bot.send_message(call.message.chat.id, "Stopped.")


def send_media(chat_id: int, media_type: str, media_url: str, post_url: str) -> None:
    media_url = media_url.replace("&amp;", "&").replace(".heic", ".jpg")
    response = requests.get(media_url, timeout=40, stream=True)
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code} while downloading media")

    file_like = BytesIO(response.content)
    if media_type == "video":
        file_like.name = "video.mp4"
        bot.send_video(chat_id, file_like, supports_streaming=True)
    else:
        image = Image.open(file_like).convert("RGB")
        jpeg = BytesIO()
        image.save(jpeg, format="JPEG")
        jpeg.seek(0)
        bot.send_photo(chat_id, jpeg)
    time.sleep(random.uniform(1.0, 2.2))


@bot.callback_query_handler(func=lambda call: call.data == "next")
def send_next(call):
    job = get_job(call.message.chat.id)
    if not job:
        bot.send_message(call.message.chat.id, "No active job.")
        return
    if job.failed:
        bot.send_message(call.message.chat.id, f"Job failed: {job.fail_reason}")
        return
    if not job.ready:
        bot.send_message(call.message.chat.id, "Still collecting posts. Try again in a few seconds.")
        return

    start_idx = job.sent
    end_idx = min(start_idx + SEND_BATCH_SIZE, len(job.posts))
    posts = job.posts[start_idx:end_idx]
    if not posts:
        bot.send_message(call.message.chat.id, "No more posts to send.")
        return

    bot.send_message(call.message.chat.id, f"Downloading {len(posts)} posts...")

    for post_url in posts:
        try:
            post = get_post_from_url(post_url)
            if not post:
                bot.send_message(call.message.chat.id, f"Could not load post:\n{post_url}")
                continue

            medias = extract_media(post)
            if not medias:
                bot.send_message(call.message.chat.id, f"No media found:\n{post_url}")
                continue

            for media_type, media_url in medias:
                try:
                    send_media(call.message.chat.id, media_type, media_url, post_url)
                except Exception as exc:
                    bot.send_message(
                        call.message.chat.id,
                        f"Failed media in post:\n{post_url}\nReason: {exc}",
                    )
        except Exception as exc:
            bot.send_message(call.message.chat.id, f"Error processing post:\n{post_url}\nReason: {exc}")

    job.sent = end_idx
    remaining = len(job.posts) - job.sent
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton(f"Next {SEND_BATCH_SIZE}", callback_data="next"),
        InlineKeyboardButton("Cancel", callback_data="cancel"),
    )
    bot.send_message(
        call.message.chat.id,
        f"Sent: {job.sent}/{len(job.posts)} posts. Remaining: {remaining}",
        reply_markup=markup,
    )


if __name__ == "__main__":
    print("Bot started")
    threading.Thread(target=playwright_worker, daemon=True).start()
    bot.infinity_polling(skip_pending=True)
