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
from flask import Flask, request as flask_request
from telegram import (
    Bot,
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.utils.request import Request

# ------------ BASIC LOGGING ------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# ------------ ENVIRONMENT CONFIG ------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

ADMIN_USER_IDS_RAW = os.getenv("ADMIN_USER_IDS", "").strip()

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
    raise RuntimeError(
        "Missing environment variables! TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID set karo."
    )

# Admin IDs set (comma separated integers)
if ADMIN_USER_IDS_RAW:
    ADMIN_USER_IDS = {
        int(x.strip()) for x in ADMIN_USER_IDS_RAW.split(",") if x.strip()
    }
    logging.info(f"Admin lock enabled, admins: {ADMIN_USER_IDS}")
else:
    ADMIN_USER_IDS = set()
    logging.warning(
        "ADMIN_USER_IDS set nahi hai. Sab users ko admin maana jayega (dev mode)."
    )

# ------------ GLOBAL SETTINGS ------------
RSS_LINKS = [
    "https://news.google.com/rss?hl=en-IN&gl=IN&ceid=IN:en",
    "https://feeds.reuters.com/reuters/worldNews",
    "https://feeds.bbci.co.uk/news/world/rss.xml",
]

NEWS_PER_RUN = 5            # 15 minutes me 5 news
AUTO_NEWS_ENABLED = True    # control panel se on/off hoga

sent_ids = set()            # duplicate block ke liye
LAST_POST_TIME = None
POSTS_TODAY = 0
DUPLICATE_SKIPPED = 0

request = Request(con_pool_size=8)
bot = Bot(token=TELEGRAM_BOT_TOKEN, request=request)
app = Flask(__name__)


# ------------ HELPER: TIME + ADMIN CHECK ------------
def now_ist():
    """UTC + 5:30"""
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


def is_admin(user_id: int) -> bool:
    if not ADMIN_USER_IDS:
        # agar list khali hai to sab admin (dev mode)
        return True
    return user_id in ADMIN_USER_IDS


# ------------ URL SHORTENER ------------
def short_url(url: str) -> str:
    try:
        r = requests.get(
            "https://tinyurl.com/api-create.php",
            params={"url": url},
            timeout=10,
        )
        if r.status_code == 200:
            return r.text.strip()
        return url
    except Exception:
        return url


# ------------ AI / SIMPLE SUMMARY ------------
def ai_summarize(title: str, description: str, link: str):
    """
    Agar OPENAI_API_KEY hai -> smart AI summary.
    Agar nahi hai -> simple fallback summary.
    """
    # Fallback simple mode
    if not OPENAI_API_KEY:
        # Simple English summary (title + thoda desc)
        short_desc = (description or "").strip()
        if len(short_desc) > 200:
            short_desc = short_desc[:197] + "..."

        summary_en = f"{title}\n\n{short_desc}".strip()
        voice_hi = ""  # audio system band, to kuch nahi
        hashtags = "#WorldNews #Breaking #Update"
        return summary_en, voice_hi, hashtags

    # --- AI mode ---
    system_prompt = """
Tum ek professional news assistant ho.

Har news ke liye SIRF yeh JSON return karo:

{
  "summary_en": "...",
  "voice_hi": "...",
  "hashtags": "..."
}

Rules:
- summary_en: 2-3 short simple English lines.
- voice_hi: Hindi me 1-2 lines, jaise news anchor bolta hai.
- hashtags: exactly 3-4 tags (one line).
- JSON ke bahar kuch mat likho.
"""

    user_text = f"Title: {title}\n\nDescription: {description}\n\nLink: {link}"

    payload = {
        "model": "gpt-4.1-mini",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        "max_tokens": 200,
        "temperature": 0.7,
    }

    try:
        res = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        data = res.json()
        raw = data["choices"][0]["message"]["content"]
        obj = json.loads(raw)

        return (
            obj.get("summary_en", ""),
            obj.get("voice_hi", ""),
            obj.get("hashtags", "#WorldNews #Breaking #Update"),
        )

    except Exception as e:
        logging.error(f"AI Error: {e}")
        # fallback simple
        short_desc = (description or "").strip()
        if len(short_desc) > 200:
            short_desc = short_desc[:197] + "..."
        summary_en = f"{title}\n\n{short_desc}".strip()
        return summary_en, "", "#WorldNews #Breaking #Update"


# ------------ FETCH LATEST NEWS (WITH IMAGE TRY) ------------
def fetch_news():
    entries = []

    for url in RSS_LINKS:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:8]:
                eid = getattr(e, "id", None) or getattr(e, "link", None)
                if not eid:
                    continue

                summary = getattr(e, "summary", "") or getattr(
                    e, "description", ""
                )

                # try to find image
                img_url = None
                try:
                    if hasattr(e, "media_content"):
                        mc = e.media_content
                        if mc and isinstance(mc, list) and mc[0].get("url"):
                            img_url = mc[0]["url"]
                except Exception:
                    img_url = None

                if not img_url:
                    try:
                        for l in getattr(e, "links", []):
                            if l.get("type", "").startswith("image/"):
                                img_url = l.get("href")
                                break
                    except Exception:
                        img_url = None

                entries.append(
                    {
                        "id": eid,
                        "title": getattr(e, "title", ""),
                        "link": getattr(e, "link", ""),
                        "summary": summary,
                        "image": img_url,
                    }
                )
        except Exception:
            pass

    # latest pehle aaye isliye reverse
    return entries[::-1]


