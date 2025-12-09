import os
import time
import requests
import feedparser
import schedule

# ====== CONFIG (ENVIRONMENT VARIABLES) =======

BOT_TOKEN = os.environ.get("BOT_TOKEN")   # Render me set karoge
CHANNEL_ID = os.environ.get("CHANNEL_ID") # @chxuhan
AI_KEY = os.environ.get("AI_KEY")         # OpenAI API key

if not BOT_TOKEN or not CHANNEL_ID or not AI_KEY:
    raise ValueError("BOT_TOKEN, CHANNEL_ID, AI_KEY environment variables set karo.")

TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ====== NEWS SOURCES (WORLDWIDE) =======
RSS_LINKS = [
    "https://news.google.com/news/rss?hl=en-IN&gl=IN&ceid=IN:en",
    "http://feeds.bbci.co.uk/news/world/rss.xml",
    "https://www.reutersagency.com/feed/?best-topics=world&post_type=best",
]

# ========== AI SUMMARY (OpenAI API) ==========
def summarize(text: str) -> str:
    url = "https://api.openai.com/v1/chat/completions"

    system_prompt = (
        "Tum ek Telegram channel ke liye world news summarizer ho. "
        "Har news ko 2-3 line ki short Hinglish me batao, "
        "simple words, 1-2 emoji, aur end me 3 short hashtags lagao. "
        "Bahut lamba mat likho."
    )

    payload = {
        "model": "gpt-4.1-mini",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
        "max_tokens": 120,
        "temperature": 0.7,
    }

    headers = {
        "Authorization": f"Bearer {AI_KEY}",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        print("❌ Error in summarize:", e)
        return "News summarize karte waqt error aaya 😅"


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
            all_news.append(f"{entry.title} - {entry.link}")

    return all_news[:3]  # total 3 hi lenge


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
                short = summarize(news)
                post = f"🌍 *World Update*\n\n{short}"
                send_message(post)
                time.sleep(5)
            except Exception as e:
                print("❌ Ek news post karte time error:", e)
                time.sleep(2)

    except Exception as e:
        print("❌ post_news top-level error:", e)


def main():
    # Har 10 minute me news
    schedule.every(10).minutes.do(post_news)
    # Test ke liye:
    # schedule.every(1).minutes.do(post_news)

    print("🤖 Auto News Bot 24/7 mode me chal raha hai (Render pe)...")
    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
