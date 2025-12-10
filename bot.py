import os
import time
import json
import html
import logging
from datetime import datetime, timedelta
from threading import Thread

import feedparser
import schedule
import requests
from flask import Flask, request
from telegram import Bot, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.utils.request import Request
from googletrans import Translator   # <-- NEW: Hindi translation

# ------------ ENVIRONMENT CONFIG ------------

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")  # e.g. -1001234567890
OWNER_ID = os.getenv("OWNER_ID")  # tumhara personal chat id (string)

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
    raise RuntimeError(
        "Missing environment variables! TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID set karo."
    )

# ------------ GLOBAL SETTINGS ------------

RSS_LINKS = [
    "https://news.google.com/rss?hl=en-IN&gl=IN&ceid=IN:hi",   # Hindi edition
    "https://feeds.reuters.com/reuters/worldNews",
    "https://feeds.bbci.co.uk/news/world/rss.xml",
]

NEWS_PER_RUN = 5          # har 15 min me max 5 news
sent_ids = set()          # yahan sab posted news IDs store hongi

IS_PAUSED = False         # admin pause/resume
LAST_RUN = None           # last successful run time
TOTAL_POSTS = 0           # total news posts

translator = Translator()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

request_ = Request(con_pool_size=8)
bot = Bot(token=TELEGRAM_BOT_TOKEN, request=request_)

app = Flask(__name__)


# ------------ HELPERS ------------

def is_owner(chat_id) -> bool:
    """Check kare ki ye owner hai ya nahi."""
    if not OWNER_ID:
        return True   # agar OWNER_ID set nahi hai to sab ko allow
    return str(chat_id) == OWNER_ID


def short_url(url: str) -> str:
    """TinyURL se link chhota karta hai."""
    try:
        r = requests.get(
            "https://tinyurl.com/api-create.php",
            params={"url": url},
            timeout=10
        )
        if r.status_code == 200:
            return r.text.strip()
        return url
    except Exception:
        return url


def to_hindi(text: str) -> str:
    """English text ko Hindi me translate karta hai."""
    if not text:
        return ""
    try:
        res = translator.translate(text, dest="hi")
        return res.text
    except Exception as ex:
        logging.warning(f"Translate fail: {ex}")
        return text   # fallback: original


def extract_image(entry) -> str | None:
    """RSS entry se thumbnail nikalne ki koshish."""
    try:
        if hasattr(entry, "media_content"):
            mc = entry.media_content
            if mc and isinstance(mc, list) and "url" in mc[0]:
                return mc[0]["url"]

        if hasattr(entry, "media_thumbnail"):
            mt = entry.media_thumbnail
            if mt and isinstance(mt, list) and "url" in mt[0]:
                return mt[0]["url"]

        if hasattr(entry, "image") and isinstance(entry.image, dict):
            return entry.image.get("href")
    except Exception:
        pass

    return None


def fetch_news():
    """Sabhi RSS se latest entries laata hai."""
    entries = []

    for url in RSS_LINKS:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:10]:
                eid = getattr(e, "id", None) or getattr(e, "link", None)
                if not eid:
                    continue

                entries.append({
                    "id": eid,
                    "title": getattr(e, "title", ""),
                    "link": getattr(e, "link", ""),
                    "summary": getattr(e, "summary", "") or getattr(e, "description", ""),
                    "image": extract_image(e),
                })
        except Exception as ex:
            logging.warning(f"RSS error ({url}): {ex}")

    # sabse purani → sabse nayi
    return entries[::-1]


def default_hashtags() -> str:
    return "#Breaking #WorldNews"


# ------------ MESSAGE FORMAT (Hindi) ------------

