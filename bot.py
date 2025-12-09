import os
import time
import threading

import requests
import feedparser
import schedule
from flask import Flask

# ====== CONFIG (sirf Telegram ke liye, koi AI nahi) =======

BOT_TOKEN = os.environ.get("BOT_TOKEN")   # Render Environment me set karo
CHANNEL_ID = os.environ.get("CHANNEL_ID") # e.g. @chxuhan

if not BOT_TOKEN or not CHANNEL_ID:
    raise ValueError("BOT_TOKEN aur CHANNEL_ID environment variables set karo.")

TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ====== NEWS SOURCES (WORLDWIDE) =======
RSS_LINKS = [
    "https://news.google.com/news/rss?hl=en-IN&gl=IN&ceid=IN:en",
    "http://feeds.bbci.co.uk/news/world/rss.xml",
    "https://www.reutersagency.com/feed/?best-topics=world&post_type=best",
]

# ========== TELEGRAM SEND MESSAGE ==========
def send_message(text: str):
    try:
        payload = {
            "chat_id": CHANNEL_ID,
            "text": text,
            "parse_mode": "Markdown",
        }
        r = requests.post(f"{TELEGRAM_API_URL}/sendMessage", json=payload, timeout=20)
        r.raise_for_status()
        print("✅ Message sent to channel")
    except Exception as e:
        print("❌ Error sending message:", e)


# ========== FETCH NEWS ==========
def get_latest_news():
    all_news = []

    for link in RSS_LINKS:
        feed = feedparser.parse(link)
        for entry in feed.entries[:3]:  # har source se 3
            title = entry.title
            url = entry.link
            all_news.append({"title": title, "link": url})

    # total 3 hi lenge
    return all_news[:3]


# ========== POST NEWS TO CHANNEL ==========
def post_news():
    print("🔄 New cycle: news fetch + post karna start...")
    try:
        news_list = get_latest_news()
        if not news_list:
            print("❌ Koi news nahi mili.")
            return

        for news in news_list:
            try:
                title = news["title"]
                link = news["link"]

                # 👉 Tumhara chosen format: Premium + Breaking
                message = (
                    "🚨 *International Breaking News*\n\n"
                    f"📰 *{title}*\n\n"
                    f"🔍 Full Story: {link}\n\n"
                    "#WorldNews #Breaking #Update"
                )

                send_message(message)
                time.sleep(5)
            except Exception as e:
                print("❌ Ek news post karte time error:", e)
                time.sleep(2)

    except Exception as e:
        print("❌ post_news top-level error:", e)


# ========== SCHEDULER THREAD ==========
def run_scheduler():
    # Har 10 minutes me news
    schedule.every(10).minutes.do(post_news)
    # Test ke liye agar fast chahiye:
    # schedule.every(1).minutes.do(post_news)

    print("🕒 Scheduler thread start ho gaya...")
    while True:
        schedule.run_pending()
        time.sleep(1)


# ========== FLASK APP (Render ke liye PORT open) ==========
app = Flask(__name__)

@app.route("/")
def index():
    return "Telegram News Bot (NO AI) is running ✅"


def main():
    # Scheduler ko background thread me chalao
    t = threading.Thread(target=run_scheduler, daemon=True)
    t.start()

    # Flask app ko Render ke PORT par start karo
    port = int(os.environ.get("PORT", 10000))
    print(f"🌐 Flask web server starting on port {port}...")
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    print("🤖 Auto News Bot (NO AI) start ho raha hai...")
    main()
