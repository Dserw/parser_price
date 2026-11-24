import os
import re
import json
import time
import random
import requests
from playwright.sync_api import sync_playwright
from playwright_stealth import stealth_sync

GAS_URL = os.environ["GAS_WEBAPP_URL"]


def clean_price(text: str):
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def parse_ozon(page, url: str):
    """Извлекает цену с Ozon Картой и без неё"""
    if not url or "ozon.ru" not in url:
        return None, None
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=35000)
        page.wait_for_timeout(random.randint(2500, 4000))

        # Ищем блок цен Ozon (webPrice)
        price_block = page.locator('[data-widget="webPrice"]').first
        if price_block.count() == 0:
            return None, None

        raw_text = price_block.inner_text()
        # Находим все числа перед символом ₽
        prices = [clean_price(m) for m in re.findall(r"([\d\s\u00a0\u2009]+)\s*₽", raw_text)]
        prices = [p for p in prices if p and p > 10]

        if not prices:
            return None, None

        # На Ozon самая низкая цена в блоке — с Ozon Картой, следующая за ней — без Карты
        unique_sorted = sorted(set(prices))
        if len(unique_sorted) >= 2:
            with_card = unique_sorted[0]
            no_card = unique_sorted[1]
            return no_card, with_card
        else:
            return unique_sorted[0], unique_sorted[0]
    except Exception as e:
        print(f"[Ozon error] {url}: {e}")
        return None, None


def parse_ym(page, url: str):
    """Извлекает цену с Яндекс Пэй и обычную цену"""
    if not url or "market.yandex.ru" not in url:
        return None, None
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=35000)
        page.wait_for_timeout(random.randint(2500, 4000))

        body_text = page.locator("body").inner_text()

        # Ищем цену с Яндекс Пэй
        pay_match = re.search(r"([\d\s\u00a0\u2009]+)\s*₽\s*(?:с\s*Пэй|с\s*картой\s*Пэй)", body_text, re.IGNORECASE)
        with_pay = clean_price(pay_match.group(1)) if pay_match else None

        # Собираем цены из основного ценового блока карточки
        price_elements = page.locator('[data-auto="price-value"], [data-zone-name="price"] span').all_inner_texts()
        prices = []
        for el in price_elements:
            for m in re.findall(r"([\d\s\u00a0\u2009]+)\s*₽", el):
                val = clean_price(m)
                if val and val > 10:
                    prices.append(val)

        unique_sorted = sorted(set(prices))
        if with_pay:
            # Берём ближайшую цену выше цены с Пэй как обычную цену
            higher = [p for p in unique_sorted if p > with_pay]
            no_pay = higher[0] if higher else with_pay
            return no_pay, with_pay

        if len(unique_sorted) >= 2:
            return unique_sorted[1], unique_sorted[0]
        elif len(unique_sorted) == 1:
            return unique_sorted[0], unique_sorted[0]
        return None, None
    except Exception as e:
        print(f"[YM error] {url}: {e}")
        return None, None


def main():
    print("Загружаем список товаров из Google Таблицы...")
    resp = requests.get(GAS_URL, timeout=30)
    resp.raise_for_status()
    tasks = resp.json()
    print(f"Получено строк: {len(tasks)}")

    updates = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 768},
            locale="ru-RU",
            timezone_id="Europe/Moscow",
        )
        page = context.new_page()
        stealth_sync(page)

        for idx, item in enumerate(tasks, 1):
            row = item["row"]
            sku = item.get("sku", "")
            ozon_url = item.get("ozonUrl", "")
            ym_url = item.get("ymUrl", "")

            if not ozon_url and not ym_url:
                continue

            print(f"[{idx}/{len(tasks)}] Обработка {sku}...")
            row_update = {"row": row}

            if ozon_url:
                oz_no, oz_with = parse_ozon(page, ozon_url)
                if oz_no:
                    row_update["ozonNoCard"] = oz_no
                    row_update["ozonWithCard"] = oz_with

            if ym_url:
                ym_no, ym_with = parse_ym(page, ym_url)
                if ym_no:
                    row_update["ymNoPay"] = ym_no
                    row_update["ymWithPay"] = ym_with

            if len(row_update) > 1:
                updates.append(row_update)

            # Отправляем батчами по 15 товаров, чтобы данные появлялись в таблице постепенно
            if len(updates) >= 15:
                requests.post(GAS_URL, json=updates, timeout=30)
                print(f"Отправлено {len(updates)} обновлений в таблицу.")
                updates = []

            time.sleep(random.uniform(1.0, 2.5))

        if updates:
            requests.post(GAS_URL, json=updates, timeout=30)
            print(f"Отправлены финальные {len(updates)} обновлений.")

        browser.close()


if __name__ == "__main__":
    main()
