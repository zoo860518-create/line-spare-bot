import os
import json
import hmac
import hashlib
import base64
import requests
from flask import Flask, request, abort

app = Flask(__name__)

CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

INVENTORY_FILE = "inventory.json"


def load_inventory():
    with open(INVENTORY_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def check_low_stock(data):
    results = []

    for project, items in data.items():
        low_items = []
        for item_name, item_data in items.items():
            stock = item_data.get("stock", 0)
            safety = item_data.get("safety", 0)
            if stock < safety:
                low_items.append(f"- {item_name}: {stock} / {safety}")

        if low_items:
            results.append(f"{project}\n" + "\n".join(low_items))

    if results:
        return "⚠️ Low stock alert\n\n" + "\n\n".join(results)
    return "✅ 目前沒有低於安全庫存的品項"


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
    requests.post(url, headers=headers, json=payload, timeout=10)


@app.route("/", methods=["GET"])
def home():
    return "LINE spare bot is running."


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data()

    if not verify_signature(body, signature):
        abort(400, "Invalid signature")

    data = request.get_json()

    for event in data.get("events", []):
        if event.get("type") != "message":
            continue
        if event["message"].get("type") != "text":
            continue

        user_text = event["message"]["text"].strip()
        reply_token = event["replyToken"]

        if user_text == "檢查":
            inventory = load_inventory()
            result = check_low_stock(inventory)
            reply_message(reply_token, result)
        else:
            reply_message(reply_token, "請輸入：檢查")

    return "OK"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)