# ------------ PREMIUM FORMAT MESSAGE ------------
def format_message(title, summary_en, link, hashtags):
    safe_title = html.escape(title)
    safe_summary = html.escape(summary_en)
    safe_tags = html.escape(hashtags)

    ist_time = now_ist().strftime("%d %b %Y | %I:%M %p IST")
    short = short_url(link)

    msg = (
        f"🚨 <b>International Breaking News</b>\n"
        f"📅 <i>{ist_time}</i>\n\n"
        f"📰 <b>{safe_title}</b>\n\n"
        f"{safe_summary}\n\n"
        f"🔗 Full Story: <a href=\"{short}\">Read here</a>\n\n"
        f"{safe_tags}\n"
        f"<i>Powered by @Axshchxhan</i>"
    )

    return msg


# ------------ MAIN POSTING JOB ------------
def post_news(force: bool = False, max_items: int | None = None):
    global LAST_POST_TIME, POSTS_TODAY, DUPLICATE_SKIPPED

    if not force and not AUTO_NEWS_ENABLED:
        logging.info("Auto news disabled, skipping scheduled run.")
        return

    logging.info("Checking for new news...")
    entries = fetch_news()

    if max_items is None:
        limit = NEWS_PER_RUN
    else:
        limit = max_items

    count = 0
    today_date = now_ist().date()

    for e in entries:
        if count >= limit:
            break

        if e["id"] in sent_ids:
            DUPLICATE_SKIPPED += 1
            continue

        title = e["title"]
        link = e["link"]
        desc = e["summary"]
        img = e["image"]

        summary_en, voice_hi, hashtags = ai_summarize(title, desc, link)

        if not summary_en:
            sent_ids.add(e["id"])
            continue

        msg_text = format_message(title, summary_en, link, hashtags)

        try:
            if img:
                bot.send_photo(
                    chat_id=TELEGRAM_CHANNEL_ID,
                    photo=img,
                    caption=msg_text,
                    parse_mode="HTML",
                )
            else:
                bot.send_message(
                    chat_id=TELEGRAM_CHANNEL_ID,
                    text=msg_text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )

            sent_ids.add(e["id"])
            count += 1
            LAST_POST_TIME = now_ist()
            if LAST_POST_TIME.date() == today_date:
                POSTS_TODAY += 1

            time.sleep(2)
        except Exception as e:
            logging.error(f"Send error: {e}")
            sent_ids.add(e["id"])
            continue


# ------------ STATUS TEXT ------------
def build_status_text():
    ist_now = now_ist()
    auto_state = "ON ✅" if AUTO_NEWS_ENABLED else "OFF ⏸"

    last_time = LAST_POST_TIME.strftime("%d %b %Y | %I:%M %p IST") if LAST_POST_TIME else "N/A"
    text = (
        "<b>🤖 Ayush News Bot Status</b>\n\n"
        f"⏱ Time (IST): <code>{ist_now.strftime('%d %b %Y | %I:%M:%S %p')}</code>\n"
        f"⚙ Auto News: <b>{auto_state}</b>\n"
        f"📰 Posts Today: <b>{POSTS_TODAY}</b>\n"
        f"🚫 Duplicates Blocked: <b>{DUPLICATE_SKIPPED}</b>\n"
        f"🕒 Last Post: <i>{last_time}</i>\n"
    )
    return text


# ------------ CONTROL PANEL ------------
def send_control_panel(chat_id: int):
    keyboard = [
        [
            InlineKeyboardButton("📊 Status", callback_data="panel_status"),
            InlineKeyboardButton("📰 1 News Now", callback_data="panel_news1"),
        ],
        [
            InlineKeyboardButton("▶️ Auto ON/OFF", callback_data="panel_toggle"),
        ],
        [
            InlineKeyboardButton("🧹 Refresh Cache", callback_data="panel_refresh"),
            InlineKeyboardButton("🧪 Test Message", callback_data="panel_test"),
        ],
    ]
    markup = InlineKeyboardMarkup(keyboard)
    bot.send_message(
        chat_id=chat_id,
        text="🛠 <b>Ayush News Bot Control Panel</b>",
        parse_mode="HTML",
        reply_markup=markup,
    )


# ------------ DEMO MESSAGE (restart / test) ------------
def send_demo_update(chat_id: int | str):
    ist_time = now_ist().strftime("%d %b %Y | %I:%M %p IST")
    text = (
        "🟢 <b>Ayush News Bot Updated</b>\n"
        f"🗓 <i>{ist_time}</i>\n\n"
        "Demo: Bot safaltapurvak chaalu ho chuka hai.\n"
        "Ab har 15 minute me latest international news + image milegi.\n\n"
        "#Update #LiveBot\n"
        "Powered by @Axshchxhan"
    )
    bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


