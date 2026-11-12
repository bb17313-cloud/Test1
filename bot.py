import os
import time
import html
from html.parser import HTMLParser
from datetime import datetime
import pytz
import requests

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

FINVIZ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# لمنع تكرار نفس السعر لنفس السهم خلال الدورة
alerts_state = {}

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

def fetch_finviz_top_gainers(limit=20):
    """جلب الأسهم المتحركة (Top Gainers) حالياً"""
    url = "https://finviz.com/screener.ashx?v=111&s=ta_topgainers"
    try:
        res = requests.get(url, headers=FINVIZ_HEADERS, timeout=20)
        res.raise_for_status()
        parser = FinvizRowsParser()
        parser.feed(res.text)

        rows = []
        seen_keys = set()
        for row in parser.rows:
            key = row.get("row_key")
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            rows.append(row)
        return rows[:limit]
    except Exception as e:
        print(f"⚠️ خطأ أثناء الاتصال بـ Finviz: {e}")
        return []

def send_telegram_message(message, parse_mode="HTML"):
    if not BOT_TOKEN or not CHAT_ID:
        print("❌ خطأ: لم يتم العثور على إعدادات BOT_TOKEN أو CHAT_ID.")
        return False

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True
    }
    try:
        response = requests.post(url, json=payload, timeout=15)
        if response.status_code != 200:
            print(f"❌ فشل إرسال رسالة التلغرام: {response.text}")
            return False
        return True
    except Exception as e:
        print(f"❌ خطأ أثناء الاتصال بالتلغرام: {e}")
        return False

def to_float(value):
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None

def send_movers():
    """إرسال الأسهم المتحركة حالياً بدون أي شروط"""
    global alerts_state
    data_rows = fetch_finviz_top_gainers(limit=15)
    if not data_rows:
        print("⚠️ لم يتم العثور على أسهم متحركة.")
        return 0

    sent = 0
    now_str = datetime.now(pytz.timezone('Asia/Riyadh')).strftime("%H:%M")

    for stock in data_rows:
        ticker = str(stock.get('row_key', '')).strip().upper()
        price = to_float(stock.get('price'))
        if not ticker or price is None or price <= 0:
            continue

        # تجنب إرسال نفس السعر بالضبط لنفس السهم في الدورة الحالية
        last_price = alerts_state.get(ticker)
        if last_price is not None and abs(price - last_price) < 0.001:
            continue

        change = str(stock.get('change', '-'))
        volume = str(stock.get('volume', '-'))
        sector = str(stock.get('sector', '-'))
        industry = str(stock.get('industry', '-'))

        tv_url = f"https://www.tradingview.com/chart/?symbol={ticker}"

        message = (
            f"🚨 <b>Post-Market Mover</b>\n"
            f"⏰ <code>{now_str}</code>\n\n"
            f"🔹 <b>{html.escape(ticker)}</b>\n"
            f"💵 <code>${price:.2f}</code> · 📈 <code>{html.escape(change)}</code>\n"
            f"📊 Vol: <code>{html.escape(volume)}</code>\n"
            f"🏭 {html.escape(sector)} · {html.escape(industry)}\n\n"
            f"📈 <a href='{tv_url}'>TradingView</a>"
        )

        if send_telegram_message(message):
            alerts_state[ticker] = price
            sent += 1
            time.sleep(0.8)

    return sent

def main_loop():
    print("🚀 رادار Post-Market يعمل كل 3 دقائق...")
    send_telegram_message("🟢 <b>تم تشغيل رادار Post-Market</b>\n📌 يجلب الأسهم المتحركة كل 3 دقائق بدون شروط")

    while True:
        try:
            print(f"⏳ فحص الأسهم المتحركة... {datetime.now().strftime('%H:%M:%S')}")
            sent = send_movers()
            print(f"✅ تم إرسال {sent} بطاقة.")
        except Exception as e:
            print(f"❌ خطأ: {e}")

        time.sleep(180)  # كل 3 دقائق

if __name__ == "__main__":
    main_loop()