def format_message(title_en: str, summary_en: str, link: str, hashtags: str) -> str:
    # Hindi title + summary
    title_hi = to_hindi(title_en)
    summary_hi = to_hindi(summary_en or title_en)

    safe_title_hi = html.escape(title_hi)
    safe_summary_hi = html.escape(summary_hi)
    safe_tags = html.escape(hashtags)

    ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
    time_str = ist.strftime("%d %b %Y | %I:%M %p IST")

    short = short_url(link)

    msg = (
        f"🚨 <b>अंतरराष्ट्रीय ब्रेकिंग न्यूज़</b>\n"
        f"📅 <i>{time_str}</i>\n\n"
        f"📰 <b>{safe_title_hi}</b>\n\n"
        f"{safe_summary_hi}\n\n"
        f"🔗 पूरी खबर: <a href=\"{short}\">यहाँ देखें</a>\n\n"
        f"{safe_tags}\n"
        f"Powered by <a href=\"https://t.me/Axshchxhan\">@Axshchxhan</a>"
    )
    return msg


# ------------ MAIN POSTING JOB ------------

def post_news():
    global IS_PAUSED, LAST_RUN, TOTAL_POSTS

    if IS_PAUSED:
        logging.info("Bot paused hai, news post nahi kar raha.")
        return

    logging.info("Checking for new news...")
    entries = fetch_news()

    count = 0
    for e in entries:
        if count >= NEWS_PER_RUN:
            break

        if e["id"] in sent_ids:
            continue

        title = e["title"]
        link = e["link"]
        summary = e["summary"] or "ताज़ा अंतरराष्ट्रीय अपडेट।"
        image = e["image"]

        msg_text = format_message(title, summary, link, default_hashtags())

        # buttons
        buttons = [
            [
                InlineKeyboardButton("🌐 पूरी खबर", url=short_url(link)),
                InlineKeyboardButton("📢 Updates Channel", url="https://t.me/GlobalUpdates")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(buttons)

        try:
            if image:
                bot.send_photo(
                    chat_id=TELEGRAM_CHANNEL_ID,
                    photo=image,
                    caption=msg_text,
                    parse_mode="HTML",
                    reply_markup=reply_markup
                )
            else:
                bot.send_message(
                    chat_id=TELEGRAM_CHANNEL_ID,
                    text=msg_text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    reply_markup=reply_markup
                )

            sent_ids.add(e["id"])
            count += 1
            TOTAL_POSTS += 1
            LAST_RUN = datetime.utcnow() + timedelta(hours=5, minutes=30)
            time.sleep(2)

        except Exception as ex:
            logging.error(f"Send error: {ex}")
            sent_ids.add(e["id"])  # taaki repeat na ho


# ------------ DEMO UPDATE (ONLY OWNER DM) ------------

def send_demo_update():
    ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
    time_str = ist.strftime("%d %b %Y | %I:%M %p IST")

    text = (
        "🟢 <b>Ayush News Bot Updated</b>\n"
        f"🗓 <i>{time_str}</i>\n\n"
        "Demo: Bot सफलतापूर्वक चालू हो चुका है.\n"
        "अब हर 15 मिनट में लेटेस्ट international news + image Hindi में मिलेगी.\n\n"
        "#Update #LiveBot\n"
        "Powered by @Axshchxhan"
    )

    target = OWNER_ID if OWNER_ID else TELEGRAM_CHANNEL_ID

    try:
        bot.send_message(
            chat_id=target,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True
        )
        logging.info("Demo update sent.")
    except Exception as ex:
        logging.error(f"Demo update send fail: {ex}")


# ------------ TELEGRAM WEBHOOK (BotFather-style panel) ------------

@app.route(f"/{TELEGRAM_BOT_TOKEN}", methods=["POST"])
def telegram_webhook():
    global IS_PAUSED

    data = request.get_json(force=True)
    logging.info("Incoming update: %s", json.dumps(data))

    message = data.get("message") or data.get("edited_message")
    if not message:
        return "ok", 200

    chat = message.get("chat", {})
    chat_id = chat.get("id")
    text = (message.get("text") or "").strip()

    if not text:
        return "ok", 200

    # command ko normalize karo: / hatao, lowercase
    cmd = text.strip()
    if cmd.startswith("/"):
        cmd = cmd.split("@")[0][1:]  # /cmd@botname → cmd
    cmd = cmd.lower()

    # ----------- OWNER / CONTROL COMMANDS -----------

    if cmd in ("id", "myid"):
        if not is_owner(chat_id):
            bot.send_message(chat_id=chat_id, text="❌ Ye command sirf bot owner ke liye hai.")
        else:
            bot.send_message(
                chat_id=chat_id,
                text=f"🆔 Chat ID: `{chat_id}`",
                parse_mode="Markdown"
            )

    elif cmd in ("help", "panel", "menu"):
        if not is_owner(chat_id):
            bot.send_message(chat_id=chat_id, text="❌ Panel sirf bot owner ke liye hai.")
        else:
            ist_now = datetime.utcnow() + timedelta(hours=5, minutes=30)
            last = LAST_RUN.strftime("%d %b %Y | %I:%M %p IST") if LAST_RUN else "N/A"

            text_panel = (
                "🤖 <b>Ayush News Bot Control Panel</b>\n\n"
                "Commands (bina / ke bhi chalenge):\n"
                "• <code>status</code> – bot ki current स्थिति.\n"
                "• <code>pause</code> – news posting रोक दो.\n"
                "• <code>resume</code> – news posting चालू करो.\n"
                "• <code>id</code> – tumhara chat ID.\n\n"
                f"<b>Last run:</b> {last}\n"
                f"<b>Total posts:</b> {TOTAL_POSTS}\n"
                f"<b>Paused:</b> {'Yes' if IS_PAUSED else 'No'}\n"
                f"<b>Server time (IST approx):</b> {ist_now.strftime('%d %b %Y | %I:%M %p')}"
            )

            bot.send_message(
                chat_id=chat_id,
                text=text_panel,
                parse_mode="HTML"
            )

    elif cmd in ("status", "stats"):
        if not is_owner(chat_id):
            bot.send_message(chat_id=chat_id, text="❌ Status sirf bot owner ke liye hai.")
        else:
            last = LAST_RUN.strftime("%d %b %Y | %I:%M %p IST") if LAST_RUN else "N/A"
            msg = (
                f"📊 <b>Status</b>\n"
                f"• Last run: {last}\n"
                f"• Total posts: {TOTAL_POSTS}\n"
                f"• Paused: {'Yes' if IS_PAUSED else 'No'}"
            )
            bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML")

    elif cmd in ("pause", "stop"):
        if not is_owner(chat_id):
            bot.send_message(chat_id=chat_id, text="❌ Pause sirf bot owner ke liye hai.")
        else:
            IS_PAUSED = True
            bot.send_message(chat_id=chat_id, text="⏸ Bot paused. Ab new posts nahi jayengi.")

    elif cmd in ("resume", "startbot", "run"):
        if not is_owner(chat_id):
            bot.send_message(chat_id=chat_id, text="❌ Resume sirf bot owner ke liye hai.")
        else:
            IS_PAUSED = False
            bot.send_message(chat_id=chat_id, text="▶️ Bot resumed. Ab se news fir se jayegi.")

    # ----------- NORMAL USERS (ya random text) -----------

    elif cmd.startswith("start"):
        bot.send_message(
            chat_id=chat_id,
            text=(
                "👋 Namaste!\n"
                "Ye bot @GlobalUpdates channel ke लिए news automation karta hai.\n"
                "News sirf channel par post hoti hai.\n"
                "Owner ke liye commands: help, status, pause, resume, id."
            )
        )

    else:
        # koi bhi random text → chhota sa reply (BotFather jaisa)
        bot.send_message(
            chat_id=chat_id,
            text="🙂 Yeh bot sirf news automation ke लिए है. Control commands: help, status, pause, resume."
        )

    return "ok", 200


# ------------ SCHEDULER + FLASK HOME ------------

def scheduler_loop():
    while True:
        schedule.run_pending()
        time.sleep(1)


@app.route("/")
def home():
    return "Ayush Telegram News Bot Running!", 200


def main():
    logging.info("🔥 Ayush Telegram News Bot Started!")

    # har 15 minute
    schedule.every(15).minutes.do(post_news)

    # restart pe ek demo update (sirf owner DM)
    send_demo_update()

    t = Thread(target=scheduler_loop, daemon=True)
    t.start()

    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
