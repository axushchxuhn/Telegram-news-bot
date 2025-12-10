import os
import time
import json
import html
import logging
import tempfile
from datetime import datetime, timedelta
from threading import Thread
from difflib import SequenceMatcher

import feedparser
import schedule
import requests
from flask import Flask, request
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.utils.request import Request


# ============= ENVIRONMENT CONFIG =============

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")  # optional
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")    # optional (DM alerts)

SAFE_MODE = os.getenv("SAFE_MODE", "0").lower() in ("1", "true", "yes")

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHANNEL_ID!")


# ============= GLOBAL SETTINGS =============

WORLD_RSS = [
    "https://news.google.com/rss?hl=en-IN&gl=IN&ceid=IN:en",
    "https://feeds.reuters.com/reuters/worldNews",
    "https://feeds.bbci.co.uk/news/world/rss.xml",
]

INDIA_RSS = [
    "https://news.google.com/rss?hl=en-IN&gl=IN&ceid=IN:hi",
]

NEWS_PER_RUN = 5               # max news per cycle
SUMMARY_MAX_CHARS = 260        # summary length limit

sent_ids = set()               # avoid exact duplicates
sent_titles = []               # for similarity-based duplicate block
recent_posts = []              # for summaries

last_run_time = None
last_run_count = 0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

request_obj = Request(con_pool_size=8)
bot = Bot(token=TELEGRAM_BOT_TOKEN, request=request_obj)
app = Flask(__name__)


# ============= ADMIN ALERTS =============

def send_admin_alert(message: str):
    if not ADMIN_CHAT_ID:
        return
    try:
        bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=f"⚠ <b>News Bot Alert</b>\n{html.escape(message)}",
            parse_mode="HTML"
        )
    except Exception as e:
        logging.error(f"Failed to send admin alert: {e}")


# ============= URL SHORTENER =============

def short_url(url: str) -> str:
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


# ============= TEXT LIMIT HELPER =============

def limit_text(text: str, max_chars: int) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut + "..."


# ============= AI SUMMARY (TEXT ONLY) =============

