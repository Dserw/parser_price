import os
import re
import json
import time
import random
from urllib.parse import urlparse, parse_qs
from curl_cffi import requests as cffi_requests
import requests

GAS_URL = os.environ["GAS_WEBAPP_URL"]
PROXY_URL = os.environ.get("PROXY_URL", "").strip()

PROXIES = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None


def clean_price(val):
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return int(round(val))
    digits = re.sub(r"[^\d]", "", str(val))
    return int(digits) if digits else None


def create_session():
    session = cffi_requests.Session(impersonate="chrome131")
    if PROXIES:
        session.proxies = PROXIES
    session.headers.update({
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    return session


def parse_ozon(session, url: str, debug=False):
    """
    Парсит Ozon через внутренний JSON-эндпоинт витрины (webPrice).
    Возвращает (цена_без_карты, цена_с_Ozon_картой).
    """
    if not url or "ozon.ru" not in url:
        return None, None

    try:
        parsed = urlparse(url)
        path = parsed.path
        api_url = f"https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2?url={path}"

        resp = session.get(
            api_url,
            headers={
                "Accept": "application/json",
                "x-o3-app-name": "dweb_client",
                "x-o3-app-version": "release_1-1-2025",
                "Referer": url,
            },
            timeout=20,
        )

        if debug:
            print(f"  [DEBUG Ozon] HTTP {resp.status_code} | len={len(resp.text)}")

        if resp.status_code != 200:
            return None, None

        data = resp.json()
        widget_states = data.get("widgetStates", {})

        for key, raw_json in widget_states.items():
            if key.startswith("webPrice"):
                price_data = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
                # cardPrice — цена по Ozon Карте, price — обычная цена без карты
                with_card = clean_price(price_data.get("cardPrice"))
                no_card = clean_price(price_data.get("price"))

                if no_card or with_card:
                    if not no_card:
                        no_card = with_card
                    if not with_card:
                        with_card = no_card
                    return no_card, with_card

        # Резервный поиск цен в полном JSON ответа
        text = resp.text
        card_m = re.search(r'"cardPrice"\s*:\s*"([^"]+)"', text)
        price_m = re.search(r'"price"\s*:\s*"([\d\s\u00a0]+₽)"', text)
        if card_m or price_m:
            with_card = clean_price(card_m.group(1)) if card_m else None
            no_card = clean_price(price_m.group(1)) if price_m else with_card
            if no_card:
                return no_card, (with_card or no_card)

        return None, None
    except Exception as e:
        if debug:
            print(f"  [DEBUG Ozon Error] {e}")
        return None, None


def parse_ym(session, url: str, debug=False):
    """
    Парсит Яндекс Маркет из JSON-состояния карточки товара.
    Возвращает (цена_без_Пэй, цена_с_Яндекс_Пэй).
    """
    if not url or "market.yandex.ru" not in url:
        return None, None

    try:
        resp = session.get(url, timeout=20)
        html = resp.text

        if debug:
            is_captcha = "SmartCaptcha" in html or "showcaptcha" in resp.url
            print(f"  [DEBUG YM] HTTP {resp.status_code} | captcha={is_captcha} | len={len(html)}")

        if resp.status_code != 200 or "showcaptcha" in resp.url:
            return None, None

        # Ищем цены в встроенном JSON-состоянии страницы Маркета
        # 1. Цена с Яндекс Пэй (yaPayPrice / greenPrice / fintechPrice)
        pay_patterns = [
            r'"yaPayPrice"\s*:\s*\{\s*"value"\s*:\s*(\d+)',
            r'"greenPrice"\s*:\s*\{\s*"value"\s*:\s*(\d+)',
            r'"paymentSystemPrice"\s*:\s*\{\s*"value"\s*:\s*(\d+)',
            r'([\d\s\u00a0\u2009]+)\s*₽\s*(?:с\s*Пэй|с\s*картой\s*Пэй)',
        ]
        with_pay = None
        for pat in pay_patterns:
            m = re.search(pat, html)
            if m:
                val = clean_price(m.group(1))
                if val and val > 10:
                    with_pay = val
                    break

        # 2. Основная цена без Пэй ("price":{"value":1234,"currency":"RUR"})
        price_matches = re.findall(r'"price"\s*:\s*\{\s*"value"\s*:\s*(\d+)\s*,\s*"currency"\s*:\s*"RUR"', html)
        prices = [int(p) for p in price_matches if int(p) > 10]

        no_pay = None
        if prices:
            if with_pay:
                higher = [p for p in prices if p >= with_pay]
                no_pay = higher[0] if higher else prices[0]
            else:
                no_pay = prices[0]

        if no_pay or with_pay:
            return (no_pay or with_pay), (with_pay or no_pay)

        return None, None
    except Exception as e:
        if debug:
            print(f"  [DEBUG YM Error] {e}")
        return None, None


def send_updates(updates):
    if not updates:
        return
    try:
        # Google Apps Script при POST делает 302 редирект, requests сохраняет тело при правильной обработке
        r = requests.post(GAS_URL, json=updates, timeout=30)
        print(f"  -> Записано в таблицу: {len(updates)} строк (статус {r.status_code})")
    except Exception as e:
        print(f"  -> Ошибка отправки в Google Таблицу: {e}")


def main():
    print(f"Старт парсера. Прокси подключен: {'ДА' if PROXIES else 'НЕТ'}")
    resp = requests.get(GAS_URL, timeout=30)
    resp.raise_for_status()
    tasks = resp.json()

    if not isinstance(tasks, list):
        raise RuntimeError(f"Ошибка ответа Google Таблицы: {tasks}")

    print(f"Получено строк из таблицы: {len(tasks)}")
    session = create_session()

    # Прогреваем куки Ozon заходом на главную
    try:
        session.get("https://www.ozon.ru/", timeout=15)
    except Exception:
        pass

    updates = []
    success_ozon = 0
    success_ym = 0

    for idx, item in enumerate(tasks, 1):
        row = item["row"]
        sku = item.get("sku", "")
        ozon_url = item.get("ozonUrl", "")
        ym_url = item.get("ymUrl", "")

        if not ozon_url and not ym_url:
            continue

        debug = idx <= 5  # Подробный лог для первых 5 товаров
        if debug:
            print(f"[{idx}/{len(tasks)}] Проверка {sku}...")

        row_update = {"row": row}

        if ozon_url:
            oz_no, oz_with = parse_ozon(session, ozon_url, debug=debug)
            if oz_no:
                row_update["ozonNoCard"] = oz_no
                row_update["ozonWithCard"] = oz_with
                success_ozon += 1
                if debug:
                    print(f"  Ozon OK: без карты={oz_no}, с картой={oz_with}")

        if ym_url:
            ym_no, ym_with = parse_ym(session, ym_url, debug=debug)
            if ym_no:
                row_update["ymNoPay"] = ym_no
                row_update["ymWithPay"] = ym_with
                success_ym += 1
                if debug:
                    print(f"  YM OK: без Пэй={ym_no}, с Пэй={ym_with}")

        if len(row_update) > 1:
            updates.append(row_update)

        if len(updates) >= 20:
            send_updates(updates)
            updates = []

        time.sleep(random.uniform(0.6, 1.4))

    send_updates(updates)
    print(f"Готово! Спарсено Ozon: {success_ozon}, Яндекс Маркет: {success_ym}")


if __name__ == "__main__":
    main()
