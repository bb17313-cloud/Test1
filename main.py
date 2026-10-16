import os
import json
import time
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# ================================
# إعدادات التلغرام والبيئة والمفاتيح
# ================================
TOKEN = os.environ.get("BOT_TOKEN", "ضع_توكن_البوت_هنا")
CHAT_ID = os.environ.get("CHAT_ID", "ضع_معرف_الشات_هنا")

ALPACA_KEY = os.environ.get("ALPACA_API_KEY", "ضع_مفتاح_البكا_هنا")
ALPACA_SECRET = os.environ.get("ALPACA_SECRET_KEY", "ضع_السر_هنا")

RIYADH = ZoneInfo("Asia/Riyadh")
NEW_YORK = ZoneInfo("America/New_York")
SEEN_FILE = "seen_stocks.json"
MAX_CANDIDATES = 45
MAX_SHOWN = 25
MIN_VOLUME = 5000
REALERT_THRESHOLD = 1.5

# ================================
# إدارة جلسات السوق
# ================================
def get_current_session():
    now_ny = datetime.now(NEW_YORK)
    
    if now_ny.weekday() not in range(5):
        return "closed", "⏸️ المغلق (عطلة نهاية الأسبوع)"

    time_num_ny = now_ny.hour * 100 + now_ny.minute

    if 400 <= time_num_ny < 930:
        return "premarket", "🌅 Pre-Market (ما قبل الافتتاح)"
    elif 930 <= time_num_ny < 1600:
        return "market", "🔔 Main Session (السوق الرئيسي)"
    elif 1600 <= time_num_ny < 2000:
        return "postmarket", "🌙 Post-Market (ما بعد الإغلاق)"
    else:
        return "closed", "⏸️ المغلق (خارج أوقات التداول)"

def load_seen():
    today = datetime.now(RIYADH).strftime("%Y-%m-%d")
    session_key, _ = get_current_session()
    try:
        if os.path.exists(SEEN_FILE):
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("date") == today and data.get("session") == session_key:
                return data.get("stocks", {})
    except Exception as e:
        print(f"⚠️ خطأ في قراءة ملف السجل: {e}")
    return {}

def save_seen(stocks_data):
    today = datetime.now(RIYADH).strftime("%Y-%m-%d")
    session_key, _ = get_current_session()
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "date": today,
                "session": session_key,
                "stocks": stocks_data,
                "last_update": datetime.now(RIYADH).strftime("%H:%M:%S")
            }, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"❌ خطأ في حفظ ملف السجل: {e}")

# ================================
# إرسال الرسائل عبر تيليجرام
# ================================
def send_telegram_message(text):
    if not TOKEN or TOKEN == "ضع_توكن_البوت_هنا" or not CHAT_ID or CHAT_ID == "ضع_معرف_الشات_هنا":
        print("❌ خطأ: توكن البوت أو معرف الشات غير متوفر.")
        return False
        
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    
    try:
        response = requests.post(url, json=payload, timeout=10)
        return response.status_code == 200
    except Exception as e:
        print(f"❌ خطأ في الاتصال بتيليجرام: {e}")
        return False

# ================================
# إفصاحات SEC
# ================================
def get_sec_description(f_form):
    f_upper = f_form.upper()
    if f_upper == "10-K":
        return "التقرير المالي السنوي"
    elif f_upper == "10-Q":
        return "القوائم المالية الفصلية"
    elif f_upper == "8-K":
        return "إفصاح جوهري طارئ"
    return "إفصاح رسمي معتمد"

