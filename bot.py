import os
import time
import html
from datetime import datetime
import pytz
import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
RUN_SECONDS = int(os.getenv("RUN_SECONDS", "20700"))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

def send_telegram(text):
    if not BOT_TOKEN or not CHAT_ID:
        print("❌ BOT_TOKEN أو CHAT_ID ناقص")
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        return r.status_code == 200
    except Exception as e:
        print(f"❌ خطأ تلغرام: {e}")
        return False

def fetch_barchart_postmarket(limit=15):
    """جلب أسهم Post-Market Gainers من Barchart"""
    url = "https://www.barchart.com/stocks/post-market-trading/percent-change/advances"
    try:
        res = requests.get(url, headers=HEADERS, timeout=20)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, "html.parser")

        rows = []
        # محاولة استخراج الجدول
        table = soup.find("table") or soup.find("div", class_="bc-table-scrollable-inner")
        if not table:
            # إذا ما لقى جدول، نرجع قائمة فارغة (الصفحة ديناميكية)
            print("⚠️ Barchart: الصفحة ديناميكية، لم أستخرج بيانات مباشرة")
            return []

        for tr in table.find_all("tr")[1:limit+1]:
            cols = tr.find_all("td")
            if len(cols) < 4:
                continue
            try:
                symbol = cols[0].get_text(strip=True)
                last = cols[1].get_text(strip=True)
                change = cols[2].get_text(strip=True)
                if symbol and last:
                    rows.append({
                        "symbol": symbol,
                        "price": last,
                        "change": change
                    })
            except:
                continue
        return rows
    except Exception as e:
        print(f"⚠️ خطأ Barchart: {e}")
        return []

def fetch_finviz_fallback(limit=12):
    """احتياطي من Finviz إذا Barchart فشل"""
    url = "https://finviz.com/screener.ashx?v=111&s=ta_topgainers"
    try:
        res = requests.get(url, headers=HEADERS, timeout=20)
        res.raise_for_status()
        from html.parser import HTMLParser

        class Parser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.rows = []
                self.current = None
                self.cell_id = None
                self.depth = 0
                self.text = []
            def handle_starttag(self, tag, attrs):
                if tag != "div": return
                a = dict(attrs)
                if self.cell_id is not None:
                    self.depth += 1
                    return
                if "data-row-key" in a:
                    self.current = {"row_key": a["data-row-key"]}
                    self.rows.append(self.current)
                elif self.current is not None and "data-column-id" in a:
                    self.cell_id = a["data-column-id"]
                    self.depth = 1
                    self.text = []
            def handle_endtag(self, tag):
                if tag != "div" or self.cell_id is None: return
                self.depth -= 1
                if self.depth == 0:
                    self.current[self.cell_id] = "".join(self.text).strip()
                    self.cell_id = None
            def handle_data(self, data):
                if self.cell_id is not None:
                    self.text.append(data)

        p = Parser()
        p.feed(res.text)
        result = []
        seen = set()
        for row in p.rows:
            t = str(row.get("row_key", "")).strip().upper()
            if not t or t in seen: continue
            seen.add(t)
            result.append({
                "symbol": t,
                "price": row.get("price", "-"),
                "change": row.get("change", "-")
            })
            if len(result) >= limit:
                break
        return result
    except Exception as e:
        print(f"⚠️ خطأ Finviz: {e}")
        return []

def build_list_message(stocks, source="Barchart"):
    if not stocks:
        return None

    now = datetime.now(pytz.timezone("Asia/Riyadh")).strftime("%H:%M")
    lines = [f"🚨 <b>Post-Market Movers</b> · {source}", f"⏰ <code>{now}</code>\n"]

    for i, s in enumerate(stocks, 1):
        symbol = html.escape(str(s.get("symbol", "-")))
        price = html.escape(str(s.get("price", "-")))
        change = html.escape(str(s.get("change", "-")))
        lines.append(f"<b>{i}. {symbol}</b>  ·  <code>{price}</code>  ·  {change}")

    lines.append(f"\n📊 المصدر: {source}")
    return "\n".join(lines)

def main():
    start = time.time()
    print(f"🚀 رادار Post-Market (قائمة مجمعة) بدأ")
    send_telegram("🟢 <b>تم تشغيل رادار Post-Market</b>\n📌 يرسل قائمة مجمعة كل 3 دقائق")

    last_sent_hash = None

    while True:
        if time.time() - start >= RUN_SECONDS:
            send_telegram("🔴 انتهى وقت التشغيل")
            break

        try:
            print(f"⏳ فحص... {datetime.now().strftime('%H:%M:%S')}")

            stocks = fetch_barchart_postmarket(15)
            source = "Barchart"

            if not stocks:
                print("→ التحول إلى Finviz كاحتياطي")
                stocks = fetch_finviz_fallback(12)
                source = "Finviz"

            if stocks:
                msg = build_list_message(stocks, source)
                # منع تكرار نفس القائمة
                current_hash = hash(msg)
                if current_hash != last_sent_hash:
                    if send_telegram(msg):
                        last_sent_hash = current_hash
                        print(f"✅ أرسل قائمة من {len(stocks)} سهم ({source})")
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
