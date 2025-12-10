import os
import time
import json
import html
import logging
from datetime import datetime, timedelta
from threading import Thread

import feedparser
import requests
from flask import Flask, request as flask_request
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.utils.request import Request


# ============ ENVIRONMENT CONFIG ============

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")  # e.g. -1001234567890

# Ayush ka ID default:
OWNER_ID = int(os.getenv("OWNER_ID", "7821087304") or "7821087304")

# Extra admins (comma ya space se separate numeric IDs)
ADMIN_USER_IDS_RAW = os.getenv("ADMIN_USER_IDS", "")

# Optional AI keys (OpenAI / DeepSeek)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_API_URL = os.getenv("DEEPSEEK_API_URL", "").strip()  # e.g. https://api.deepseek.com/v1/chat/completions
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "").strip() or "deepseek-chat"

SELF_PING_URL = os.getenv("SELF_PING_URL", "").strip()

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
    raise RuntimeError("TELEGRAM_BOT_TOKEN aur TELEGRAM_CHANNEL_ID zaroor set karo.")


def parse_admin_ids(raw: str):
    ids = set()
    raw = raw.strip()
    if not raw:
        return ids
    import re as _re
    for part in _re.split(r"[,\s]+", raw):
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            pass
    return ids


ADMIN_IDS = parse_admin_ids(ADMIN_USER_IDS_RAW)
ADMIN_IDS.add(OWNER_ID)  # owner hamesha admin

CONTROL_CHAT_ID = OWNER_ID


# ============ GLOBAL SETTINGS ============

RSS_LINKS = [
    "https://news.google.com/rss?hl=en-IN&gl=IN&ceid=IN:en",
    "https://feeds.reuters.com/reuters/worldNews",
    "https://feeds.bbci.co.uk/news/world/rss.xml",
]

NEWS_PER_RUN = 5
NEWS_INTERVAL_MINUTES = 30

sent_ids = set()

POSTING_PAUSED = False
last_news_run_ts = 0
total_posts = 0
last_morning_brief_date = None
last_night_brief_date = None
last_error_text = ""


# ============ TELEGRAM & FLASK ============

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

tg_request = Request(con_pool_size=8)
bot = Bot(token=TELEGRAM_BOT_TOKEN, request=tg_request)

app = Flask(__name__)


# ============ TIME & HELPERS ============

def ist_now():
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


def format_ist(dt: datetime) -> str:
    return dt.strftime("%d %b %Y | %I:%M %p IST")


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def short_url(url: str) -> str:
    if not url:
        return url
    try:
        r = requests.get("https://tinyurl.com/api-create.php", params={"url": url}, timeout=10)
        if r.status_code == 200 and r.text.strip():
            return r.text.strip()
    except Exception:
        pass
    return url


def clean(text: str) -> str:
    if not text:
        return ""
    # remove HTML tags
    import re as _re
    text = html.unescape(text)
    text = _re.sub(r"<[^>]+>", " ", text)
    text = _re.sub(r"\s+", " ", text)
    return text.strip()


# ============ AI SUMMARY (Hindi) ============

