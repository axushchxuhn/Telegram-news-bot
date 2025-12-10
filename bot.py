import os
import time
import json
import html
import logging
import textwrap
import re
from datetime import datetime, timedelta
from threading import Thread

import feedparser
import requests
from flask import Flask, request as flask_request
from telegram import Bot, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.utils.request import Request
from googletrans import Translator


# =============== ENVIRONMENT CONFIG ===============

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")  # -100… format

OWNER_ID = int(os.getenv("OWNER_ID", "0") or "0")
ADMIN_USER_IDS_RAW = os.getenv("ADMIN_USER_IDS", "")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_API_URL = os.getenv("DEEPSEEK_API_URL", "").strip()
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "") or "deepseek-chat"

SELF_PING_URL = os.getenv("SELF_PING_URL", "").strip()

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
    raise RuntimeError("TELEGRAM_BOT_TOKEN & TELEGRAM_CHANNEL_ID missing!")

def parse_admin_ids(raw: str):
    ids = set()
    raw = raw.strip()
    if not raw:
        return ids
    for part in re.split(r"[,\s]+", raw):
        if not part:
            continue
        try:
            ids.add(int(part))
        except:
            pass
    return ids

ADMIN_USER_IDS = parse_admin_ids(ADMIN_USER_IDS_RAW)
if OWNER_ID:
    ADMIN_USER_IDS.add(OWNER_ID)

CONTROL_CHAT_ID = OWNER_ID  # Bot will DM owner

# =============== GLOBAL SETTINGS ===============

RSS_LINKS = [
    "https://news.google.com/rss?hl=en-IN&gl=IN&ceid=IN:en",
    "https://feeds.reuters.com/reuters/worldNews",
    "https://feeds.bbci.co.uk/news/world/rss.xml",
]

NEWS_PER_RUN = 5
NEWS_INTERVAL_MINUTES = 30

sent_ids = set()

POSTING_PAUSED = False
total_posts = 0
last_run_ts = 0
last_error_text = ""

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

request = Request(con_pool_size=8)
bot = Bot(token=TELEGRAM_BOT_TOKEN, request=request)
translator = Translator()

app = Flask(__name__)


# =============== TIME HELPERS ===============

def now_ist():
    return datetime.utcnow() + timedelta(hours=5, minutes=30)

def format_ist(dt: datetime):
    return dt.strftime("%d %b %Y | %I:%M %p IST")


# =============== BASIC HELPERS ===============

def is_admin(uid: int):
    return uid in ADMIN_USER_IDS

def short_url(url: str):
    try:
        r = requests.get("https://tinyurl.com/api-create.php", params={"url": url}, timeout=10)
        if r.status_code == 200 and r.text:
            return r.text.strip()
    except:
        pass
    return url

def clean(text: str):
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()

def english_summary(title, desc):
    base = clean(desc) or clean(title)
    return (base[:260] + "...") if len(base) > 260 else base

def to_hindi(text: str):
    try:
        return translator.translate(text, src="en", dest="hi").text
    except:
        return text
      # =============== AI SUMMARY (OpenAI + Deepseek + Fallback) ===============

def ai_summary_hi(title, desc, link):
    system_prompt = (
        "Tum ek professional Hindi news editor ho. "
        "Sirf Hindi me 3 line ki simple summary do. "
        "Koi opinion / analysis nahi. Sirf facts."
    )

    user_text = f"Title: {title}\n\nDescription: {desc}\n\nLink: {link}"

    summary_hi = ""
    hashtags = "#WorldNews #Breaking #Update"

    # ----- TRY OPENAI -----
    if OPENAI_API_KEY:
        try:
            payload = {
                "model": "gpt-4.1-mini",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text},
                ],
                "max_tokens": 220,
                "temperature": 0.5,
            }
            r = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}",
                         "Content-Type": "application/json"},
                json=payload,
                timeout=25,
            )
            data = r.json()
            summary_hi = data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            logging.error(f"OpenAI error: {e}")

    # ----- TRY DEEPSEEK -----
    if (not summary_hi) and DEEPSEEK_API_KEY and DEEPSEEK_API_URL:
        try:
            payload = {
                "model": DEEPSEEK_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text},
                ],
                "max_tokens": 200,
                "temperature": 0.4,
            }
            r = requests.post(
                DEEPSEEK_API_URL,
                headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                         "Content-Type": "application/json"},
                json=payload,
                timeout=25,
            )
            data = r.json()
            summary_hi = data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            logging.error(f"DeepSeek error: {e}")

    # ----- FALLBACK: English -> Hindi -----
    if not summary_hi:
        en = english_summary(title, desc)
        summary_hi = to_hindi(en)

    return summary_hi, hashtags