def fetch_sec_filings_only(symbol):
    symbol = symbol.upper().strip()
    sec_headers = {"User-Agent": "StockRadarBot/1.0 (contact@stockradar.com)"}
    filings = []
    cik_str_val = None

    try:
        tickers_res = requests.get("https://www.sec.gov/files/company_tickers.json", headers=sec_headers, timeout=5)
        if tickers_res.status_code == 200:
            tickers_data = tickers_res.json()
            cik = None
            cik_raw = None
            for idx, val in tickers_data.items():
                if val.get("ticker", "").upper() == symbol:
                    cik = str(val.get("cik_str")).zfill(10)
                    cik_raw = str(val.get("cik_str"))
                    cik_str_val = cik_raw
                    break

            if cik:
                sub_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
                sub_res = requests.get(sub_url, headers=sec_headers, timeout=5)
                if sub_res.status_code == 200:
                    recent = sub_res.json().get("filings", {}).get("recent", {})
                    forms = recent.get("form", [])
                    accession_numbers = recent.get("accessionNumber", [])
                    primary_docs = recent.get("primaryDocument", [])
                    filing_dates = recent.get("filingDate", [])

                    for i in range(min(5, len(forms))):
                        f_form = forms[i]
                        f_date = filing_dates[i]
                        acc_num = accession_numbers[i]
                        acc_clean = acc_num.replace("-", "")
                        p_doc = primary_docs[i]

                        if p_doc:
                            filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik_raw}/{acc_clean}/{p_doc}"
                        else:
                            filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik_raw}/{acc_clean}/{acc_num}-index.htm"

                        brief_desc = get_sec_description(f_form)
                        filings.append({
                            "form": f_form, "date": f_date, "url": filing_url,
                            "accession": acc_num, "brief": brief_desc,
                        })
    except Exception as e:
        print(f"⚠️ خطأ في جلب إفصاحات SEC لـ {symbol}: {e}")

    sec_lines = []
    seen_accessions = set()
    latest_accession = ""
    
    if filings:
        latest_accession = filings[0]["accession"]
        for f in filings:
            if f["accession"] in seen_accessions:
                continue
            seen_accessions.add(f["accession"])
            sec_lines.append(f"• <b>{f['form']}</b> ({f['date']}): <a href='{f['url']}'>{f['brief']}</a>")
            if len(sec_lines) >= 3:
                break

    sec_summary = "\n".join(sec_lines) if sec_lines else "• لا توجد إفصاحات حديثة"
    return sec_summary, cik_str_val, latest_accession

# ========================================
# المسح الشامل - Alpaca فقط
# ========================================
def fetch_all_market_stocks():
    all_stocks_dict = {}

    if not ALPACA_KEY or ALPACA_KEY == "ضع_مفتاح_البكا_هنا" or not ALPACA_SECRET or ALPACA_SECRET == "ضع_السر_هنا":
        print("❌ مفاتيح Alpaca غير موجودة")
        return []

    try:
        print("🔄 [Alpaca] جاري سحب Top Gainers...")
        url = "https://data.alpaca.markets/v1beta1/screener/stocks/movers?top=50"
        headers = {
            "APCA-API-KEY-ID": ALPACA_KEY,
            "APCA-API-SECRET-KEY": ALPACA_SECRET
        }
        res = requests.get(url, headers=headers, timeout=12)
        print(f"📡 [Alpaca] Status: {res.status_code}")

        if res.status_code == 200:
            data = res.json()
            gainers = data.get("gainers", [])
            print(f"📦 [Alpaca] عدد الأسهم الراجعة: {len(gainers)}")

            for item in gainers:
                symbol = item.get("symbol")
                price = float(item.get("price", 0) or 0)
                change_pct = float(item.get("percent_change", 0) or 0)
                volume = int(item.get("volume", 0) or 0)

                if symbol and price > 0 and volume >= MIN_VOLUME and change_pct >= 3.0:
                    all_stocks_dict[symbol] = {
                        "symbol": symbol,
                        "price": price,
                        "change_pct": change_pct,
                        "volume": volume
                    }
            print(f"✅ [Alpaca] تم تخزين {len(all_stocks_dict)} سهم")
        else:
            print(f"❌ [Alpaca] فشل - الرد: {res.text[:400]}")
    except Exception as e:
        print(f"⚠️ [Alpaca] خطأ: {e}")

    # ترتيب وأخذ أفضل 25
    filtered_list = list(all_stocks_dict.values())
    filtered_list.sort(key=lambda x: x["change_pct"], reverse=True)

    candidates = filtered_list[:MAX_CANDIDATES]
    top_25 = candidates[:MAX_SHOWN]

    print(f"🎯 أعلى {len(candidates)} مرشح | سيتم متابعة أفضل {len(top_25)}")
    
    if top_25:
        print("أعلى الأسهم حالياً:")
        for s in top_25[:8]:
            print(f"   → {s['symbol']}: {s['change_pct']:+.2f}% | ${s['price']:.2f} | Vol: {s['volume']:,}")

    return top_25