def ai_summary_hi(title: str, description: str, link: str):
    """
    Pehle OpenAI try, fir DeepSeek. Agar dono fail -> simple fallback Hindi text.
    Return: (summary_hi, hashtags)
    """
    system_prompt = (
        "Tum ek professional Hindi news editor ho. "
        "Har news ka 2-4 line ka simple, neutral Hindi summary do. "
        "Koi opinion ya extra analysis nahi. Sirf facts."
    )

    user_text = f"Title: {title}\n\nDescription: {description}\n\nLink: {link}"
    default_tags = "#WorldNews #Breaking #Update"

    # --- Try OpenAI ---
    if OPENAI_API_KEY:
        try:
            payload = {
                "model": OPENAI_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text},
                ],
                "max_tokens": 220,
                "temperature": 0.5,
            }
            r = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=25,
            )
            data = r.json()
            summary_hi = data["choices"][0]["message"]["content"].strip()
            return summary_hi, default_tags
        except Exception as e:
            logging.error(f"OpenAI error: {e}")

    # --- Try DeepSeek ---
    if DEEPSEEK_API_KEY and DEEPSEEK_API_URL:
        try:
            payload = {
                "model": DEEPSEEK_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text},
                ],
                "max_tokens": 220,
                "temperature": 0.5,
            }
            r = requests.post(
                DEEPSEEK_API_URL,
                headers={
                    "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=25,
            )
            data = r.json()
            summary_hi = data["choices"][0]["message"]["content"].strip()
            return summary_hi, default_tags
        except Exception as e:
            logging.error(f"DeepSeek error: {e}")

    # --- Fallback (no AI) ---
    base = clean(description) or clean(title) or "नई अंतरराष्ट्रीय खबर उपलब्ध है।"
    if len(base) > 260:
        base = base[:260] + "..."
    summary_hi = (
        f"{base}\n\n"
        "यह अंतरराष्ट्रीय स्रोतों से ली गई एक महत्वपूर्ण खबर है। "
        "पूरी जानकारी के लिए नीचे दिए लिंक पर क्लिक करें।"
    )
    return summary_hi, default_tags


# ============ FETCH NEWS (RSS) ============

def fetch_news():
    items = []
    for url in RSS_LINKS:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:10]:
                nid = getattr(e, "id", None) or getattr(e, "link", None)
                if not nid:
                    continue
                items.append(
                    {
                        "id": nid,
                        "title": getattr(e, "title", ""),
                        "link": getattr(e, "link", ""),
                        "summary": getattr(e, "summary", "")
                        or getattr(e, "description", ""),
                        "entry": e,
                    }
                )
        except Exception as e:
            logging.error(f"RSS error from {url}: {e}")
    # latest last
    return items[::-1]


def extract_image(entry):
    # try media_content
    try:
        mc = getattr(entry, "media_content", None)
        if mc and isinstance(mc, list) and mc and mc[0].get("url"):
            return mc[0]["url"]
    except Exception:
        pass
    # try media_thumbnail
    try:
        mt = getattr(entry, "media_thumbnail", None)
        if mt and isinstance(mt, list) and mt and mt[0].get("url"):
            return mt[0]["url"]
    except Exception:
        pass
    # try links
    try:
        for l in getattr(entry, "links", []):
            if l.get("type", "").startswith("image/"):
                return l.get("href")
    except Exception:
        pass
    return None


# ============ FORMAT MESSAGE ============

def format_news_message(title: str, summary_hi: str, link: str, hashtags: str) -> str:
    safe_title = html.escape(title)
    safe_summary = html.escape(summary_hi)
    safe_tags = html.escape(hashtags)

    ist = ist_now()
    time_str = format_ist(ist)
    short = short_url(link)

    msg = (
        "📰 <b>International Breaking News</b>\n"
        f"📅 <i>{time_str}</i>\n\n"
        f"🗞 <b>{safe_title}</b>\n\n"
        f"{safe_summary}\n\n"
        f"🔗 पूरी खबर: <a href=\"{short}\">यहाँ पढ़ें</a>\n\n"
        f"{safe_tags}\n"
        f"<i>Powered by @Axshchxhan</i>"
    )
    return msg


def get_news_keyboard(link: str):
    short = short_url(link)
    buttons = [
        [InlineKeyboardButton("🌐 Full Story", url=short)],
        [InlineKeyboardButton("📣 Join Updates Channel", url="https://t.me/chxuhan")],
    ]
    return InlineKeyboardMarkup(buttons)


# ============ POST NEWS RUN ============

