"""
Симулятор веб-оплаты Bozorlik AI Pro — прогоняет ПОЛНЫЙ цикл оплаты локально,
изображая серверы Payme и Click (без реальных денег и без их песочниц).

Что делает:
  1. Создаёт заказ через /api/pro/{user}/checkout (payme и click).
  2. Payme: шлёт CheckPerformTransaction → CreateTransaction → PerformTransaction
     на /api/payments/payme с Basic-авторизацией Paycom:{PAYME_TEST_KEY}.
  3. Click: шлёт prepare → complete на /api/payments/click/* с настоящей md5-подписью.
  4. Проверяет, что подписка пользователя стала plan=paid, и печатает результат.

Подготовка (.env — значения для локального теста могут быть любыми):
    PAYME_MERCHANT_ID=local_test_kassa
    PAYME_TEST_KEY=local_test_key
    CLICK_SERVICE_ID=777
    CLICK_MERCHANT_ID=888
    CLICK_SECRET_KEY=local_click_secret

Запуск:
    python run_local.py            # в одном терминале
    python simulate_payments.py    # в другом
"""

import base64
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from dotenv import load_dotenv

load_dotenv()

BACKEND = os.getenv("BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
USER_ID = int(os.getenv("SIM_USER_ID", "990077"))
PAYME_KEY = os.getenv("PAYME_TEST_KEY") or os.getenv("PAYME_KEY", "")
CLICK_SERVICE_ID = os.getenv("CLICK_SERVICE_ID", "")
CLICK_SECRET_KEY = os.getenv("CLICK_SECRET_KEY", "")


def http(method: str, path: str, json_body=None, form_body=None, headers=None):
    url = BACKEND + path
    headers = dict(headers or {})
    if json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    elif form_body is not None:
        data = urllib.parse.urlencode(form_body).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    else:
        data = b""
    req = urllib.request.Request(url, data, headers, method=method)
    try:
        return json.loads(urllib.request.urlopen(req).read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())


def check(name: str, ok: bool, detail=""):
    print(f"  {'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        sys.exit(1)


def payme_call(method: str, params: dict, rpc_id: int = 1):
    auth = base64.b64encode(f"Paycom:{PAYME_KEY}".encode()).decode()
    return http("POST", "/api/payments/payme",
                json_body={"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params},
                headers={"Authorization": f"Basic {auth}"})


def simulate_payme():
    print("\n— Payme Merchant API —")
    d = http("POST", f"/api/pro/{USER_ID}/checkout", json_body={"provider": "payme"})
    check("checkout: заказ создан", d.get("success"), d.get("error", ""))
    order_id, amount_tiyin = d["order_id"], d["amount"] * 100
    print(f"     заказ #{order_id}, {d['amount']} сум, ссылка: {d['checkout_url'][:60]}…")

    r = payme_call("CheckPerformTransaction", {"amount": amount_tiyin, "account": {"order_id": order_id}})
    check("CheckPerformTransaction → allow", r.get("result", {}).get("allow") is True, str(r.get("error", "")))

    txn = f"sim_{int(time.time() * 1000)}"
    r = payme_call("CreateTransaction", {"id": txn, "time": int(time.time() * 1000),
                                         "amount": amount_tiyin, "account": {"order_id": order_id}})
    check("CreateTransaction → state 1", r.get("result", {}).get("state") == 1, str(r.get("error", "")))

    r = payme_call("PerformTransaction", {"id": txn})
    check("PerformTransaction → state 2", r.get("result", {}).get("state") == 2, str(r.get("error", "")))

    # неправильная сумма должна отклоняться
    r = payme_call("CheckPerformTransaction", {"amount": 1, "account": {"order_id": order_id}})
    check("повторная оплата отклонена", "error" in r, "")


def simulate_click():
    print("\n— Click SHOP API —")
    d = http("POST", f"/api/pro/{USER_ID}/checkout", json_body={"provider": "click"})
    check("checkout: заказ создан", d.get("success"), d.get("error", ""))
    order_id = d["order_id"]
    amount = f"{d['amount']}.0"
    sign_time = time.strftime("%Y-%m-%d %H:%M:%S")
    click_trans = str(int(time.time()))

    def sign(action, prepare_id=""):
        base = click_trans + CLICK_SERVICE_ID + CLICK_SECRET_KEY + str(order_id)
        if action == "1":
            base += str(prepare_id)
        return hashlib.md5((base + amount + action + sign_time).encode()).hexdigest()

    r = http("POST", "/api/payments/click/prepare", form_body={
        "click_trans_id": click_trans, "service_id": CLICK_SERVICE_ID, "click_paydoc_id": "1",
        "merchant_trans_id": order_id, "amount": amount, "action": "0", "error": "0",
        "error_note": "Success", "sign_time": sign_time, "sign_string": sign("0")})
    check("prepare → error 0", r.get("error") == 0, r.get("error_note", ""))
    prepare_id = r["merchant_prepare_id"]

    r = http("POST", "/api/payments/click/complete", form_body={
        "click_trans_id": click_trans, "service_id": CLICK_SERVICE_ID, "click_paydoc_id": "1",
        "merchant_trans_id": order_id, "merchant_prepare_id": prepare_id, "amount": amount,
        "action": "1", "error": "0", "error_note": "Success",
        "sign_time": sign_time, "sign_string": sign("1", prepare_id)})
    check("complete → error 0", r.get("error") == 0, r.get("error_note", ""))

    # подделанная подпись должна отклоняться
    r = http("POST", "/api/payments/click/prepare", form_body={
        "click_trans_id": click_trans, "service_id": CLICK_SERVICE_ID, "click_paydoc_id": "1",
        "merchant_trans_id": order_id, "amount": amount, "action": "0", "error": "0",
        "error_note": "x", "sign_time": sign_time, "sign_string": "bad_signature"})
    check("подделанная подпись отклонена", r.get("error") == -1, "")


def main():
    if not PAYME_KEY or not CLICK_SECRET_KEY or not CLICK_SERVICE_ID:
        sys.exit("Заполните PAYME_TEST_KEY, CLICK_SERVICE_ID и CLICK_SECRET_KEY в .env (см. шапку файла)")

    # сбрасываем подписку тестового пользователя, чтобы проверить активацию честно
    http("POST", f"/api/pro/{USER_ID}", json_body={"is_pro": False})

    simulate_payme()
    status = http("GET", f"/api/pro/{USER_ID}")
    check("после Payme: подписка активна (plan=paid)", status.get("plan") == "paid",
          f"plan={status.get('plan')}, до {str(status.get('paid_until'))[:10]}")

    http("POST", f"/api/pro/{USER_ID}", json_body={"is_pro": False})
    simulate_click()
    status = http("GET", f"/api/pro/{USER_ID}")
    check("после Click: подписка активна (plan=paid)", status.get("plan") == "paid",
          f"plan={status.get('plan')}, до {str(status.get('paid_until'))[:10]}")

    print("\n🧡 Всё работает: оба протокола проводят оплату и активируют Pro.")


if __name__ == "__main__":
    main()
