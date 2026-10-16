

import os

import json

import time

import requests

from datetime import datetime

from zoneinfo import ZoneInfo

# ================================

# إعدادات التلغرام والبيئة والمفاتيح

# ================================

TOKEN = os.environ.get("BOT_TOKEN", "ضع_توكن_البوت_هنا")

CHAT_ID = os.environ.get("CHAT_ID", "ضع_معرف_الشات_هنا")

RIYADH = ZoneInfo("Asia/Riyadh")

NEW_YORK = ZoneInfo("America/New_York")

SEEN_FILE = "seen_stocks.json"

MAX_CANDIDATES = 250

MAX_SHOWN = 250

REALERT_THRESHOLD = 1.0  # إعادة التنبيه عند ارتفاع السعر 1% عن آخر تنبيه

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

            if (

                data.get("date") == today

                and data.get("session") == session_key

            ):

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

    if (

        not TOKEN

        or TOKEN == "ضع_توكن_البوت_هنا"

        or not CHAT_ID

        or CHAT_ID == "ضع_معرف_الشات_هنا"

    ):

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

        if response.status_code == 200:

            return True

        print(f"❌ فشل إرسال تيليجرام: {response.text[:300]}")

        return False

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

    sec_headers = {

        "User-Agent": "StockRadarBot/1.0 (contact@stockradar.com)"

    }

    filings = []

    cik_str_val = None

    try:

        tickers_res = requests.get(

            "https://www.sec.gov/files/company_tickers.json",

            headers=sec_headers,

            timeout=5

        )

        if tickers_res.status_code == 200:

            tickers_data = tickers_res.json()

            cik = None

            cik_raw = None

            for idx, val in tickers_data.items():

                if val.get("ticker", "").upper() == symbol:

                    cik_raw = str(val.get("cik_str"))

                    cik = cik_raw.zfill(10)

                    cik_str_val = cik_raw

                    break

            if cik:

                sub_url = (

                    f"https://data.sec.gov/submissions/CIK{cik}.json"

                )

                sub_res = requests.get(

                    sub_url,

                    headers=sec_headers,

                    timeout=5

                )

                if sub_res.status_code == 200:

                    recent = (

                        sub_res.json()

                        .get("filings", {})

                        .get("recent", {})

                    )

                    forms = recent.get("form", [])

                    accession_numbers = recent.get(

                        "accessionNumber", []

                    )

                    primary_docs = recent.get("primaryDocument", [])

                    filing_dates = recent.get("filingDate", [])

                    for i in range(min(5, len(forms))):

                        f_form = forms[i]

                        f_date = filing_dates[i]

                        acc_num = accession_numbers[i]

                        acc_clean = acc_num.replace("-", "")

                        p_doc = primary_docs[i]

                        if p_doc:

                            filing_url = (

                                "https://www.sec.gov/Archives/edgar/data/"

                                f"{cik_raw}/{acc_clean}/{p_doc}"

                            )

                        else:

                            filing_url = (

                                "https://www.sec.gov/Archives/edgar/data/"

                                f"{cik_raw}/{acc_clean}/{acc_num}-index.htm"

                            )

                        filings.append({

                            "form": f_form,

                            "date": f_date,

                            "url": filing_url,

                            "accession": acc_num,

                            "brief": get_sec_description(f_form)

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

            sec_lines.append(

                f"• <b>{f['form']}</b> ({f['date']}): "

                f"<a href='{f['url']}'>{f['brief']}</a>"

            )

            if len(sec_lines) >= 3:

                break

    sec_summary = (

        "\n".join(sec_lines)

        if sec_lines

        else "• لا توجد إفصاحات حديثة"

    )

    return sec_summary, cik_str_val, latest_accession

# ========================================

# جلب الأسهم من Yahoo Finance

# ========================================

def fetch_all_market_stocks():

    all_stocks_dict = {}

    url = (

        "https://query1.finance.yahoo.com/v1/finance/"

        "screener/predefined/saved"

    )

    params = {

        "formatted": "true",

        "scrIds": "day_gainers",

        "count": MAX_CANDIDATES,

        "start": 0,

        "sortField": "percentchange",

        "sortType": "DESC"

    }

    headers = {

        "User-Agent": (

            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "

            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "

            "Mobile/15E148 Safari/604.1"

        )

    }

    try:

        print("🔄 [Yahoo Finance] جاري جلب الأسهم...")

        res = requests.get(

            url,

            params=params,

            headers=headers,

            timeout=20

        )

        res.raise_for_status()

        data = res.json()

        results = (

            data.get("finance", {})

            .get("result", [])

        )

        if not results:

            print("⚠️ لم يُرجع Yahoo Finance نتائج.")

            return []

        quotes = results[0].get("quotes", [])

        for item in quotes:

            symbol = item.get("symbol")

            price = item.get("regularMarketPrice")

            change_pct = item.get("regularMarketChangePercent")

            # لا توجد شروط سعر أو ارتفاع أو بورصة

            if not symbol or price is None:

                continue

            try:

                price = float(price)

                change_pct = float(change_pct or 0)

            except (TypeError, ValueError):

                continue

            if price <= 0:

                continue

            exchange = (

                item.get("fullExchangeName")

                or item.get("exchange")

                or "غير محدد"

            )

            all_stocks_dict[symbol] = {

                "symbol": symbol,

                "price": price,

                "change_pct": change_pct,

                "exchange": exchange

            }

        print(

            f"✅ [Yahoo Finance] تم جلب "

            f"{len(all_stocks_dict)} سهم."

        )

    except Exception as e:

        print(f"❌ [Yahoo Finance] خطأ في جلب الأسهم: {e}")

        return []

    filtered_list = list(all_stocks_dict.values())

    filtered_list.sort(

        key=lambda x: x["change_pct"],

        reverse=True

    )

    return filtered_list[:MAX_SHOWN]

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

        print("⚠️ لم يتم جلب أسهم في هذه الجولة.")

        return

    seen_stocks = load_seen()

    new_stocks_found = 0

    for stock in stocks:

        symbol = stock["symbol"]

        current_price = stock["price"]

        current_change = stock["change_pct"]

        exchange = stock["exchange"]

        # شرط التكرار الوحيد:

        # إعادة التنبيه إذا ارتفع السعر 1% أو أكثر

        # مقارنة بسعر آخر تنبيه ناجح لهذا السهم.

        if symbol in seen_stocks:

            last_alert_price = seen_stocks[symbol].get(

                "last_price"

            )

            if last_alert_price is not None:

                try:

                    last_alert_price = float(last_alert_price)

                    required_price = (

                        last_alert_price

                        * (1 + REALERT_THRESHOLD / 100)

                    )

                    if current_price < required_price:

                        continue

                except (TypeError, ValueError):

                    # إذا تعذر قراءة السعر السابق،

                    # يسمح بإرسال تنبيه جديد.

                    pass

        sec_summary, _, latest_acc = fetch_sec_filings_only(

            symbol

        )

        tv_url = (

            f"https://www.tradingview.com/chart/?symbol={symbol}"

        )

        message = (

            f"🚨 <b>تنبيه سهم صاعد / تحديث ارتفاع</b> "

            f"({session_name})\n\n"

            f"🔹 الرمز: <b>{symbol}</b> ({exchange})\n"

            f"💵 السعر الحالي: <b>${current_price:.2f}</b>\n"

            f"📈 التغير: <b>{current_change:+.2f}%</b>\n\n"

            f"📄 <b>أحدث إفصاحات SEC:</b>\n"

            f"{sec_summary}\n\n"

            f"📈 <a href='{tv_url}'>"

            f"فتح الشارت على TradingView</a>"

        )

        if send_telegram_message(message):

            # تسجيل السعر الحالي كأساس للتنبيه التالي

            seen_stocks[symbol] = {

                "time": datetime.now(RIYADH).strftime("%H:%M:%S"),

                "first_change_pct": (

                    seen_stocks.get(symbol, {}).get(

                        "first_change_pct",

                        current_change

                    )

                ),

                "last_price": current_price,

                "latest_acc": latest_acc

            }

            new_stocks_found += 1

            time.sleep(1)

    save_seen(seen_stocks)

    print(

        f"✨ انتهت دورة الفحص. "

        f"إجمالي التنبيهات الجديدة/المحدثة: {new_stocks_found}"

    )

# ================================

# التشغيل (دورة واحدة)

# ================================

def main():

    print(

        "🚀 بدء تشغيل بوت مراقبة الأسهم "

        "(Yahoo Finance - إعادة التنبيه عند ارتفاع 1%)"

    )

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
