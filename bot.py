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
from telegram import Bot, ParseMode, ReplyKeyboardMarkup
from telegram.utils.request import Request as TgRequest


# ========== CONFIG FROM ENV ==========

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")  # "-100...."
ADMIN_USER_IDS_RAW = os.getenv("ADMIN_USER_IDS", "")
OWNER_ID = os.getenv("OWNER_ID", "").strip() or None

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_BASE = os.getenv("DEEPSEEK_BASE", "https://api.deepseek.com")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

SELF_PING_URL = os.getenv("SELF_PING_URL", "").strip()

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
    raise RuntimeError(
        "Missing environment variables! TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID set karo."
    )

ADMIN_USER_IDS = set()
for part in ADMIN_USER_IDS_RAW.split(","):
    part = part.strip()
    if part.isdigit():
        ADMIN_USER_IDS.add(int(part))

if OWNER_ID and OWNER_ID.isdigit():
    ADMIN_USER_IDS.add(int(OWNER_ID))

CONTROL_CHAT_ID = int(OWNER_ID) if OWNER_ID and OWNER_ID.isdigit() else None


# ========== GLOBAL SETTINGS ==========

RSS_LINKS = [
    "https://news.google.com/rss?hl=en-IN&gl=IN&ceid=IN:en",
    "https://feeds.reuters.com/reuters/worldNews",
    "https://feeds.bbci.co.uk/news/world/rss.xml",
]

NEWS_PER_RUN = 5
NEWS_INTERVAL_MINUTES = 30  # tumhari demand ke hisaab se
POSTING_PAUSED = False

sent_ids = set()          # duplicate filter
recent_news = []          # last 100 news for briefs

last_news_run_ts = 0
last_morning_brief_date = None
last_night_brief_date = None

campaign_enabled = False
campaign_text = ""  # ex: "Crypto ke liye @XYZ join karein"

ai_error_streak = 0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

tg_request = TgRequest(con_pool_size=8)
bot = Bot(token=TELEGRAM_BOT_TOKEN, request=tg_request)

app = Flask(__name__)


# ========== SMALL UTILS ==========

def ist_now():
    """Current time in IST."""
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


def short_url(url: str) -> str:
    """TinyURL shortener, safe fallback to original."""
    try:
        r = requests.get(
            "https://tinyurl.com/api-create.php",
            params={"url": url},
            timeout=10
        )
        if r.status_code == 200:
            return r.text.strip()
    except Exception:
        pass
    return url


def add_recent_news(entry):
    """Store limited history for briefs."""
    global recent_news
    recent_news.append(entry)
    if len(recent_news) > 100:
        recent_news = recent_news[-100:]


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_USER_IDS


# ========== AI SUMMARY (HINDI FIRST) ==========

def build_ai_prompt(title, description, link, mode="normal"):
    base = f"Title: {title}\n\nDescription: {description}\n\nLink: {link}\n"

    if mode == "normal":
        extra = """
Tum ek professional Hindi news anchor ho.

Sirf yeh JSON return karo (JSON ke bahar kuch mat likho):

{
  "summary_hi": "2-3 chhoti, seedhi Hindi lines jo news samjhayen.",
  "hashtags": "#WorldNews #Breaking #Update"
}

Rules:
- summary_hi bilkul natural Hindi ho, TV news anchor jaise.
- Koi emoji nahi summary me.
- hashtags me 3-4 relevant tags, ek hi line me (space se separated).
"""
    elif mode == "morning":
        extra = """
Tum Hindi news anchor ho.

Subah ke liye ek chhota sa bulletin banaao.
Sirf yeh JSON return karo:

{
  "summary_hi": "4-5 short Hindi lines - raat bhar ki sabse badi world headlines.",
  "hashtags": "#MorningBrief #WorldNews"
}
"""
    else:  # night
        extra = """
Tum Hindi news anchor ho.

Raat ke liye ek chhota sa bulletin banaao.
Sirf yeh JSON return karo:

{
  "summary_hi": "4-5 short Hindi lines - poore din ki sabse important world headlines.",
  "hashtags": "#NightBrief #WorldNews"
}
"""
    return base + extra