def post_news():
    global last_news_run_ts, total_posts, last_error_text

    logging.info("Checking for new news...")
    if POSTING_PAUSED:
        logging.info("Posting paused, skipping.")
        return

    entries = fetch_news()
    count = 0

    for item in entries:
        if count >= NEWS_PER_RUN:
            break

        nid = item["id"]
        if nid in sent_ids:
            continue

        title = item["title"] or "Breaking News"
        link = item["link"]
        desc = item["summary"]
        entry = item["entry"]

        try:
            summary_hi, tags = ai_summary_hi(title, desc, link)
            msg = format_news_message(title, summary_hi, link, tags)
            kb = get_news_keyboard(link)
            img = extract_image(entry)

            if img:
                bot.send_photo(
                    chat_id=TELEGRAM_CHANNEL_ID,
                    photo=img,
                    caption=msg,
                    parse_mode="HTML",
                    reply_markup=kb,
                )
            else:
                bot.send_message(
                    chat_id=TELEGRAM_CHANNEL_ID,
                    text=msg,
                    parse_mode="HTML",
                    reply_markup=kb,
                    disable_web_page_preview=False,
                )

            sent_ids.add(nid)
            total_posts += 1
            count += 1
            time.sleep(2)

        except Exception as e:
            last_error_text = f"{type(e).__name__}: {e}"
            logging.error(f"post_news error: {e}")

    last_news_run_ts = time.time()
    logging.info(f"post_news finished. Sent {count} items.")


# ============ DAILY BRIEFS ============

def send_brief(kind: str):
    if kind not in ("morning", "night"):
        return

    ist = ist_now()
    date_str = ist.strftime("%d %b %Y")

    if kind == "morning":
        title = "🌅 Morning Global Brief"
        body = (
            "Good morning! Aaj ka din shuru ho chuka hai.\n"
            "Har 30 minute me latest international updates Hindi summary ke saath milengi."
        )
    else:
        title = "🌙 Night Global Brief"
        body = (
            "Good night! Aaj ke din ki important international sukhiyan post ho chuki hain.\n"
            "Kal phir se har 30 minute me updates aayengi."
        )

    text = (
        f"{title}\n"
        f"📅 <i>{date_str}</i>\n\n"
        f"{body}\n\n"
        "<i>Powered by @Axshchxhan</i>"
    )

    try:
        bot.send_message(
            chat_id=TELEGRAM_CHANNEL_ID,
            text=text,
            parse_mode="HTML",
        )
    except Exception as e:
        logging.error(f"Brief ({kind}) send error: {e}")


# ============ ADMIN PANEL (OWNER DM) ============

def admin_menu_text():
    paused = "⏸ Paused" if POSTING_PAUSED else "▶ Active"
    return (
        "⚙ <b>Ayush News Bot V2 ULTRA – Control Panel</b>\n\n"
        "Commands (bina / ke type karo):\n"
        "- <code>menu</code> – ye panel\n"
        "- <code>status</code> – bot status\n"
        "- <code>post</code> – turant ek news run\n"
        "- <code>pause</code> – auto posting rok do\n"
        "- <code>resume</code> – auto posting chalu karo\n"
        "- <code>id</code> – tumhara Telegram ID\n\n"
        f"Current state: {paused}"
    )


