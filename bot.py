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

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
    raise RuntimeError(
        "Missing environment variables! TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID set karo."
    )

# ------------ GLOBAL SETTINGS ------------
RSS_LINKS = [
    "https://news.google.com/rss?hl=en-IN&gl=IN&ceid=IN:en",
    "https://feeds.reuters.com/reuters/worldNews",
    "https://feeds.bbci.co.uk/news/world/rss.xml",
]

NEWS_PER_RUN = 3
sent_ids = set()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

request = Request(con_pool_size=8)
bot = Bot(token=TELEGRAM_TOKEN, request=request)

app = Flask(__name__)


# ------------ URL SHORTENER ------------
def short_url(url):
    try:
        r = requests.get("https://tinyurl.com/api-create.php", params={"url": url}, timeout=10)
        if r.status_code == 200:
            return r.text.strip()
        return url
    except:
        return url


# ------------ AI SUMMARY + HINDI VOICE TEXT ------------
def ai_summarize(title, description, link):
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
        "temperature": 0.7
    }

    try:
        res = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json=payload,
            timeout=30
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
def make_voice(text):
    if not text.strip():
        return None

    temp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
    temp.close()

    try:
        tts = gTTS(text=text, lang="hi")
        tts.save(temp.name)
        return temp.name
    except:
        return None


# ------------ FETCH LATEST NEWS ------------
def fetch_news():
    entries = []

    for url in RSS_LINKS:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:5]:
                eid = getattr(e, "id", None) or getattr(e, "link", None)
                if eid:
                    entries.append({
                        "id": eid,
                        "title": getattr(e, "title", ""),
                        "link": getattr(e, "link", ""),
                        "summary": getattr(e, "summary", "") or getattr(e, "description", "")
                    })
        except:
            pass

    return entries[::-1]


# ------------ PREMIUM FORMAT MESSAGE ------------
def format_message(title, summary_en, link, hashtags):
    safe_title = html.escape(title)
    safe_summary = html.escape(summary_en)
    safe_tags = html.escape(hashtags)

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
        f"{safe_tags}\n"
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

        if e["id"] in sent_ids:
            continue

        title = e["title"]
        link = e["link"]
        desc = e["summary"]

        summary_en, voice_hi, hashtags = ai_summarize(title, desc, link)

        if not summary_en:
            sent_ids.add(e["id"])
            continue

        msg_text = format_message(title, summary_en, link, hashtags)

        # Send text news
        bot.send_message(
            chat_id=CHANNEL_ID,
            text=msg_text,
            parse_mode="HTML",
            disable_web_page_preview=True
        )

        # Send Hindi voice
        if voice_hi:
            audio_path = make_voice(voice_hi)
            if audio_path:
                with open(audio_path, "rb") as f:
                    bot.send_audio(
                        chat_id=CHANNEL_ID,
                        audio=f,
                        caption="🎙 हिंदी ऑडियो न्यूज़"
                    )
                os.remove(audio_path)

        sent_ids.add(e["id"])
        count += 1
        time.sleep(2)


# ------------ SCHEDULER ------------

def scheduler_loop():
    while True:
        schedule.run_pending()
        time.sleep(1)


@app.route("/")
def home():
    return "Ayush Telegram News Bot Running!", 200


def main():
    logging.info("🔥 Ayush Telegram News Bot Started!")
    schedule.every(10).minutes.do(post_news)

    # Run first time instantly
    try:
        post_news()
    except:
        pass

    t = Thread(target=scheduler_loop, daemon=True)
    t.start()

    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