def ai_call_deepseek(prompt):
    global ai_error_streak

    if not DEEPSEEK_API_KEY:
        return None

    try:
        payload = {
            "model": DEEPSEEK_MODEL,
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.4,
            "max_tokens": 300,
        }
        r = requests.post(
            f"{DEEPSEEK_BASE}/chat/completions",
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=25,
        )
        data = r.json()
        content = data["choices"][0]["message"]["content"]
        obj = json.loads(content)
        ai_error_streak = 0
        return obj
    except Exception as e:
        logging.error(f"Deepseek error: {e}")
        ai_error_streak += 1
        return None


def ai_call_openai(prompt):
    global ai_error_streak

    if not OPENAI_API_KEY:
        return None

    try:
        payload = {
            "model": "gpt-4.1-mini",
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.4,
            "max_tokens": 300,
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
        content = data["choices"][0]["message"]["content"]
        obj = json.loads(content)
        ai_error_streak = 0
        return obj
    except Exception as e:
        logging.error(f"OpenAI error: {e}")
        ai_error_streak += 1
        return None


def ai_summarize(title, description, link, mode="normal"):
    """
    Returns: (summary_hi, hashtags)
    hamesha kuch na kuch return karega (fallback bhi hai).
    """
    global ai_error_streak

    text = description or title or ""
    text = text[:700]

    prompt = build_ai_prompt(title, text, link, mode=mode)

    obj = ai_call_deepseek(prompt)
    if obj is None:
        obj = ai_call_openai(prompt)

    if obj is not None:
        summary_hi = obj.get("summary_hi", "").strip()
        hashtags = obj.get("hashtags", "#WorldNews #Update").strip()
        if summary_hi:
            return summary_hi, hashtags

    # ---- Fallback: simple Hindi summary (no AI needed) ----
    ai_error_streak += 1
    summary_hi = f"{title}\n\n{text[:200]}..."
    hashtags = "#WorldNews #Update"
    return summary_hi, hashtags


# ========== FETCH LATEST NEWS ==========

def fetch_news():
    entries = []
    for url in RSS_LINKS:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:6]:
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
        except Exception as e:
            logging.error(f"RSS error from {url}: {e}")
    # latest last
    return entries[::-1]


def extract_image(entry):
    """RSS se image nikaalne ki attempt."""
    try:
        if hasattr(entry, "media_content") and entry.media_content:
            return entry.media_content[0].get("url")
        if hasattr(entry, "media_thumbnail") and entry.media_thumbnail:
            return entry.media_thumbnail[0].get("url")
    except Exception:
        pass
    return None


# ========== MESSAGE FORMAT ==========

def format_message(title, summary_hi, link, hashtags):
    safe_title = html.escape(title)
    safe_summary = html.escape(summary_hi)
    safe_tags = html.escape(hashtags)

    ist = ist_now()
    time_str = ist.strftime("%d %b %Y | %I:%M %p IST")

    short = short_url(link)

    parts = [
        f"📰 <b>{safe_title}</b>",
        f"🕒 <i>{time_str}</i>",
        "",
        safe_summary,
        "",
        f"🔗 <b>Full Story:</b> <a href=\"{short}\">Read here</a>",
    ]

    if campaign_enabled and campaign_text:
        parts.append("")
        parts.append(html.escape(campaign_text))

    parts.append("")
    parts.append(safe_tags)
    parts.append("Powered by @Axshchxhan")

    return "\n".join(parts)


# ========== MAIN NEWS POSTING ==========