def handle_admin_text(chat_id: int, user_id: int, text: str):
    global POSTING_PAUSED

    t = (text or "").strip().lower()

    if t in ("menu", "help", "start", "/start"):
        bot.send_message(chat_id, admin_menu_text(), parse_mode="HTML")
        return

    if t in ("id", "/id"):
        bot.send_message(
            chat_id,
            f"🆔 Your Telegram ID: <code>{user_id}</code>",
            parse_mode="HTML",
        )
        return

    if t == "status":
        ist = ist_now()
        last = (
            format_ist(datetime.fromtimestamp(last_news_run_ts) + timedelta(hours=5, minutes=30))
            if last_news_run_ts
            else "Not yet"
        )
        paused = "⏸ Paused" if POSTING_PAUSED else "▶ Active"

        msg = (
            "📊 <b>Bot Status</b>\n\n"
            f"State: {paused}\n"
            f"Interval: {NEWS_INTERVAL_MINUTES} min\n"
            f"Total posts: {total_posts}\n"
            f"Last run: {last}\n"
            f"Now IST: {format_ist(ist)}\n"
        )
        if last_error_text:
            msg += f"\nLast error:\n<code>{html.escape(last_error_text)}</code>"

        bot.send_message(chat_id, msg, parse_mode="HTML")
        return

    if t in ("post", "post now", "force"):
        bot.send_message(chat_id, "⏳ Running one news cycle…")
        try:
            post_news()
            bot.send_message(chat_id, "✅ News cycle complete.")
        except Exception as e:
            bot.send_message(chat_id, f"❌ Error: {e}")
        return

    if t == "pause":
        POSTING_PAUSED = True
        bot.send_message(chat_id, "⏸ Auto posting paused.")
        return

    if t == "resume":
        POSTING_PAUSED = False
        bot.send_message(chat_id, "▶ Auto posting resumed.")
        return

    # default: show menu
    bot.send_message(chat_id, "Command samajh nahi aaya, yeh options hain:", parse_mode="HTML")
    bot.send_message(chat_id, admin_menu_text(), parse_mode="HTML")


def handle_update(update: dict):
    message = update.get("message") or update.get("edited_message")
    if not message:
        return

    chat = message.get("chat", {})
    chat_id = chat.get("id")
    chat_type = chat.get("type")
    from_user = message.get("from", {})
    user_id = from_user.get("id")
    text = message.get("text", "")

    if not chat_id or not user_id:
        return

    # Sirf private chat me owner/admin ke liye control panel
    if chat_type == "private" and is_admin(int(user_id)):
        handle_admin_text(int(chat_id), int(user_id), text)


# ============ FLASK ROUTES ============

@app.route("/", methods=["GET", "POST"])
def index():
    if flask_request.method == "POST":
        try:
            update = flask_request.get_json(force=True)
            handle_update(update)
        except Exception as e:
            logging.error(f"Webhook error: {e}")
        return "OK", 200

    return "Ayush News Bot V2 ULTRA Running!", 200


# ============ SCHEDULER LOOP ============

def scheduler_loop():
    global last_news_run_ts, last_morning_brief_date, last_night_brief_date

    while True:
        now_ts = time.time()
        now_ist = ist_now()

        # auto news
        if now_ts - last_news_run_ts >= NEWS_INTERVAL_MINUTES * 60:
            try:
                post_news()
            except Exception as e:
                logging.error(f"post_news error (scheduler): {e}")

        # morning brief at 09:00 IST
        if now_ist.hour == 9:
            if last_morning_brief_date != now_ist.date():
                try:
                    send_brief("morning")
                except Exception as e:
                    logging.error(f"Morning brief error: {e}")
                last_morning_brief_date = now_ist.date()

        # night brief at 22:00 IST
        if now_ist.hour == 22:
            if last_night_brief_date != now_ist.date():
                try:
                    send_brief("night")
                except Exception as e:
                    logging.error(f"Night brief error: {e}")
                last_night_brief_date = now_ist.date()

        # self-ping
        if SELF_PING_URL:
            try:
                requests.get(SELF_PING_URL, timeout=5)
            except Exception:
                pass

        time.sleep(10)


# ============ MAIN ============

def main():
    logging.info("🔥 Ayush News Bot V2 ULTRA Started!")

    # startup DM
    try:
        ist = ist_now()
        msg = (
            "🟢 <b>Ayush News Bot V2 ULTRA Online</b>\n"
            f"🗓 <i>{format_ist(ist)}</i>\n\n"
            f"Ab se har {NEWS_INTERVAL_MINUTES} minute me "
            "international news Hindi summary ke saath channel par aayegi.\n\n"
            "Control ke liye DM me 'menu' likho.\n\n"
            "#Update #LiveBot\n"
            "Powered by @Axshchxhan"
        )
        bot.send_message(chat_id=CONTROL_CHAT_ID, text=msg, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Startup DM error: {e}")

    # scheduler
    t = Thread(target=scheduler_loop, daemon=True)
    t.start()

    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
