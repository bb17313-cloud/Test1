import os
import json
import re
import html
import time
import requests
import urllib.parse
from datetime import datetime
from zoneinfo import ZoneInfo

# ================================
# إعدادات التلغرام والبيئة والمفاتيح
# ================================
TOKEN = os.environ.get("BOT_TOKEN", "ضع_توكن_البوت_هنا")
CHAT_ID = os.environ.get("CHAT_ID", "ضع_معرف_الشات_هنا")
POLYGON_API_KEY = os.environ.get("POLYGON_API_KEY", "ضع_مفتاح_بوليغون_هنا")

RIYADH = ZoneInfo("Asia/Riyadh")
NEW_YORK = ZoneInfo("America/New_York")
SEEN_FILE = "seen_stocks.json"
MAX_SHOWN = 40   

# ================================
# إدارة جلسات السوق الثلاث
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
        print("❌ خطأ: توكن البوت أو معرف الشات غير متوفر في المتغيرات البيئية.")
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
        if response.status_code == 200:
            print("✅ تم إرسال الرسالة إلى تيليجرام بنجاح.")
            return True
        else:
            print(f"❌ فشل الإرسال إلى تيليجرام: {response.text}")
            return False
    except Exception as e:
        print(f"❌ خطأ في الاتصال بتيليجرام: {e}")
        return False

# ================================
# تحليل ووصف إفصاحات SEC
# ================================
def get_sec_description(f_form, item_val="", doc_text=""):
    f_upper = f_form.upper()
    item_str = str(item_val)
    text_lower = doc_text.lower() if doc_text else ""

    if f_upper == "10-K":
        if "item 1a" in text_lower or "risk factors" in text_lower:
            return "عوامل المخاطرة"
        elif "item 7" in text_lower:
            return "تحليل الإدارة للوضع المالي"
        elif "item 8" in text_lower:
            return "القوائم المالية"
        elif "item 3" in text_lower:
            return "القضايا والإجراءات القانونية"
        elif "item 9a" in text_lower:
            return "الضوابط والإجراءات الرقابية"
        return "التقرير المالي السنوي"

    if f_upper == "10-Q":
        if "item 2" in text_lower or "md&a" in text_lower:
            return "تحليل الإدارة الفصلي"
        elif "item 1a" in text_lower:
            return "عوامل المخاطرة الجديدة"
        return "القوائم المالية الفصلية"

    if f_upper == "8-K":
        if "1.01" in item_str:
            return "اتفاقية جوهرية جديدة"
        elif "2.01" in item_str:
            return "استحواذ أو بيع أصول"
        elif "2.02" in item_str:
            return "نتائج الأرباح الأولية"
        elif "5.02" in item_str:
            return "تغييرات الإدارة التنفيذية"
        elif "3.02" in item_str:
            return "مبيعات أسهم غير مسجلة"
        elif "8.01" in item_str:
            return "أحداث جوهرية أخرى"
        return "إفصاح جوهري طارئ"

    if "14A" in f_upper:
        if "compensation" in text_lower:
            return "مكافآت التنفيذيين"
        elif "ownership" in text_lower:
            return "ملكية كبار الملاك"
        return "حوكمة الشركات"

    if f_upper == "6-K":
        return "تقرير شركة أجنبية"

    if f_upper.startswith("424B") or any(f_upper.startswith(p) for p in ["S-1", "S-3", "F-1", "F-3"]):
        if "warrant" in text_lower or "exhibit" in text_lower:
            return "اتفاقيات ومرفقات الإصدار"
        return "نشرة طرح واكتتاب أسهم"

    return "إفصاح رسمي معتمد"

# ================================
# جلب إفصاحات SEC فقط
# ================================
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
                    items_list = recent.get("items", [])

                    for i in range(min(10, len(forms))):
                        f_form = forms[i]
                        f_date = filing_dates[i]
                        acc_num = accession_numbers[i]
                        acc_clean = acc_num.replace("-", "")
                        p_doc = primary_docs[i]
                        item_val = items_list[i] if i < len(items_list) else ""

                        if p_doc:
                            filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik_raw}/{acc_clean}/{p_doc}"
                        else:
                            filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik_raw}/{acc_clean}/{acc_num}-index.htm"

                        doc_text = ""
                        if f_form.upper() in ["10-K", "10-Q", "8-K", "6-K"] or f_form.upper().startswith("424B"):
                            try:
                                doc_res = requests.get(filing_url, headers=sec_headers, timeout=3)
                                if doc_res.status_code == 200:
                                    doc_text = doc_res.text
                            except Exception:
                                pass

                        brief_desc = get_sec_description(f_form, item_val, doc_text)
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
            if len(sec_lines) >= 5:
                break

    sec_summary = "\n".join(sec_lines) if sec_lines else "• لا توجد إفصاحات حديثة"
    return sec_summary, cik_str_val, latest_accession