# ------------ TELEGRAM UPDATE HANDLER ------------
def handle_update(update: Update):
    try:
        if update.callback_query:
            handle_callback(update)
            return

        message = update.effective_message
        chat = update.effective_chat
        user = update.effective_user

        if not message or not chat or not user:
            return

        # panel & commands sirf private chat me
        if chat.type != "private":
            return

        text = (message.text or "").strip()

        if text.startswith("/start"):
            bot.send_message(
                chat_id=chat.id,
                text=(
                    "Namaste! 👋\n\n"
                    "Main <b>Ayush Global News Bot</b> hoon.\n"
                    "Har 15 minute me channel par latest international news bhejta hoon.\n\n"
                    "Agar aap admin ho to /panel likh kar control panel open kar sakte hain."
                ),
                parse_mode="HTML",
            )

        elif text.startswith("/panel"):
            if not is_admin(user.id):
                bot.send_message(
                    chat_id=chat.id,
                    text="❌ Ye control sirf admins ke liye hai.",
                )
                return
            send_control_panel(chat.id)

        elif text.startswith("/status"):
            if not is_admin(user.id):
                bot.send_message(
                    chat_id=chat.id,
                    text="❌ Ye command sirf admins ke liye hai.",
                )
                return
            bot.send_message(
                chat_id=chat.id,
                text=build_status_text(),
                parse_mode="HTML",
            )

        elif text.startswith("/news1"):
            if not is_admin(user.id):
                bot.send_message(
                    chat_id=chat.id,
                    text="❌ Ye command sirf admins ke liye hai.",
                )
                return
            bot.send_message(chat_id=chat.id, text="📰 1 news channel par bhej raha hoon...")
            post_news(force=True, max_items=1)

        elif text.startswith("/enable"):
            if not is_admin(user.id):
                bot.send_message(chat_id=chat.id, text="❌ Only admins allowed.")
                return
            global AUTO_NEWS_ENABLED
            AUTO_NEWS_ENABLED = True
            bot.send_message(chat_id=chat.id, text="▶️ Auto news <b>ON</b> ho gaya.", parse_mode="HTML")

        elif text.startswith("/disable"):
            if not is_admin(user.id):
                bot.send_message(chat_id=chat.id, text="❌ Only admins allowed.")
                return
            AUTO_NEWS_ENABLED = False
            bot.send_message(chat_id=chat.id, text="⏸ Auto news <b>OFF</b> ho gaya.", parse_mode="HTML")

    except Exception as e:
        logging.error(f"handle_update error: {e}")


def handle_callback(update: Update):
    query = update.callback_query
    user = query.from_user
    chat_id = query.message.chat.id

    if not is_admin(user.id):
        query.answer("Not allowed ❌", show_alert=True)
        return

    data = query.data or ""

    if data == "panel_status":
        query.answer("Status bhej raha hoon…")
        bot.send_message(
            chat_id=chat_id, text=build_status_text(), parse_mode="HTML"
        )

    elif data == "panel_news1":
        query.answer("1 news channel par…")
        post_news(force=True, max_items=1)

    elif data == "panel_toggle":
        global AUTO_NEWS_ENABLED
        AUTO_NEWS_ENABLED = not AUTO_NEWS_ENABLED
        state = "ON ✅" if AUTO_NEWS_ENABLED else "OFF ⏸"
        query.answer(f"Auto news {state}")
        bot.send_message(chat_id=chat_id, text=f"Auto news ab {state} hai.")

    elif data == "panel_refresh":
        sent_ids.clear()
        query.answer("Cache clear ✅")
        bot.send_message(chat_id=chat_id, text="News cache reset ho gaya.")

    elif data == "panel_test":
        query.answer("Test message ✅")
        send_demo_update(chat_id)

    else:
        query.answer("Unknown action")


# ------------ SCHEDULER LOOP ------------
def scheduler_loop():
    while True:
        schedule.run_pending()
        time.sleep(1)


# ------------ FLASK ROUTES (WEBHOOK + HOME) ------------
@app.route("/", methods=["GET", "POST"])
def webhook():
    if flask_request.method == "POST":
        try:
            update = Update.de_json(flask_request.get_json(force=True), bot)
            handle_update(update)
        except Exception as e:
            logging.error(f"Webhook error: {e}")
        return "ok"
    else:
        return "Ayush Telegram News Bot Running!", 200


# ------------ MAIN ------------
def main():
    logging.info("🔥 Ayush Telegram News Bot Started!")

    # 15 minute schedule
    schedule.every(15).minutes.do(post_news)

    # ek baar demo update channel par
    try:
        send_demo_update(TELEGRAM_CHANNEL_ID)
    except Exception as e:
        logging.error(f"Demo update send error: {e}")

    # scheduler background thread
    t = Thread(target=scheduler_loop, daemon=True)
    t.start()

    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
