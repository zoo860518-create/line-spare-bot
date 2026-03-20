import taiwan_holidays
import os
import json
import hmac
import hashlib
import base64
import requests
import re
from datetime import datetime, timedelta, date
from flask import Flask, request, abort, jsonify

app = Flask(__name__)

CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

TARGET_LINE_USER_ID = os.getenv("TARGET_LINE_USER_ID", "")
CRON_SECRET = os.getenv("CRON_SECRET", "")

SAFETY_FILE = "safety_stock.json"
LATEST_STOCK_FILE = "latest_stock.json"
HOLIDAYS_FILE = "holidays_2026.json"


def load_json_file(path, default=None):
    if default is None:
        default = {}
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json_file(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def normalize_text(text):
    return re.sub(r"\s+", " ", text.strip()).lower()


def verify_signature(body, signature):
    hash_value = hmac.new(
        CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256
    ).digest()
    expected_signature = base64.b64encode(hash_value).decode("utf-8")
    return hmac.compare_digest(expected_signature, signature)


def reply_message(reply_token, text):
    url = "https://api.line.me/v2/bot/message/reply"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}"
    }
    payload = {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": text}]
    }
    response = requests.post(url, headers=headers, json=payload, timeout=10)
    print("reply_message status:", response.status_code)
    print("reply_message body:", response.text)
    response.raise_for_status()


def push_message(to_user_id, text):
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}"
    }
    payload = {
        "to": to_user_id,
        "messages": [{"type": "text", "text": text}]
    }
    response = requests.post(url, headers=headers, json=payload, timeout=10)
    print("push_message status:", response.status_code)
    print("push_message body:", response.text)
    response.raise_for_status()


def extract_section(report_text, section_name):
    headings = ["ZN", "CFXD"]
    other_headings = [h for h in headings if h != section_name]

    if other_headings:
        next_heading_pattern = "|".join(re.escape(h) for h in other_headings)
        pattern = rf"(?is)^\s*{re.escape(section_name)}\s*$([\s\S]*?)(?=^\s*(?:{next_heading_pattern})\s*$|\Z)"
    else:
        pattern = rf"(?is)^\s*{re.escape(section_name)}\s*$([\s\S]*?)\Z"

    match = re.search(pattern, report_text, re.MULTILINE)
    return match.group(1) if match else ""


def find_item_qty_in_text(text, item_name):
    lines = text.splitlines()
    target = normalize_text(item_name)

    for line in lines:
        line_norm = normalize_text(line)
        if target in line_norm:
            qty_match = re.search(r"(?:x|\*)\s*(\d+)", line, re.IGNORECASE)
            if qty_match:
                return int(qty_match.group(1))
    return None


def parse_report_to_stock(report_text, safety_stock):
    parsed = {
        "ZN": {},
        "CFXD": {},
        "COMMON": {}
    }

    zn_text = extract_section(report_text, "ZN")
    cfxd_text = extract_section(report_text, "CFXD")
    whole_text = report_text

    for item in safety_stock.get("ZN", {}):
        qty = find_item_qty_in_text(zn_text, item)
        if qty is not None:
            parsed["ZN"][item] = qty

    for item in safety_stock.get("CFXD", {}):
        qty = find_item_qty_in_text(cfxd_text, item)
        if qty is not None:
            parsed["CFXD"][item] = qty

    for item in safety_stock.get("COMMON", {}):
        qty = find_item_qty_in_text(whole_text, item)
        if qty is not None:
            parsed["COMMON"][item] = qty

    return parsed


