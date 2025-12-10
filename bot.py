import os
import time
import json
import html
import logging
import tempfile
from datetime import datetime, timedelta
from threading import Thread

import feedparser
import schedule
import requests
from flask import Flask, request as flask_request
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.utils.request import Request

# ------------- ENVIRONMENT CONFIG -------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")  # like "-1001234567890"
OWNER_ID = int(os.getenv("OWNER_ID", "0"))  # your user id
ADMIN_USER_IDS = os.getenv("ADMIN_USER_IDS", "")  # "123,456"
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
SELF_PING_URL = os.getenv("SELF_PING_URL", "")

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
    raise RuntimeError("Missing env vars: TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID")

ADMIN_IDS = set()
if OWNER_ID:
    ADMIN_IDS.add(OWNER_ID)
if ADMIN_USER_IDS.strip():
    for x in ADMIN_USER_IDS.replace(" ", "").split(","):
        if x.isdigit():
            ADMIN_IDS.add(int(x))

# ====== GLOBAL SETTINGS ======
NEWS_INTERVAL_MINUTES = 30   # har 30 minute me cycle
NEWS_PER_RUN = 5             # har cycle max 5 news

RSS_LINKS = [
    "https://news.google.com/rss?hl=en-IN&gl=IN&ceid=IN:en",
    "https://feeds.reuters.com/reuters/worldNews",
    "https://feeds.bbci.co.uk/news/world/rss.xml",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

request = Request(con_pool_size=8)
bot = Bot(token=TELEGRAM_BOT_TOKEN, request=request)

app = Flask(__name__)

sent_ids = set()
POSTING_PAUSED = False
last_news_run_ts = 0
last_morning_brief_date = None
last_night_brief_date = None


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def ist_now():
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


def short_url(url: str) -> str:
    try:
        r = requests.get(
            "https://tinyurl.com/api-create.php",
            params={"url": url},
            timeout=8,
        )
        if r.status_code == 200:
            return r.text.strip()
    except Exception:
        pass
    return url


def ai_summarize_hi(title: str, description: str, link: str) -> str:
    """
    Short Hindi summary. Pehle DeepSeek / OpenAI use karega,
    agar key na ho toh simple fallback.
    """
    base_text = f"{title}\n\n{description}\n\nLink: {link}"

    # ---- DeepSeek ----
    if DEEPSEEK_API_KEY:
        try:
            payload = {
                "model": DEEPSEEK_MODEL,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Tum ek professional Hindi news editor ho. "
                            "Har baar 3-4 chhoti simple lines me Hindi me summary do. "
                            "Koi role ya explanation mat do."
                        ),
                    },
                    {"role": "user", "content": base_text},
                ],
                "max_tokens": 220,
                "temperature": 0.7,
            }
            r = requests.post(
                f"{DEEPSEEK_BASE_URL}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=25,
            )
            data = r.json()
            return data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            logging.error(f"DeepSeek error: {e}")

    # ---- OpenAI (optional backup) ----
    if OPENAI_API_KEY:
        try:
            payload = {
                "model": "gpt-4.1-mini",
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Tum ek professional Hindi news editor ho. "
                            "Har baar 3-4 chhoti simple lines me Hindi me summary do. "
                            "Koi role ya explanation mat do."
                        ),
                    },
                    {"role": "user", "content": base_text},
                ],
                "max_tokens": 220,
                "temperature": 0.7,
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
            return data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            logging.error(f"OpenAI error: {e}")

    # ---- simple fallback (no AI) ----
    if description:
        return f"{title}\n\n{description[:260]}..."
    return title


def fetch_image(url: str) -> str | None:
    """
    Page se og:image nikalne ki koshish karega.
    Sirf image URL return karega.
    """
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return None
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(r.text, "html.parser")
        og = soup.find("meta", property="og:image") or soup.find(
            "meta", attrs={"name": "og:image"}
        )
        if og and og.get("content"):
            return og["content"]
    except Exception:
        return None
    return None


def fetch_news():
    entries = []
    for url in RSS_LINKS:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:10]:
                eid = getattr(e, "id", None) or getattr(e, "link", None)
                if not eid or eid in sent_ids:
                    continue
                entries.append(
                    {
                        "id": eid,
                        "title": getattr(e, "title", ""),
                        "link": getattr(e, "link", ""),
                        "summary": getattr(e, "summary", "")
                        or getattr(e, "description", ""),
                    }
                )
        except Exception as ex:
            logging.error(f"RSS error: {ex}")

    # newest last -> first
    return entries[::-1]


