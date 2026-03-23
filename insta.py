import asyncio
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List
from urllib.parse import urlparse

import instaloader
from dotenv import load_dotenv
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters


PROFILE_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
POST_PATH_PARTS = ("/p/", "/reel/", "/tv/")
MEDIA_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".mp4"}


@dataclass
class Config:
    telegram_bot_token: str
    ig_storage_state_path: Path
    ig_user_agent: str
    max_posts_per_request: int
    headless: bool
    request_timeout_ms: int
    instaloader_username: str
    instaloader_password: str
    instaloader_session_file: str


def load_config() -> Config:
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN is required.")

    storage_state = Path(os.getenv("IG_STORAGE_STATE_PATH", "ig_storage_state.json")).resolve()
    user_agent = os.getenv(
        "IG_USER_AGENT",
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
    )
    max_posts = int(os.getenv("MAX_POSTS_PER_REQUEST", "5"))
    headless = os.getenv("PLAYWRIGHT_HEADLESS", "true").lower() in {"1", "true", "yes"}
    timeout_ms = int(os.getenv("PLAYWRIGHT_TIMEOUT_MS", "60000"))

    return Config(
        telegram_bot_token=token,
        ig_storage_state_path=storage_state,
        ig_user_agent=user_agent,
        max_posts_per_request=max_posts,
        headless=headless,
        request_timeout_ms=timeout_ms,
        instaloader_username=os.getenv("IG_USERNAME", "").strip(),
        instaloader_password=os.getenv("IG_PASSWORD", "").strip(),
        instaloader_session_file=os.getenv("IG_INSTALOADER_SESSIONFILE", "").strip(),
    )


def extract_username(value: str) -> str:
    raw = value.strip().rstrip("/")
    if not raw:
        return ""
    if PROFILE_RE.fullmatch(raw):
        return raw

    if not raw.startswith("http://") and not raw.startswith("https://"):
        raw = f"https://{raw}"

    try:
        parsed = urlparse(raw)
    except Exception:
        return ""

    if "instagram.com" not in parsed.netloc.lower():
        return ""

    parts = [p for p in parsed.path.split("/") if p]
    if not parts:
        return ""
    username = parts[0]
    return username if PROFILE_RE.fullmatch(username) else ""


async def collect_profile_post_links(config: Config, username: str) -> List[str]:
    if not config.ig_storage_state_path.exists():
        raise FileNotFoundError(
            f"Missing Instagram storage state file: {config.ig_storage_state_path}. "
            "Run create_ig_session.py first."
        )

    profile_url = f"https://www.instagram.com/{username}/"
    links: List[str] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=config.headless)
        context = await browser.new_context(
            storage_state=str(config.ig_storage_state_path),
            user_agent=config.ig_user_agent,
            viewport={"width": 1366, "height": 768},
        )
        page = await context.new_page()
        page.set_default_timeout(config.request_timeout_ms)

        try:
            await page.goto(profile_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(1500)
            for _ in range(4):
                hrefs = await page.eval_on_selector_all(
                    "a[href]",
                    "els => els.map(e => e.getAttribute('href')).filter(Boolean)",
                )
                for href in hrefs:
                    if any(part in href for part in POST_PATH_PARTS):
                        full = f"https://www.instagram.com{href.split('?')[0]}".rstrip("/")
                        if full not in links:
                            links.append(full)
                            if len(links) >= config.max_posts_per_request:
                                return links
                await page.mouse.wheel(0, 2500)
                await page.wait_for_timeout(1200)
        except PlaywrightTimeoutError:
            logging.warning("Playwright timeout while reading profile %s", username)
        finally:
            await context.close()
            await browser.close()

    return links


def _create_loader(config: Config, download_dir: str) -> instaloader.Instaloader:
    loader = instaloader.Instaloader(
        dirname_pattern=download_dir,
        save_metadata=False,
        compress_json=False,
        download_comments=False,
        download_video_thumbnails=False,
        post_metadata_txt_pattern="",
        storyitem_metadata_txt_pattern="",
    )
    if config.instaloader_session_file and config.instaloader_username:
        try:
            loader.load_session_from_file(config.instaloader_username, config.instaloader_session_file)
            return loader
        except Exception as exc:
            logging.warning("Instaloader session file failed: %s", exc)

    if config.instaloader_username and config.instaloader_password:
        loader.login(config.instaloader_username, config.instaloader_password)
    return loader


def _download_post_via_instaloader(config: Config, post_link: str, download_dir: str) -> List[Path]:
    match = re.search(r"/(p|reel|tv)/([^/?#]+)/?", post_link)
    if not match:
        return []

    shortcode = match.group(2)
    loader = _create_loader(config, download_dir)
    post = instaloader.Post.from_shortcode(loader.context, shortcode)
    target = f"post_{shortcode}"
    loader.download_post(post, target=target)

    files = []
    for file in Path(download_dir).rglob("*"):
        if file.is_file() and file.suffix.lower() in MEDIA_EXTS:
            files.append(file)
    return sorted(files)


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _ = context
    await update.message.reply_text(
        "Send Instagram username or profile URL.\n"
        "Example: `natgeo` or `https://www.instagram.com/natgeo/`",
        parse_mode="Markdown",
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    config: Config = context.application.bot_data["config"]
    username = extract_username(update.message.text or "")
    if not username:
        await update.message.reply_text("Invalid input. Send a valid Instagram username or profile URL.")
        return

    await update.message.reply_text(f"Fetching recent posts from @{username} ...")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    try:
        post_links = await collect_profile_post_links(config, username)
    except FileNotFoundError as exc:
        await update.message.reply_text(str(exc))
        return
    except Exception as exc:
        logging.exception("Playwright profile scrape failed")
        await update.message.reply_text(f"Failed while opening profile: {exc}")
        return

    if not post_links:
        await update.message.reply_text("No posts found (or profile is private/inaccessible).")
        return

    await update.message.reply_text(f"Found {len(post_links)} post link(s). Downloading and sending media...")

    for link in post_links:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_DOCUMENT)
        with tempfile.TemporaryDirectory(prefix="ig_post_") as tmp_dir:
            try:
                files = await asyncio.to_thread(_download_post_via_instaloader, config, link, tmp_dir)
            except Exception as exc:
                logging.warning("Download failed for %s: %s", link, exc)
                await update.message.reply_text(f"Failed for {link}")
                continue

            if not files:
                await update.message.reply_text(f"No downloadable media for {link}")
                continue

            await update.message.reply_text(f"Post: {link}")
            for file_path in files:
                try:
                    with file_path.open("rb") as fp:
                        await update.message.reply_document(document=fp, filename=file_path.name)
                except Exception as exc:
                    logging.warning("Telegram upload failed for %s: %s", file_path, exc)
                    await update.message.reply_text(f"Upload failed: {file_path.name}")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    config = load_config()
    app = Application.builder().token(config.telegram_bot_token).build()
    app.bot_data["config"] = config

    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logging.info("Bot started.")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
