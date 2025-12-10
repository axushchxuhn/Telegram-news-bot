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
from gtts import gTTS
from flask import Flask
from telegram import Bot
from telegram.utils.request import Request


# ------------ ENVIRONMENT CONFIG ------------

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SELF_PING_URL = os.getenv("SELF_PING_URL")  # keep-alive ke liye (Render URL)

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID or not OPENAI_API_KEY:
    raise RuntimeError(
        "Missing environment variables! TELEGRAM_BOT_TOKEN, "
        "TELEGRAM_CHANNEL_ID, OPENAI_API_KEY set karo."
    )


# ------------ GLOBAL SETTINGS ------------

RSS_LINKS = [
    "https://news.google.com/rss?hl=en-IN&gl=IN&ceid=IN:en",
    "https://feeds.reuters.com/reuters/worldNews",
    "https://feeds.bbci.co.uk/news/world/rss.xml",
]

# Har 15 minute me kitni news bhejni hai
NEWS_PER_RUN = 5

# Duplicate news rokne ke liye
sent_ids = set()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

request = Request(con_pool_size=8)
bot = Bot(token=TELEGRAM_BOT_TOKEN, request=request)

app = Flask(__name__)


# ------------ URL SHORTENER ------------

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


# ------------ AI SUMMARY + HINDI VOICE TEXT ------------

def ai_summarize(title: str, description: str, link: str):
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
            obj.get("hashtags", "#WorldNews #Breaking #Update")
        )

    except Exception as e:
        logging.error(f"AI Error: {e}")
        return None, None, None


# ------------ HINDI AUDIO ------------

def make_voice(text: str):
    if not text or not text.strip():
        return None

    temp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
    temp.close()

    try:
        tts = gTTS(text=text, lang="hi")
        tts.save(temp.name)
        return temp.name
    except Exception:
        return None


# ------------ FETCH LATEST NEWS ------------

def fetch_news():
    entries = []

    for url in RSS_LINKS:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:5]:
                eid = getattr(e, "id", None) or getattr(e, "link", None)
                if not eid:
                    continue

                # Agar pehle hi bhej chuke hain to chhodo
                if eid in sent_ids:
                    continue

                entries.append({
                    "id": eid,
                    "title": getattr(e, "title", ""),
                    "link": getattr(e, "link", ""),
                    "summary": getattr(e, "summary", "") or getattr(e, "description", "")
                })
        except Exception:
            pass

    # Latest wali pehle
    return entries[::-1]


# ------------ PREMIUM FORMAT MESSAGE ------------

def format_message(title: str, summary_en: str, link: str, hashtags: str) -> str:
    safe_title = html.escape(title)
    safe_summary = html.escape(summary_en)

    base_tags = hashtags or "#WorldNews #Breaking #Update"
    growth_tags = "#TopStories #GlobalNews"
    safe_tags = html.escape(base_tags + " " + growth_tags)

    # IST TIME
    ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
    time_str = ist.strftime("%d %b %Y | %I:%M %p IST")

    short = short_url(link)

    msg = (
        f"🚨 <b>International Breaking News</b>\n"
        f"📅 <i>{time_str}</i>\n\n"
        f"📰 <b>{safe_title}</b>\n\n"
        f"{safe_summary}\n\n"
        f"🔗 Full Story: <a href=\"{short}\">Read here</a>\n\n"
        f"{safe_tags}\n\n"
        f"<i>Share karein aur updates ke liye channel pe bane rahein.</i>\n"
        f"<i>Powered by @Axshchxhan</i>"
    )

    return msg


# ------------ MAIN POSTING JOB ------------

def post_news():
    logging.info("Checking for new news...")
    entries = fetch_news()

    count = 0
    for e in entries:
        if count >= NEWS_PER_RUN:
            break

        news_id = e["id"]

        title = e["title"]
        link = e["link"]
        desc = e["summary"]

        summary_en, voice_hi, hashtags = ai_summarize(title, desc, link)

        if not summary_en:
            # AI fail hua to bhi duplicate na aaye, id mark kar do
            sent_ids.add(news_id)
            continue

        msg_text = format_message(title, summary_en, link, hashtags)

        # Text news
        bot.send_message(
            chat_id=TELEGRAM_CHANNEL_ID,
            text=msg_text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

        # Hindi voice news
        if voice_hi:
            audio_path = make_voice(voice_hi)
            if audio_path:
                with open(audio_path, "rb") as f:
                    bot.send_audio(
                        chat_id=TELEGRAM_CHANNEL_ID,
                        audio=f,
                        caption="🎙 हिंदी ऑडियो न्यूज़",
                    )
                os.remove(audio_path)

        sent_ids.add(news_id)
        count += 1
        time.sleep(2)


# ------------ KEEP-ALIVE PING ------------

def ping_self():
    if not SELF_PING_URL:
        return
    try:
        requests.get(SELF_PING_URL, timeout=8)
        logging.info("Self ping OK")
    except Exception as e:
        logging.warning(f"Self ping failed: {e}")


# ------------ STARTUP DEMO MESSAGE ------------

def send_startup_demo():
    try:
        ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
        time_str = ist.strftime("%d %b %Y | %I:%M %p IST")

        text = (
            "🟢 <b>Ayush Telegram News Bot Updated</b>\n"
            f"🗓 <i>{time_str}</i>\n\n"
            "📰 Demo Update:\n"
            "Bot successfully restart ho chuka hai. "
            "Ab se har 15 minute latest international breaking news, "
            "English summary + Hindi audio ke saath milegi.\n\n"
            "#WorldNews #Breaking #Update\n"
            "Powered by @Axshchxhan"
        )

        bot.send_message(
            chat_id=TELEGRAM_CHANNEL_ID,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as e:
        logging.error(f"Startup demo send nahi ho paya: {e}")


# ------------ SCHEDULER LOOP ------------

def scheduler_loop():
    while True:
        schedule.run_pending()
        time.sleep(1)


@app.route("/")
def home():
    return "Ayush Telegram News Bot Running!", 200


def main():
    logging.info("🔥 Ayush Telegram News Bot Started!")

    # Ek baar start pe demo message
    send_startup_demo()

    # Har 15 min me news (5 articles)
    schedule.every(15).minutes.do(post_news)

    # Har 5 min me self ping (keep-alive)
    schedule.every(5).minutes.do(ping_self)

    # Pehli baar turant ek run (agar chaho)
    try:
        post_news()
    except Exception:
        pass

    t = Thread(target=scheduler_loop, daemon=True)
    t.start()

    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