def ai_summarize(title: str, description: str, link: str):
    """
    Sirf English summary + AI hashtags.
    Agar OPENAI_API_KEY nahi hai ya error aata hai to (None, None) return karega.
    """
    if not OPENAI_API_KEY:
        return None, None

    system_prompt = """
Tum ek professional news assistant ho.

Har news ke liye SIRF yeh JSON return karo:

{
  "summary_en": "...",
  "hashtags": "..."
}

Rules:
- summary_en: 2-3 short simple English lines.
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
        "max_tokens": 160,
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
            obj.get("hashtags", "#WorldNews #Breaking #Update"),
        )

    except Exception as e:
        logging.error(f"AI Error: {e}")
        send_admin_alert(f"AI summary failed: {e}")
        return None, None


# ============= TOPIC/CATEGORY DETECTION =============

def detect_category(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ["stock", "market", "share", "crypto", "bitcoin", "sensex", "nifty"]):
        return "FINANCE"
    if any(w in t for w in ["technology", "tech", "ai ", "artificial intelligence", "app", "startup"]):
        return "TECH"
    if any(w in t for w in ["parliament", "election", "minister", "government", "modi", "bjp", "congress"]):
        return "POLITICS"
    if any(w in t for w in ["war", "attack", "missile", "gaza", "ukraine", "russia", "israel", "palestine"]):
        return "CONFLICT"
    return "GENERAL"


def topic_hashtags(title: str, summary: str, base_tags: str | None) -> str:
    text = (title + " " + summary).lower()
    tags = set()

    if base_tags:
        for t in base_tags.split():
            if t.startswith("#"):
                tags.add(t)

    # India
    if any(w in text for w in ["india", "delhi", "mumbai", "kolkata", "bangalore", "modi"]):
        tags.update(["#India", "#Politics"])

    # USA
    if any(w in text for w in ["usa", "us ", "biden", "trump", "white house"]):
        tags.update(["#USA", "#Politics"])

    # Finance
    if any(w in text for w in ["stock", "market", "share", "crypto", "bitcoin", "sensex", "nifty"]):
        tags.update(["#Finance", "#Economy"])

    # Tech
    if any(w in text for w in ["technology", "tech", "ai ", "artificial intelligence", "app", "startup"]):
        tags.update(["#Technology", "#Innovation"])

    # Conflict
    if any(w in text for w in ["war", "attack", "missile", "gaza", "ukraine", "russia", "israel", "palestine"]):
        tags.update(["#Conflict", "#GlobalCrisis"])

    tags.add("#WorldNews")
    tags.add("#Breaking")

    final = list(tags)[:6]
    return " ".join(final)


# ============= DUPLICATE (SIMILARITY) CHECK =============

def is_similar_title(new_title: str, threshold: float = 0.9) -> bool:
    new_title_clean = new_title.lower().strip()
    for old in sent_titles:
        ratio = SequenceMatcher(None, old, new_title_clean).ratio()
        if ratio >= threshold:
            return True
    return False


# ============= IMAGE EXTRACTION =============

def extract_image_from_entry(e) -> str | None:
    try:
        media_content = getattr(e, "media_content", None)
        if media_content and len(media_content) > 0:
            url = media_content[0].get("url")
            if url:
                return url
    except Exception:
        pass

    try:
        media_thumb = getattr(e, "media_thumbnail", None)
        if media_thumb and len(media_thumb) > 0:
            url = media_thumb[0].get("url")
            if url:
                return url
    except Exception:
        pass

    try:
        for link in getattr(e, "links", []):
            if link.get("type", "").startswith("image/"):
                return link.get("href")
    except Exception:
        pass

    return None


# ============= FETCH NEWS (INDIA + WORLD + IMAGE) =============

def fetch_news():
    entries = []

    # World news
    for url in WORLD_RSS:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:5]:
                eid = getattr(e, "id", None) or getattr(e, "link", None)
                if not eid or eid in sent_ids:
                    continue

                entries.append(
                    {
                        "id": eid,
                        "title": getattr(e, "title", ""),
                        "link": getattr(e, "link", ""),
                        "summary": getattr(e, "summary", "") or getattr(e, "description", ""),
                        "region": "WORLD",
                        "image": extract_image_from_entry(e),
                    }
                )
        except Exception as ex:
            logging.error(f"World RSS fetch error from {url}: {ex}")
            send_admin_alert(f"World RSS error: {ex}")

    # India news
    for url in INDIA_RSS:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:5]:
                eid = getattr(e, "id", None) or getattr(e, "link", None)
                if not eid or eid in sent_ids:
                    continue

                entries.append(
                    {
                        "id": eid,
                        "title": getattr(e, "title", ""),
                        "link": getattr(e, "link", ""),
                        "summary": getattr(e, "summary", "") or getattr(e, "description", ""),
                        "region": "INDIA",
                        "image": extract_image_from_entry(e),
                    }
                )
        except Exception as ex:
            logging.error(f"India RSS fetch error from {url}: {ex}")
            send_admin_alert(f"India RSS error: {ex}")

    return entries[::-1]


# ============= PREMIUM MESSAGE FORMAT =============

def format_message(region: str, category: str, title: str, summary_en: str, link: str, hashtags: str) -> str:
    safe_title = html.escape(title)
    safe_summary = html.escape(summary_en)
    safe_tags = html.escape(hashtags)

    ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
    time_str = ist.strftime("%d %b %Y | %I:%M %p IST")

    short = short_url(link)

    region_tag = "🇮🇳 INDIA" if region == "INDIA" else "🌍 WORLD"
    cat_emoji = {
        "FINANCE": "💰 Finance",
        "TECH": "💻 Tech",
        "POLITICS": "🏛 Politics",
        "CONFLICT": "⚔ Conflict",
        "GENERAL": "📰 General",
    }.get(category, "📰 General")

    header_tag = f"[{region_tag} | {cat_emoji}]"

    msg = (
        f"🌍 <b>International Breaking News</b> {header_tag}\n"
        f"📅 <i>{time_str}</i>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📰 <b>{safe_title}</b>\n\n"
        f"{safe_summary}\n\n"
        f"🔗 Full Story: <a href=\"{short}\">Read here</a>\n\n"
        f"{safe_tags}\n"
        "Powered by <i>@Axshchxhan</i>"
    )

    return msg


def build_keyboard(short_link: str):
    buttons = [
        [InlineKeyboardButton("🌐 Full Story", url=short_link)],
        [InlineKeyboardButton("📢 Join Updates Channel", url="https://t.me/chxuhan")],
    ]
    return InlineKeyboardMarkup(buttons)


# ============= MAIN POSTING JOB =============

def post_news():
    global last_run_time, last_run_count

    logging.info("Checking for new news...")
    entries = fetch_news()

    count = 0
    try:
        for e in entries:
            if count >= NEWS_PER_RUN:
                break

            news_id = e["id"]
            title = e["title"]
            link = e["link"]
            desc = e["summary"]
            region = e["region"]
            image_url = e["image"]

            if news_id in sent_ids:
                continue

            if is_similar_title(title):
                logging.info(f"Skipping similar title: {title}")
                sent_ids.add(news_id)
                continue

            full_text = (title + " " + desc).strip()
            category = detect_category(full_text)

            summary_en, ai_tags = ai_summarize(title, desc, link)
            if not summary_en:
                summary_en = (html.unescape(desc) or title).strip()
                if not summary_en:
                    summary_en = title
                summary_en = limit_text(summary_en, SUMMARY_MAX_CHARS)
                ai_tags = ""
            else:
                summary_en = limit_text(summary_en, SUMMARY_MAX_CHARS)

            all_tags = topic_hashtags(title, summary_en, ai_tags)

            msg_text = format_message(region, category, title, summary_en, link, all_tags)
            short = short_url(link)
            keyboard = build_keyboard(short)

            # Photo first (if available)
            if image_url:
                try:
                    bot.send_photo(
                        chat_id=TELEGRAM_CHANNEL_ID,
                        photo=image_url,
                        caption=f"📰 {title}",
                        parse_mode="HTML",
                    )
                except Exception as ex:
                    logging.error(f"Image send error: {ex}")

            # Then text message
            bot.send_message(
                chat_id=TELEGRAM_CHANNEL_ID,
                text=msg_text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=keyboard,
            )

            sent_ids.add(news_id)
            sent_titles.append(title.lower().strip())
            recent_posts.append({
                "time": datetime.utcnow(),
                "region": region,
                "category": category,
                "title": title,
                "link": link,
            })

            count += 1
            time.sleep(2)

        last_run_time = datetime.utcnow()
        last_run_count = count
        logging.info(f"Posted {count} news this run.")

    except Exception as e:
        logging.error(f"post_news failed: {e}")
        send_admin_alert(f"post_news failed: {e}")


# ============= DAILY SUMMARY (MORNING / NIGHT) =============

def build_daily_summary(title_prefix: str):
    if not recent_posts:
        return "No news collected yet."

    last_items = recent_posts[-10:]
    lines = []
    for i, item in enumerate(reversed(last_items), start=1):
        flag = "🇮🇳" if item["region"] == "INDIA" else "🌍"
        cat = item.get("category", "GENERAL")
        cat_emoji = {
            "FINANCE": "💰",
            "TECH": "💻",
            "POLITICS": "🏛",
            "CONFLICT": "⚔",
            "GENERAL": "📰",
        }.get(cat, "📰")
        lines.append(f"{i}. {flag} {cat_emoji} {item['title']}")

    body = "\n".join(lines)

    ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
    time_str = ist.strftime("%d %b %Y | %I:%M %p IST")

    text = (
        f"{title_prefix}\n"
        f"🗓 <i>{time_str}</i>\n\n"
        f"{body}\n\n"
        "Stay tuned on this channel for live updates.\n"
        "Powered by @Axshchxhan"
    )
    return text


def morning_summary():
    text = build_daily_summary("🌅 <b>Morning Top Headlines</b>")
    bot.send_message(
        chat_id=TELEGRAM_CHANNEL_ID,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


def night_summary():
    text = build_daily_summary("🌙 <b>Night Wrap-up</b>")
    bot.send_message(
        chat_id=TELEGRAM_CHANNEL_ID,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


# ============= DAILY POLL =============

def daily_poll():
    try:
        question = "Aapko kaunsi news category sabse zyada pasand hai?"
        options = ["🌍 World", "🇮🇳 India", "💰 Finance", "💻 Tech", "Mix sab"]
        bot.send_poll(
            chat_id=TELEGRAM_CHANNEL_ID,
            question=question,
            options=options,
            is_anonymous=True,
            allows_multiple_answers=False,
        )
    except Exception as e:
        logging.error(f"Poll send failed: {e}")
        send_admin_alert(f"Poll failed: {e}")


# ============= STATUS TEXT FOR /status =============

def build_status_text() -> str:
    ist_now = datetime.utcnow() + timedelta(hours=5, minutes=30)
    now_str = ist_now.strftime("%d %b %Y | %I:%M %p IST")

    if last_run_time:
        ist_last = last_run_time + timedelta(hours=5, minutes=30)
        last_str = ist_last.strftime("%d %b %Y | %I:%M %p IST")
    else:
        last_str = "N/A"

    text = (
        "<b>Ayush Turbo News Bot Status</b>\n\n"
        f"🕒 Current time (IST): <i>{now_str}</i>\n"
        f"✅ Last news run: <i>{last_str}</i>\n"
        f"📰 Last run posted: <b>{last_run_count}</b> articles\n"
        f"🛡 Safe mode: <b>{'ON' if SAFE_MODE else 'OFF'}</b>\n"
        f"🧠 AI summary: <b>{'ON' if OPENAI_API_KEY else 'OFF'}</b>\n"
        f"📦 Stored posts for summary: <b>{len(recent_posts)}</b>\n"
    )
    return text


# ============= SCHEDULER =============

def scheduler_loop():
    while True:
        schedule.run_pending()
        time.sleep(1)


@app.route("/")
def home():
    return "Ayush Turbo News Bot Running!", 200


# ============= TELEGRAM WEBHOOK FOR /status =============

@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    try:
        data = request.get_json(force=True)
    except Exception:
        return "ok"

    message = data.get("message") or data.get("edited_message")
    if not message:
        return "ok"

    chat = message.get("chat", {})
    chat_id = chat.get("id")
    chat_type = chat.get("type")
    text = message.get("text", "")

    # Sirf private chat me /status ka reply
    if chat_type == "private" and text.startswith("/status"):
        status_text = build_status_text()
        try:
            bot.send_message(
                chat_id=chat_id,
                text=status_text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as e:
            logging.error(f"Failed to send /status response: {e}")

    return "ok"


def send_demo_message():
    ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
    time_str = ist.strftime("%d %b %Y | %I:%M %p IST")

    text = (
        "🟢 <b>Ayush Turbo News Bot Updated</b>\n"
        f"🗓 <i>{time_str}</i>\n\n"
        "Bot safaltapoorvak chal raha hai.\n"
        "Ab se India + World news, images, categories, hashtags, polls & summaries "
        "sab automatic milenge.\n\n"
        "#Update #LiveBot\n"
        "Powered by @Axshchxhan"
    )

    bot.send_message(
        chat_id=TELEGRAM_CHANNEL_ID,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


def main():
    logging.info("🔥 Ayush Turbo News Bot Started!")

    # News schedule
    if SAFE_MODE:
        schedule.every(30).minutes.do(post_news)
    else:
        schedule.every(15).minutes.do(post_news)

    # Morning & Night summaries (UTC times approx for IST)
    schedule.every().day.at("02:30").do(morning_summary)  # ~08:00 IST
    schedule.every().day.at("16:00").do(night_summary)   # ~21:30 IST

    # Daily engagement poll (~20:30 IST)
    schedule.every().day.at("15:00").do(daily_poll)

    # Startup demo
    try:
        send_demo_message()
    except Exception as e:
        logging.error(f"Demo message error: {e}")
        send_admin_alert(f"Demo message error: {e}")

    # First immediate news run
    try:
        post_news()
    except Exception as e:
        logging.error(f"First run post_news error: {e}")
        send_admin_alert(f"First run error: {e}")

    t = Thread(target=scheduler_loop, daemon=True)
    t.start()

    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
