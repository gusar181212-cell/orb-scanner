#!/usr/bin/env python3
"""
Zone Watcher — мониторинг торговых зон каждые 5 минут.
Запускается через GitHub Actions. Полная сеть, без прокси.
"""

import os
import sys
import requests
from datetime import datetime, timezone

TOKEN   = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# ── Активные сетапы ────────────────────────────────────────────────────────────
# Добавляй/удаляй сюда. direction: "LONG" или "SHORT"
SETUPS = [
    {
        "symbol":    "ONDOUSDT",
        "direction": "LONG",
        "zone_low":  0.3975,
        "zone_high": 0.4020,
        "desc":      "Свип 0.4009 | держим вход LONG от 0.4040 | SL 0.3975 | TP1 0.4103 | TP2 0.4167",
    },
]

def get_okx(symbol):
    """Возвращает (current_price, prev_15m_close) или (None, None) при ошибке."""
    inst = symbol.replace("USDT", "") + "-USDT-SWAP"
    try:
        ticker = requests.get(
            f"https://www.okx.com/api/v5/market/ticker?instId={inst}",
            timeout=10
        ).json()
        price = float(ticker["data"][0]["last"])

        candles = requests.get(
            f"https://www.okx.com/api/v5/market/candles?instId={inst}&bar=15m&limit=2",
            timeout=10
        ).json()
        prev_close = float(candles["data"][1][4])

        return price, prev_close

    except Exception as e:
        print(f"  WARNING OKX {symbol}: {e}")
        return None, None


def send_tg(text):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        r.raise_for_status()
    except Exception as e:
        print(f"  WARNING Telegram: {e}")


def main():
    now = datetime.now(timezone.utc).strftime("%d.%m %H:%M UTC")
    alerts = []

    for s in SETUPS:
        price, prev = get_okx(s["symbol"])
        if price is None:
            continue

        zl, zh = s["zone_low"], s["zone_high"]
        in_zone  = zl <= price <= zh
        was_in   = zl <= prev  <= zh

        status = "IN ZONE" if in_zone else f"price {price:.5f}"
        print(f"  {s['symbol']} {s['direction']}: {status} | prev15m={prev:.5f}")

        if in_zone and not was_in:
            send_tg(
                f"🟠 <b>ЗОНА!</b> {s['symbol']} {s['direction']}\n"
                f"Цена: <b>{price:.5f}</b> | Зона: {zl}–{zh}\n"
                f"📋 {s['desc']}\n"
                f"→ Открой TV, жди 15M слабость → 1M триггер!\n"
                f"⏰ {now}"
            )
            alerts.append(f"{s['symbol']} вошёл в зону")

        elif s["direction"] == "SHORT" and was_in and price < zl:
            send_tg(
                f"⚡ <b>ВХОД SHORT?</b> {s['symbol']}\n"
                f"Цена: <b>{price:.5f}</b> пробила зону вниз ({zl})\n"
                f"→ Проверь 1M close ниже {zl}\n"
                f"⏰ {now}"
            )
            alerts.append(f"{s['symbol']} SHORT пробой")

        elif s["direction"] == "LONG" and was_in and price > zh:
            send_tg(
                f"⚡ <b>ВХОД LONG?</b> {s['symbol']}\n"
                f"Цена: <b>{price:.5f}</b> выше зоны ({zh})\n"
                f"→ Проверь 1M close выше {zh}\n"
                f"⏰ {now}"
            )
            alerts.append(f"{s['symbol']} LONG отскок")

        elif s["direction"] == "LONG" and not in_zone and price < zl and prev >= zl:
            send_tg(
                f"🔴 <b>ВНИМАНИЕ SL!</b> {s['symbol']}\n"
                f"Цена: <b>{price:.5f}</b> ниже защитной зоны {zl}\n"
                f"→ Проверь позицию!\n"
                f"⏰ {now}"
            )
            alerts.append(f"{s['symbol']} SL ZONE")

    if alerts:
        print(f"ALERTS: {', '.join(alerts)}")
    else:
        print(f"OK - no events ({now})")


if __name__ == "__main__":
    main()