# ================================
# دورة الفحص
# ================================
def run_scan_cycle():
    session_key, session_name = get_current_session()
    print(f"🔍 الجلسة الحالية: {session_name}")

    if session_key == "closed":
        print("⏸️ السوق مغلق حالياً، لا توجد تنبيهات للإرسال.")
        return

    stocks = fetch_all_market_stocks()
    if not stocks:
        print("⚠️ لم يتم العثور على أسهم تطابق الشروط في هذه الجولة.")
        return

    seen_stocks = load_seen()
    new_stocks_found = 0
    
    for stock in stocks:
        symbol = stock["symbol"]
        current_price = stock["price"]
        current_change = stock["change_pct"]
        volume_val = stock["volume"]
        
        if symbol in seen_stocks:
            first_change = seen_stocks[symbol].get("first_change_pct", 0.0)
            if current_change < (first_change + REALERT_THRESHOLD):
                continue

        sec_summary, _, latest_acc = fetch_sec_filings_only(symbol)
        tv_url = f"https://www.tradingview.com/chart/?symbol={symbol}"

        vol_str = f"{volume_val:,}" if volume_val > 0 else "غير متوفر"

        message = (
            f"🚨 <b>تنبيه سهم صاعد / تحديث ارتفاع</b> ({session_name})\n\n"
            f"🔹 الرمز: <b>{symbol}</b>\n"
            f"💵 السعر الحالي: <b>${current_price:.2f}</b>\n"
            f"📈 التغير: <b>{current_change:+.2f}%</b>\n"
            f"📊 الحجم: <b>{vol_str}</b>\n\n"
            f"📄 <b>أحدث إفصاحات SEC:</b>\n{sec_summary}\n\n"
            f"📈 <a href='{tv_url}'>فتح الشارت على TradingView</a>"
        )

        if send_telegram_message(message):
            if symbol not in seen_stocks:
                seen_stocks[symbol] = {
                    "time": datetime.now(RIYADH).strftime("%H:%M:%S"),
                    "first_change_pct": current_change,
                    "last_price": current_price,
                    "latest_acc": latest_acc
                }
            else:
                seen_stocks[symbol]["time"] = datetime.now(RIYADH).strftime("%H:%M:%S")
                seen_stocks[symbol]["last_price"] = current_price
                seen_stocks[symbol]["latest_acc"] = latest_acc

            new_stocks_found += 1
            time.sleep(1)

    save_seen(seen_stocks)
    print(f"✨ تم الانتهاء من دورة الفحص. إرسال {new_stocks_found} تنبيه جديد/محدث.")

# ================================
# التشغيل (دورة واحدة)
# ================================
def main():
    print("🚀 بدء تشغيل بوت مراقبة الأسهم (Alpaca فقط)")
    print(f"📌 الشروط: حجم ≥ {MIN_VOLUME} | أعلى {MAX_CANDIDATES} → أفضل {MAX_SHOWN}")
    print(f"📌 إعادة التنبيه فقط عند ارتفاع +{REALERT_THRESHOLD}% عن الرسالة الأولى")
    
    try:
        session_key, session_name = get_current_session()
        print(f"🔍 الجلسة الحالية: {session_name}")

        if session_key == "closed":
            print("⏸️ السوق مغلق حالياً، لا توجد تنبيهات.")
            return

        run_scan_cycle()

    except Exception as e:
        print(f"❌ حدث خطأ غير متوقع: {e}")

if __name__ == "__main__":
    main()