def format_news_message(title: str, summary_hi: str, link: str) -> str:
    safe_title = html.escape(title)
    safe_summary = html.escape(summary_hi)
    short = short_url(link)
    ist = ist_now()
    time_str = ist.strftime("%d %b %Y | %I:%M %p IST")

    msg = (
        f"🧭 <b>GlobalUpdates • Breaking News</b>\n"
        f"🕒 <i>{time_str}</i>\n\n"
        f"📰 <b>{safe_title}</b>\n\n"
        f"{safe_summary}\n\n"
        f"🔗 <a href=\"{short}\">Full Story</a>\n\n"
        f"#WorldNews #Breaking #Update\n"
        f"<i>Powered by @Axshchxhan</i>"
    )
    return msg


def post_single_news(item: dict):
    title = item["title"]
    link = item["link"]
    desc = item["summary"]

    summary_hi = ai_summarize_hi(title, desc, link)
    msg_text = format_news_message(title, summary_hi, link)

    image_url = fetch_image(link)

    if image_url:
        try:
            bot.send_photo(
                chat_id=TELEGRAM_CHANNEL_ID,
                photo=image_url,
                caption=msg_text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return
        except Exception as e:
            logging.error(f"send_photo error: {e}")

    bot.send_message(
        chat_id=TELEGRAM_CHANNEL_ID,
        text=msg_text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


def post_news():
    global last_news_run_ts
    if POSTING_PAUSED:
        logging.info("Posting paused, skipping news cycle.")
        return

    logging.info("Checking for new news...")
    entries = fetch_news()
    count = 0
    for e in entries:
        if count >= NEWS_PER_RUN:
            break
        if e["id"] in sent_ids:
            continue
        try:
            post_single_news(e)
            sent_ids.add(e["id"])
            count += 1
            time.sleep(2)
        except Exception as ex:
            logging.error(f"post_single_news error: {ex}")

    last_news_run_ts = time.time()
    logging.info(f"Posted {count} items this run.")


def build_admin_keyboard():
    btns = [
        [InlineKeyboardButton("🔄 Status", callback_data="adm_status")],
        [
            InlineKeyboardButton("⏸ Pause", callback_data="adm_pause"),
            InlineKeyboardButton("▶ Resume", callback_data="adm_resume"),
        ],
        [InlineKeyboardButton("⏱ Interval", callback_data="adm_interval")],
        [InlineKeyboardButton("❓ Help", callback_data="adm_help")],
    ]
    return InlineKeyboardMarkup(btns)
    def handle_admin_command_text(chat_id: int, user_id: int, text: str):
    """
    Plain text admin control (slash ki zarurat nahi).
    Example: 'status', 'pause', 'resume', 'interval 30'
    """
    t = text.strip().lower()

    if t in ("status", "bot status", "info"):
        status = "⏸ Paused" if POSTING_PAUSED else "▶ Active"
        ist = ist_now().strftime("%d %b %Y | %I:%M %p IST")
        msg = (
            f"🧭 <b>GlobalUpdates • Control Panel</b>\n"
            f"Status: {status}\n"
            f"Interval: {NEWS_INTERVAL_MINUTES} min\n"
            f"Last run: {time.ctime(last_news_run_ts) if last_news_run_ts else 'Not yet'}\n"
            f"Time (IST): {ist}"
        )
        bot.send_message(
            chat_id=chat_id,
            text=msg,
            parse_mode="HTML",
            reply_markup=build_admin_keyboard(),
        )
        return

    if t in ("pause", "stop"):
        global POSTING_PAUSED
        POSTING_PAUSED = True
        bot.send_message(
            chat_id=chat_id,
            text="⏸ News posting ab <b>PAUSE</b> ho gaya.",
            parse_mode="HTML",
            reply_markup=build_admin_keyboard(),
        )
        return

    if t in ("resume", "start"):
        global POSTING_PAUSED
        POSTING_PAUSED = False
        bot.send_message(
            chat_id=chat_id,
            text="▶ News posting <b>RESUME</b> ho gaya.",
            parse_mode="HTML",
            reply_markup=build_admin_keyboard(),
        )
        return

    if t.startswith("interval"):
        parts = t.split()
        if len(parts) >= 2 and parts[1].isdigit():
            global NEWS_INTERVAL_MINUTES
            NEWS_INTERVAL_MINUTES = max(5, int(parts[1]))
            bot.send_message(
                chat_id=chat_id,
                text=f"⏱ Interval ab <b>{NEWS_INTERVAL_MINUTES} minutes</b> ho gaya.",
                parse_mode="HTML",
                reply_markup=build_admin_keyboard(),
            )
        else:
            bot.send_message(
                chat_id=chat_id,
                text="Use: <code>interval 30</code>",
                parse_mode="HTML",
                reply_markup=build_admin_keyboard(),
            )
        return

    # default help
    help_text = (
        "🧭 <b>GlobalUpdates • Admin Commands</b>\n\n"
        "Bas normal text me likho (slash ki jarurat nahi):\n"
        "- <b>status</b> → bot ka status\n"
        "- <b>pause</b> → news band\n"
        "- <b>resume</b> → news chalu\n"
        "- <b>interval 30</b> → har 30 min me news\n"
    )
    bot.send_message(
        chat_id=chat_id,
        text=help_text,
        parse_mode="HTML",
        reply_markup=build_admin_keyboard(),
    )


def handle_update(update: dict):
    """
    Webhook se aane wale saare updates yahan handle honge.
    Sirf admin / owner ke DM ke liye.
    """
    if "message" in update:
        msg = update["message"]
        chat_id = msg["chat"]["id"]
        user_id = msg["from"]["id"]
        text = msg.get("text", "")

        if not is_admin(int(user_id)):
            # non-admin ko simple reply
            try:
                bot.send_message(
                    chat_id=chat_id,
                    text="Ye private control bot hai. Sirf owner ke liye available hai.",
                )
            except Exception:
                pass
            return

        handle_admin_command_text(
            chat_id=int(chat_id),
            user_id=int(user_id),
            text=text,
        )

    elif "callback_query" in update:
        cq = update["callback_query"]
        data = cq.get("data", "")
        chat_id = cq["message"]["chat"]["id"]
        user_id = cq["from"]["id"]

        if not is_admin(int(user_id)):
            return

        if data == "adm_status":
            handle_admin_command_text(chat_id, user_id, "status")
        elif data == "adm_pause":
            handle_admin_command_text(chat_id, user_id, "pause")
        elif data == "adm_resume":
            handle_admin_command_text(chat_id, user_id, "resume")
        elif data == "adm_interval":
            handle_admin_command_text(
                chat_id, user_id, f"interval {NEWS_INTERVAL_MINUTES}"
            )
        elif data == "adm_help":
            handle_admin_command_text(chat_id, user_id, "help")


# ========== FLASK ROUTES (WEBHOOK + HEALTH) ==========
@app.route("/", methods=["GET", "POST"])
def index():
    if flask_request.method == "POST":
        try:
            update = flask_request.get_json(force=True)
            handle_update(update)
        except Exception as e:
            logging.error(f"Webhook error: {e}")
        return "OK", 200

    # GET request => health check
    return "Ayush Telegram News Bot V2 ULTRA Running!", 200


# ========== SCHEDULER LOOP ==========
def scheduler_loop():
    global last_news_run_ts, last_morning_brief_date, last_night_brief_date

    while True:
        now_ist = ist_now()
        now_ts = time.time()

        # periodic news
        if not POSTING_PAUSED:
            if now_ts - last_news_run_ts >= NEWS_INTERVAL_MINUTES * 60:
                try:
                    post_news()
                except Exception as e:
                    logging.error(f"post_news error: {e}")
                last_news_run_ts = now_ts

        # self ping for Render
        if SELF_PING_URL:
            try:
                requests.get(SELF_PING_URL, timeout=10)
            except Exception:
                pass

        time.sleep(10)


# ========== MAIN ==========
def main():
    logging.info("🔥 Ayush News Bot V2 ULTRA Started!")

    # Startup DM demo -> sirf OWNER ko (channel ko nahi)
    if OWNER_ID:
        try:
            ist = ist_now().strftime("%d %b %Y | %I:%M %p IST")
            text = (
                f"🟢 <b>Ayush News Bot Updated</b>\n"
                f"🗓 <i>{ist}</i>\n\n"
                "Demo: Bot safaltapurvak chaalu ho chuka hai.\n"
                f"Ab har {NEWS_INTERVAL_MINUTES} minute me latest "
                "international news + image milegi.\n\n"
                "#Update #LiveBot\n"
                "Powered by @Axshchxhan"
            )
            bot.send_message(chat_id=OWNER_ID, text=text, parse_mode="HTML")
        except Exception as e:
            logging.error(f"Startup DM error: {e}")

    # scheduler background thread
    t = Thread(target=scheduler_loop, daemon=True)
    t.start()

    # Flask webserver for webhook + health
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
