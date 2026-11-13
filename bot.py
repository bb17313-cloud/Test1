import os
import time
import html
from html.parser import HTMLParser
from datetime import datetime
import pytz
import requests

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
RUN_SECONDS = int(os.getenv("RUN_SECONDS", "20700"))

FINVIZ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

class FinvizRowsParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.current_row = None
        self.cell_id = None
        self.cell_depth = 0
        self.cell_text = []

    def handle_starttag(self, tag, attrs):
        if tag != "div":
            return
        a = dict(attrs)
        if self.cell_id is not None:
            self.cell_depth += 1
            return
        if "data-row-key" in a:
            self.current_row = {"row_key": a["data-row-key"]}
            self.rows.append(self.current_row)
        elif self.current_row is not None and "data-column-id" in a:
            self.cell_id = a["data-column-id"]
            self.cell_depth = 1
            self.cell_text = []

    def handle_endtag(self, tag):
        if tag != "div" or self.cell_id is None:
            return
        self.cell_depth -= 1
        if self.cell_depth == 0:
            self.current_row[self.cell_id] = "".join(self.cell_text).strip()
            self.cell_id = None

    def handle_data(self, data):
        if self.cell_id is not None:
            self.cell_text.append(data)

def fetch_top_gainers(limit=15):
    url = "https://finviz.com/screener.ashx?v=111&s=ta_topgainers"
    try:
        res = requests.get(url, headers=FINVIZ_HEADERS, timeout=20)
        res.raise_for_status()
        parser = FinvizRowsParser()
        parser.feed(res.text)

        rows = []
        seen = set()
        for row in parser.rows:
            ticker = str(row.get("row_key", "")).strip().upper()
            if not ticker or ticker in seen:
                continue
            seen.add(ticker)
            rows.append({
                "ticker": ticker,
                "price": row.get("price", "-"),
                "change": row.get("change", "-"),
                "volume": row.get("volume", "-")
            })
            if len(rows) >= limit:
                break
        return rows
    except Exception as e:
        print(f"⚠️ خطأ Finviz: {e}")
        return []

def send_telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        print("❌ BOT_TOKEN أو CHAT_ID غير موجود")
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        return r.status_code == 200
    except Exception as e:
        print(f"❌ خطأ تلغرام: {e}")
        return False

def build_message(stocks):
    if not stocks:
        return None

    now = datetime.now(pytz.timezone("Asia/Riyadh")).strftime("%H:%M")
    lines = [
        f"🚨 <b>Post-Market Movers</b>",
        f"⏰ <code>{now}</code>",
        ""
    ]

    for i, s in enumerate(stocks, 1):
        ticker = html.escape(s["ticker"])
        price = html.escape(str(s["price"]))
        change = html.escape(str(s["change"]))
        lines.append(f"<b>{i}. {ticker}</b>  ·  <code>${price}</code>  ·  {change}")

    lines.append("")
    lines.append("📊 المصدر: Finviz")
    return "\n".join(lines)

def main():
    start_time = time.time()
    print(f"🚀 رادار Post-Market بدأ (قائمة مجمعة كل 3 دقائق)")
    send_telegram("🟢 <b>تم تشغيل رادار Post-Market</b>\n📌 يرسل قائمة مجمعة كل 3 دقائق")

    last_hash = None

    while True:
        if time.time() - start_time >= RUN_SECONDS:
            print("⏰ انتهى وقت التشغيل")
            send_telegram("🔴 <b>انتهى تشغيل الرادار</b>")
            break

        try:
            print(f"⏳ فحص... {datetime.now().strftime('%H:%M:%S')}")
            stocks = fetch_top_gainers(15)

            if stocks:
                msg = build_message(stocks)
                current_hash = hash(msg)

                if current_hash != last_hash:
                    if send_telegram(msg):
                        last_hash = current_hash
                        print(f"✅ أرسل قائمة بـ {len(stocks)} سهم")
                    else:
                        print("❌ فشل الإرسال")
                else:
                    print("⏸ نفس القائمة، ما أرسلت")
            else:
                print("⚠️ ما لقيت أسهم")

        except Exception as e:
            print(f"❌ خطأ: {e}")

        time.sleep(180)

if __name__ == "__main__":
    main()