# =============== FETCH NEWS ===============

def fetch_news():
    news = []

    for url in RSS_LINKS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:10]:
                nid = getattr(entry, "id", None) or getattr(entry, "link", None)
                if not nid:
                    continue
                news.append({
                    "id": nid,
                    "title": getattr(entry, "title", ""),
                    "link": getattr(entry, "link", ""),
                    "summary": getattr(entry, "summary", "")
                               or getattr(entry, "description", ""),
                    "entry": entry,
                })
        except Exception as e:
            logging.error(f"RSS fetch error: {e}")

    return news[::-1]  # newest last


# =============== IMAGE EXTRACTOR ===============

def extract_image(entry):
    try:
        mc = getattr(entry, "media_content", None)
        if mc and isinstance(mc, list) and mc and mc[0].get("url"):
            return mc[0]["url"]
    except:
        pass

    try:
        mt = getattr(entry, "media_thumbnail", None)
        if mt and isinstance(mt, list) and mt and mt[0].get("url"):
            return mt[0]["url"]
    except:
        pass

    try:
        for link in getattr(entry, "links", []):
            if link.get("type", "").startswith("image/"):
                return link.get("href")
    except:
        pass

    return None


# =============== FORMAT NEWS MESSAGE ===============

def format_message(title, summary_hi, url, tags):
    safe_title = html.escape(title)
    safe_summary = html.escape(summary_hi)
    safe_tags = html.escape(tags)

    ist = now_ist()
    time_str = format_ist(ist)
    short = short_url(url)

    return (
        f"📰 <b>International Breaking News</b>\n"
        f"📅 <i>{time_str}</i>\n\n"
        f"🔴 <b>{safe_title}</b>\n\n"
        f"{safe_summary}\n\n"
        f"🔗 <b>Full Story:</b> <a href=\"{short}\">यहाँ पढ़ें</a>\n\n"
        f"{safe_tags}\n"
        f"<i>Powered by @Axshchxhan</i>"
    )


# =============== POST NEWS RUN ===============

def run_post_cycle():
    global total_posts, last_error_text

    logging.info("Checking for new news...")
    items = fetch_news()
    count = 0

    for item in items:
        if count >= NEWS_PER_RUN:
            break

        nid = item["id"]
        if nid in sent_ids:
            continue

        title = item["title"]
        link = item["link"]
        desc = item["summary"]
        entry = item["entry"]

        try:
            summary_hi, tags = ai_summary_hi(title, desc, link)
            msg = format_message
          # =============== MORNING & NIGHT BRIEFS ===============

def send_brief(kind):
    if not CONTROL_CHAT_ID:
        return

    ist = now_ist()
    if kind == "morning":
        msg = (
            "🌅 <b>Morning Brief</b>\n\n"
            f"🕒 {format_ist(ist)}\n\n"
            "Good morning! Bot har 30 minute me latest global news deta rahega.\n"
            "Control ke liye: menu likho."
        )
    else:
        msg = (
            "🌙 <b>Night Brief</b>\n\n"
            f"🕒 {format_ist(ist)}\n\n"
            "Good night! News cycle complete.\n"
            "Kal subah phir se updates shuru hongi."
        )

    try:
        bot.send_message(chat_id=CONTROL_CHAT_ID, text=msg, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Brief send error: {e}")


# =============== ADMIN PANEL (DM) ===============

def admin_menu(chat_id):
    text = (
        "⚙️ <b>Ayush News Bot V2 ULTRA – Control Panel</b>\n\n"
        "Commands (bina / ke simple):\n"
        "- menu → panel\n"
        "- status → bot status\n"
        "- post → turant 5 news\n"
        "- pause / resume → auto stop/start\n"
        "- id → your Telegram ID\n"
    )

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Status", callback_data="status"),
            InlineKeyboardButton("📰 Post now", callback_data="post_now"),
        ],
        [
            InlineKeyboardButton("⏸ / ▶ Auto-post", callback_data="toggle_pause"),
            InlineKeyboardButton("💻 System info", callback_data="sysinfo"),
        ]
    ])

    bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=kb)