def post_news():
    global sent_ids

    if POSTING_PAUSED:
        logging.info("Posting paused, skipping this run.")
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
        desc = e["summary"]

        summary_hi, hashtags = ai_summarize(title, desc, link, mode="normal")
        msg_text = format_message(title, summary_hi, link, hashtags)

        try:
            if e["image"]:
                bot.send_photo(
                    chat_id=TELEGRAM_CHANNEL_ID,
                    photo=e["image"],
                    caption=msg_text,
                    parse_mode=ParseMode.HTML
                )
            else:
                bot.send_message(
                    chat_id=TELEGRAM_CHANNEL_ID,
                    text=msg_text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            logging.info(f"Sent news: {title}")
        except Exception as ex:
            logging.error(f"Send error: {ex}")
            # even if send fail, mark as sent to avoid spam
        sent_ids.add(e["id"])
        add_recent_news(e)
        count += 1
        time.sleep(2)


# ========== MORNING / NIGHT BRIEFS ==========

def make_brief(mode: str):
    """mode = 'morning' or 'night'."""
    if not recent_news:
        return None, None

    # last 15 items maximum
    selected = recent_news[-15:]
    titles = [n["title"] for n in selected if n.get("title")]
    joined = "\n".join(f"- {t}" for t in titles[:15])

    dummy_link = "https://news.google.com/"

    summary_hi, hashtags = ai_summarize(
        title="Brief",
        description=joined,
        link=dummy_link,
        mode=mode,
    )

    ist = ist_now()
    title = "🌅 Morning Brief" if mode == "morning" else "🌙 Night Brief"

    safe_summary = html.escape(summary_hi)
    safe_tags = html.escape(hashtags)
    time_str = ist.strftime("%d %b %Y | %I:%M %p IST")

    parts = [
        f"{title}",
        f"🕒 <i>{time_str}</i>",
        "",
        safe_summary,
        "",
        safe_tags,
        "Powered by @Axshchxhan",
    ]
    return "\n".join(parts), None


def send_brief(mode: str):
    msg, _ = make_brief(mode)
    if not msg:
        return
    try:
        bot.send_message(
            chat_id=TELEGRAM_CHANNEL_ID,
            text=msg,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        logging.info(f"Sent {mode} brief.")
    except Exception as e:
        logging.error(f"Brief send error: {e}")


# ========== ADMIN PANEL (DM ONLY) ==========

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["📊 Status", "⏱ Interval"],
        ["▶ Resume", "⏸ Pause"],
        ["📰 Test news", "📣 Campaign"],
        ["⚙ Sources", "❌ Close keyboard"],
    ],
    resize_keyboard=True,
    one_time_keyboard=False
)


def send_admin(msg: str):
    if CONTROL_CHAT_ID:
        try:
            bot.send_message(chat_id=CONTROL_CHAT_ID, text=msg)
        except Exception as e:
            logging.error(f"Failed to send admin DM: {e}")