def format_low_stock(parsed_stock, safety_stock):
    results = []

    next_holiday = get_next_long_holiday_info(date.today())
    header_lines = ["⚠️ Low stock alert"]

    if next_holiday:
        header_lines.append(
            f"Next long holiday in {next_holiday['days_remaining']} day(s)"
        )
        header_lines.append(
            f"Holiday period: {next_holiday['start']} to {next_holiday['end']}"
        )

    header = "\n".join(header_lines)

    for project in ["ZN", "CFXD", "COMMON"]:
        low_items = []
        for item_name, safety_qty in safety_stock.get(project, {}).items():
            current_qty = parsed_stock.get(project, {}).get(item_name)
            if current_qty is None:
                continue
            if current_qty < safety_qty:
                low_items.append(f"- {item_name}: {current_qty} / {safety_qty}")

        if low_items:
            results.append(f"{project}\n" + "\n".join(low_items))

    if results:
        return header + "\n\n" + "\n\n".join(results)

    if next_holiday:
        return (
            f"✅ 目前沒有低於安全庫存的品項\n"
            f"Next long holiday in {next_holiday['days_remaining']} day(s)\n"
            f"Holiday period: {next_holiday['start']} to {next_holiday['end']}"
        )

    return "✅ 目前沒有低於安全庫存的品項"


def format_need_supplements(parsed_stock, safety_stock):
    results = []

    for project in ["ZN", "CFXD", "COMMON"]:
        need_items = []
        for item_name, safety_qty in safety_stock.get(project, {}).items():
            current_qty = parsed_stock.get(project, {}).get(item_name)
            if current_qty is None:
                continue
            if current_qty < safety_qty:
                need_qty = safety_qty - current_qty
                need_items.append(f"- {item_name} x {need_qty}")

        if need_items:
            results.append(f"{project}\n" + "\n".join(need_items))

    if results:
        return "Need supplements\n\n" + "\n\n".join(results)

    return None


def format_full_stock_summary(parsed_stock):
    results = []

    for project in ["ZN", "CFXD", "COMMON"]:
        items = parsed_stock.get(project, {})
        if not items:
            continue

        lines = [f"- {item}: {qty}" for item, qty in items.items()]
        results.append(f"{project}\n" + "\n".join(lines))

    if results:
        return "📦 目前庫存整理\n\n" + "\n\n".join(results)

    return "目前沒有可用的庫存資料"


def load_holidays():
    return load_json_file(HOLIDAYS_FILE, default=[])


from datetime import datetime, timedelta, date
import taiwan_holidays


def get_next_long_holiday_info(today_date):
    print("today_date =", today_date)

    try:
        tw_holidays = taiwan_holidays.TaiwanHolidays()

        max_days = 370
        holiday_blocks = []
        current_block = []

        for i in range(max_days):
            d = today_date + timedelta(days=i)

            if d in tw_holidays:  # ✅ 正確判斷方式
                current_block.append(d)
            else:
                if len(current_block) >= 3:
                    holiday_blocks.append(current_block)
                current_block = []

        # 最後一段也要檢查
        if len(current_block) >= 3:
            holiday_blocks.append(current_block)

        print("holiday_blocks =", holiday_blocks)

        if holiday_blocks:
            next_block = holiday_blocks[0]
            start_date = next_block[0]
            end_date = next_block[-1]
            days_remaining = (start_date - today_date).days

            result = {
                "name": "Next DGPA long holiday",
                "start": start_date.isoformat(),
                "end": end_date.isoformat(),
                "days_remaining": days_remaining
            }

            print("next_holiday_result =", result)
            return result

    except Exception as e:
        print("taiwan-holidays failed, fallback to local JSON:", str(e))

    # 🔻 fallback（你原本的 JSON）
    holidays = load_holidays()
    print("fallback holidays =", holidays)

    future_holidays = []
    for holiday in holidays:
        start_date = datetime.strptime(holiday["start"], "%Y-%m-%d").date()
        end_date = datetime.strptime(holiday["end"], "%Y-%m-%d").date()
        duration = (end_date - start_date).days + 1

        if duration >= 3 and start_date >= today_date:
            future_holidays.append({
                "name": holiday["name"],
                "start": holiday["start"],
                "end": holiday["end"],
                "days_remaining": (start_date - today_date).days
            })

    print("future_holidays =", future_holidays)

    if future_holidays:
        future_holidays.sort(key=lambda x: x["start"])
        print("fallback next holiday =", future_holidays[0])
        return future_holidays[0]

    print("no holiday found")
    return None

@app.route("/", methods=["GET"])
def home():
    return "LINE spare bot is running."