def admin_text_handler(chat_id, user_id, text):
    global POSTING_PAUSED

    if not is_admin(user_id):
        bot.send_message(chat_id, "❌ Ye private bot hai sirf owner/admin ke liye.")
        return

    t = text.lower().strip()

    if t in ("menu", "start", "/start", "help"):
        admin_menu(chat_id)
        return

    if t in ("id", "/id"):
        bot.send_message(chat_id, f"🆔 Your ID: <code>{user_id}</code>", parse_mode="HTML")
        return

    if t in ("status",):
        ist = now_ist()
        last = format_ist(datetime.fromtimestamp(last_run_ts)) if last_run_ts else "Not yet"
        paused = "⏸ Paused" if POSTING_PAUSED else "▶ Active"

        msg = (
            "📊 <b>Status</b>\n\n"
            f"State: {paused}\n"
            f"Interval: {NEWS_INTERVAL_MINUTES} min\n"
            f"Total posts: {total_posts}\n"
            f"Last run: {last}\n"
            f"Time now: {format_ist(ist)}\n"
        )

        if last_error_text:
            msg += f"\nLast error:\n<code>{html.escape(last_error_text)}</code>"

        bot.send_message(chat_id, msg, parse_mode="HTML")
        return

    if t in ("post", "post now", "force"):
        bot.send_message(chat_id, "⏳ Sending 5 fresh news…")
        try:
            run_post_cycle()
            bot.send_message(chat_id, "✅ Done.")
        except Exception as e:
            bot.send_message(chat_id, f"❌ Error: {e}")
        return

    if t == "pause":
        POSTING_PAUSED = True
        bot.send_message(chat_id, "⏸ Auto-posting paused.")
        return

    if t == "resume":
        POSTING_PAUSED = False
        bot.send_message(chat_id, "▶ Auto-posting resumed.")
        return

    admin_menu(chat_id)


# =============== CALLBACK HANDLER ===============

def handle_callback(cq):
    global POSTING_PAUSED

    user_id = cq["from"]["id"]
    chat_id = cq["message"]["chat"]["id"]
    data = cq["data"]

    if not is_admin(user_id):
        return

    if data == "status":
        admin_text_handler(chat_id, user_id, "status")

    elif data == "post_now":
        admin_text_handler(chat_id, user_id, "post")

    elif data == "toggle_pause":
        if POSTING_PAUSED:
            admin_text_handler(chat_id, user_id, "resume")
        else:
            admin_text_handler(chat_id, user_id, "pause")

    elif data == "sysinfo":
        msg = (
            "💻 <b>System Info</b>\n\n"
            f"Owner: {OWNER_ID}\n"
            f"Admins: {', '.join(str(i) for i in ADMIN_USER_IDS)}\n"
            f"Channel: {TELEGRAM_CHANNEL_ID}\n"
            f"Interval: {NEWS_INTERVAL_MINUTES} min\n"
            f"Now: {format_ist(now_ist())}"
        )
        bot.send_message(chat_id, msg, parse_mode="HTML")

    bot.answer_callback_query(callback_query_id=cq["id"])


# =============== WEBHOOK UPDATE HANDLER ===============

def handle_update(update):
    if "callback_query" in update:
        return handle_callback(update["callback_query"])

    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return

    chat_id = msg["chat"]["id"]
    user_id = msg["from"]["id"]
    text = msg.get("text", "")

    if msg["chat"]["type"] == "private":
        return admin_text_handler(chat_id, user_id, text)


# =============== FLASK ROUTES ===============

@app.route("/", methods=["GET"])
def home():
    return "Ayush News Bot V2 ULTRA Running!", 200


@app.route("/webhook", methods=["POST"])
def webhook():
    upd = flask_request.get_json()
    if upd:
        handle_update(upd)
    return {"ok": True}


# =============== MAIN LOOP (AUTO POSTING) ===============

def scheduler():
    global last_run_ts

    while True:
        if not POSTING_PAUSED:
            run_post_cycle()
            last_run_ts = time.time()

        time.sleep(NEWS_INTERVAL_MINUTES * 60)


# =============== APP START ===============

def start_bot():
    try:
        bot.send_message(
            chat_id=CONTROL_CHAT_ID,
            text="✅ Bot Updated & Running!\n\nType: menu (for control panel)"
        )
    except:
        pass

    Thread(target=scheduler, daemon=True).start()


start_bot()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