# ========================================
# جلب وتصفية الأسهم من Polygon.io Snapshot
# ========================================
def fetch_filtered_stocks(session_type):
    if not POLYGON_API_KEY or POLYGON_API_KEY == "ضع_مفتاح_بوليغون_هنا":
        print("❌ خطأ: لم يتم إدخال POLYGON_API_KEY")
        return []

    url = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers?apiKey={POLYGON_API_KEY}"
    
    try:
        response = requests.get(url, timeout=15)
        if response.status_code == 200:
            all_tickers_data = response.json().get("tickers", [])
            filtered_stocks = []
            
            vol_limit = 15000 if session_type in ["premarket", "postmarket"] else 30000
            
            for ticker_info in all_tickers_data:
                symbol = ticker_info.get("ticker", "")
                
                prev_day = ticker_info.get("prevDay", {})
                prev_close = prev_day.get("c", 0.0)
                
                last_trade = ticker_info.get("lastTrade", {}).get("p", 0.0)
                min_bar = ticker_info.get("min", {})
                min_close = min_bar.get("c", 0.0)
                
                price = last_trade or min_close or ticker_info.get("day", {}).get("c", 0.0)
                
                # حساب نسبة التغير الحقيقية مقارنة بإغلاق أمس (يتجاوز مشكلة صفر البري ماركت)
                if prev_close > 0 and price > 0:
                    change_pct = ((price - prev_close) / prev_close) * 100
                else:
                    change_pct = ticker_info.get("todaysChangePercent", 0.0)
                
                day_volume = ticker_info.get("day", {}).get("v", 0)
                min_volume = min_bar.get("v", 0)
                volume = max(day_volume, min_volume)
                
                # تطبيق الشروط (التغير >= 2% والحجم المطلوب)
                if change_pct >= 2.0 and volume > vol_limit:
                    filtered_stocks.append({
                        "symbol": symbol,
                        "price": price,
                        "change_pct": change_pct,
                        "volume": volume
                    })
            
            # ترتيب الأسهم تنازلياً حسب الأعلى صعوداً
            filtered_stocks.sort(key=lambda x: x["change_pct"], reverse=True)
            return filtered_stocks
    except Exception as e:
        print(f"❌ خطأ في جلب بيانات Polygon: {e}")
    
    return []

# ================================
# الدالة الرئيسية لتشغيل البوت
# ================================
def main():
    session_key, session_name = get_current_session()
    print(f"🔍 الجلسة الحالية: {session_name}")

    if session_key == "closed":
        print("⏸️ السوق مغلق حالياً، لا توجد تنبيهات للإرسال.")
        return

    stocks = fetch_filtered_stocks(session_key)
    if not stocks:
        print("⚠️ لم يتم العثور على أسهم تطابق شروط الفلترة في هذه الجلسة.")
        return

    seen_stocks = load_seen()
    new_stocks_found = 0
    
    for stock in stocks[:MAX_SHOWN]:
        symbol = stock["symbol"]
        if symbol in seen_stocks:
            continue

        sec_summary, _, latest_acc = fetch_sec_filings_only(symbol)

        message = (
            f"🚨 <b>تنبيه سهم جديد</b> ({session_name})\n\n"
            f"🔹 الرمز: <b>{symbol}</b>\n"
            f"💵 السعر: <b>${stock['price']:.2f}</b>\n"
            f"📈 التغير: <b>{stock['change_pct']:+.2f}%</b>\n"
            f"📊 الحجم: <b>{stock['volume']:,}</b>\n\n"
            f"📄 <b>أحدث إفصاحات SEC:</b>\n{sec_summary}"
        )

        if send_telegram_message(message):
            seen_stocks[symbol] = {
                "time": datetime.now(RIYADH).strftime("%H:%M:%S"),
                "latest_acc": latest_acc
            }
            new_stocks_found += 1
            time.sleep(1)

    save_seen(seen_stocks)
    print(f"✨ تم الانتهاء. إرسال {new_stocks_found} تنبيه جديد.")

if __name__ == "__main__":
    main()