@app.route("/cron/holiday-reminder", methods=["GET"])
def holiday_reminder():
    key = request.args.get("key", "")
    if not CRON_SECRET or key != CRON_SECRET:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    if not TARGET_LINE_USER_ID:
        return jsonify({"ok": False, "error": "TARGET_LINE_USER_ID not set"}), 400

    holiday = is_two_days_before_holiday(date.today())
    if not holiday:
        return jsonify({"ok": True, "message": "not reminder day"}), 200

    latest_stock = load_json_file(LATEST_STOCK_FILE, default={})
    if not latest_stock:
        return jsonify({"ok": False, "error": "no latest stock data"}), 400

    message = (
        f"📦 {holiday['name']} 前兩天庫存提醒\n\n"
        + format_full_stock_summary(latest_stock)
    )
    push_message(TARGET_LINE_USER_ID, message)

    return jsonify({"ok": True, "message": "holiday reminder sent"}), 200


@app.route("/callback", methods=["POST"])
def callback():
    try:
        signature = request.headers.get("X-Line-Signature", "")
        body = request.get_data()

        print("Incoming callback body:", body.decode("utf-8", errors="ignore"))

        if not verify_signature(body, signature):
            abort(400, "Invalid signature")

        data = request.get_json()
        safety_stock = load_json_file(SAFETY_FILE, default={})

        for event in data.get("events", []):
            if event.get("type") != "message":
                continue
            if event["message"].get("type") != "text":
                continue

            user_text = event["message"]["text"].strip()
            reply_token = event["replyToken"]
            user_id = event.get("source", {}).get("userId", "")

            print("User text:", user_text)

            if user_text == "檢查":
                latest_stock = load_json_file(LATEST_STOCK_FILE, default={})
                if not latest_stock:
                    reply_message(reply_token, "目前還沒有最新庫存資料，請先把每日庫存貼給我。")
                    continue

                result = format_low_stock(latest_stock, safety_stock)
                print("Check result:", result)
                reply_message(reply_token, result)

                need_supplements_result = format_need_supplements(latest_stock, safety_stock)
                if need_supplements_result and user_id:
                    print("Need supplements result:", need_supplements_result)
                    push_message(user_id, need_supplements_result)
                continue

            if user_text == "庫存":
                latest_stock = load_json_file(LATEST_STOCK_FILE, default={})
                if not latest_stock:
                    reply_message(reply_token, "目前還沒有最新庫存資料。")
                    continue

                result = format_full_stock_summary(latest_stock)
                print("Stock result:", result)
                reply_message(reply_token, result)
                continue

            if user_text == "連假檢查":
                latest_stock = load_json_file(LATEST_STOCK_FILE, default={})
                if not latest_stock:
                    reply_message(reply_token, "目前還沒有最新庫存資料。")
                    continue

                result = "📦 連假前庫存確認\n\n" + format_full_stock_summary(latest_stock)
                print("Holiday check result:", result)
                reply_message(reply_token, result)
                continue

            parsed_stock = parse_report_to_stock(user_text, safety_stock)
            print("Parsed stock:", json.dumps(parsed_stock, ensure_ascii=False))

            has_any_data = any(parsed_stock.get(section) for section in ["ZN", "CFXD", "COMMON"])
            if not has_any_data:
                reply_message(
                    reply_token,
                    "無法辨識庫存資料。\n請直接貼每日庫存內容，或輸入：檢查 / 庫存 / 連假檢查"
                )
                continue

            save_json_file(LATEST_STOCK_FILE, parsed_stock)

            low_stock_result = format_low_stock(parsed_stock, safety_stock)
            print("Low stock result:", low_stock_result)
            reply_message(reply_token, low_stock_result)

            need_supplements_result = format_need_supplements(parsed_stock, safety_stock)
            if need_supplements_result and user_id:
                print("Need supplements result:", need_supplements_result)
                push_message(user_id, need_supplements_result)

        return "OK"

    except Exception as e:
        print("ERROR in callback:", str(e))
        return "Internal Server Error", 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)