def handle_admin_text(chat_id: int, user_id: int, text: str):
    global POSTING_PAUSED, NEWS_INTERVAL_MINUTES, campaign_enabled, campaign_text, ai_error_streak

    t = text.strip().lower()

    if t in ("menu", "/menu", "help", "/start"):
        bot.send_message(
            chat_id=chat_id,
            text=(
                "Ayush News Bot Admin Panel\n\n"
                "Commands (without /):\n"
                "- status\n- pause / resume\n"
                "- interval 15 / interval 30\n"
                "- test news\n"
                "- campaign on <text>\n- campaign off\n"
                "- sources (sirf info ke liye, abhi manual list)\n"
            ),
            reply_markup=MAIN_KEYBOARD
        )
        return

    if "status" in t or t.startswith("📊"):
        ist = ist_now()
        txt = (
            f"🟢 Bot status:\n"
            f"- Time IST: {ist.strftime('%d %b %Y | %I:%M %p')}\n"
            f"- Interval: {NEWS_INTERVAL_MINUTES} min\n"
            f"- Paused: {'Yes' if POSTING_PAUSED else 'No'}\n"
            f"- Sent IDs: {len(sent_ids)}\n"
            f"- Recent news stored: {len(recent_news)}\n"
            f"- AI error streak: {ai_error_streak}\n"
        )
        bot.send_message(chat_id=chat_id, text=txt)
        return

    if t.startswith("pause") or t.startswith("⏸"):
        POSTING_PAUSED = True
        bot.send_message(chat_id=chat_id, text="⏸ Posting paused.")
        return

    if t.startswith("resume") or t.startswith("▶"):
        POSTING_PAUSED = False
        bot.send_message(chat_id=chat_id, text="▶ Posting resumed.")
        return

    if t.startswith("interval"):
        # example: "interval 15"
        parts = t.split()
        if len(parts) == 2 and parts[1].isdigit():
            minutes = int(parts[1])
            if 5 <= minutes <= 180:
                NEWS_INTERVAL_MINUTES = minutes
                bot.send_message(
                    chat_id=chat_id,
                    text=f"⏱ Interval set to {minutes} minutes."
                )
            else:
                bot.send_message(
                    chat_id=chat_id,
                    text="Interval 5 se 180 minute ke beech hona chahiye."
                )
        else:
            bot.send_message(chat_id=chat_id, text="Use: interval 15  (ya 30, 45, ...)")
        return

    if t.startswith("⏱"):
        bot.send_message(chat_id=chat_id, text="Example:  interval 30")
        return

    if t.startswith("test news") or t.startswith("📰"):
        entries = fetch_news()
        if not entries:
            bot.send_message(chat_id=chat_id, text="Koi news nahi mili abhi.")
            return
        e = entries[-1]
        summary_hi, hashtags = ai_summarize(e["title"], e["summary"], e["link"])
        msg_text = format_message(e["title"], summary_hi, e["link"], hashtags)
        try:
            if e["image"]:
                bot.send_photo(
                    chat_id=chat_id,
                    photo=e["image"],
                    caption=msg_text,
                    parse_mode=ParseMode.HTML
                )
            else:
                bot.send_message(
                    chat_id=chat_id,
                    text=msg_text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
        except Exception as ex:
            logging.error(f"Admin test send error: {ex}")
        return

    if t.startswith("campaign on"):
        rest = text[len("campaign on"):].strip()
        if not rest:
            bot.send_message(
                chat_id=chat_id,
                text="Usage: campaign on  Text jo har post ke niche jaye."
            )
            return
        campaign_enabled = True
        campaign_text = rest
        bot.send_message(
            chat_id=chat_id,
            text=f"📣 Campaign ON:\n{campaign_text}"
        )
        return

    if t.startswith("campaign off") or t.startswith("📣"):
        campaign_enabled = False
        campaign_text = ""
        bot.send_message(chat_id=chat_id, text="📣 Campaign OFF.")
        return

    if t.startswith("sources") or t.startswith("⚙"):
        txt = "Current RSS sources:\n" + "\n".join(f"- {u}" for u in RSS_LINKS)
        bot.send_message(chat_id=chat_id, text=txt)
        return

    if "close keyboard" in t or "❌" in t:
        bot.send_message(
            chat_id=chat_id,
            text="Keyboard closed.",
            reply_markup=ReplyKeyboardMarkup([["/menu"]], resize_keyboard=True)
        )
        return

    # unknown admin command
    bot.send_message(chat_id=chat_id, text="Unknown admin command. 'menu' likho help ke liye.")


def handle_update(update: dict):
    """Webhook se aane wale saare Telegram updates ko yahan process karo."""
    message = update.get("message") or update.get("edited_message")
    if not message:
        return

    chat = message.get("chat", {})
    chat_type = chat.get("type")
    chat_id = chat.get("id")
    from_user = message.get("from", {})
    user_id = from_user.get("id")
    text = message.get("text", "") or ""

    # sirf private chat handle, channel/group ignore
    if chat_type != "private" or not text:
        return

    if not user_id:
        return

    if not is_admin(int(user_id)):
        # Non-admin ko simple reply
        try:
            bot.send_message(
                chat_id=chat_id,
                text="Ye private control bot hai. Sirf owner ke liye available hai."
            )
        except Exception:
            pass
        return

    handle_admin_text(chat_id=int(chat_id), user_id=int(user_id), text=text)


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

        # morning brief ~09:00 IST
        if now_ist.hour == 9:
            if last_morning_brief_date != now_ist.date():
                try:
                    send_brief("morning")
                except Exception as e:
                    logging.error(f"Morning brief error: {e}")
                last_morning_brief_date = now_ist.date()

        # night brief ~22:00 IST
        if now_ist.hour == 22:
            if last_night_brief_date != now_ist.date():
                try:
                    send_brief("night")
                except Exception as e:
                    logging.error(f"Night brief error: {e}")
                last_night_brief_date = now_ist.date()

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

    # Startup DM demo - sirf OWNER ko
    if CONTROL_CHAT_ID:
        try:
            ist = ist_now()
            ms
