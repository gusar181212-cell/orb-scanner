#!/usr/bin/env python3
"""
Zone Watcher — мониторинг торговых зон каждые 5 минут.
Запускается через GitHub Actions. Полная сеть, без прокси.

Логика жизненного цикла сетапа:
- Добавь сетап в SETUPS вручную после сигнала
- Вотчер сам удалит его если: SL пробит, TP достигнут или истёк срок (expires_date)
- Если SETUPS пуст — вотчер молчит и ничего не шлёт
"""

import os
import json
import requests
from datetime import datetime, timezone

TOKEN   = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# ── Активные сетапы ────────────────────────────────────────────────────────────
# Формат: symbol, direction, zone_low, zone_high, sl, tp1, tp2, desc
# expires_date: дата в формате "YYYY-MM-DD" — после неё сетап авто-удаляется
# Очистить все → оставить пустой список []
SETUPS = [
    # Пример (раскомментировать когда будет новый сигнал):
    # {
    #     "symbol":       "BTCUSDT",
    #     "direction":    "LONG",
    #     "zone_low":     95000,
    #     "zone_high":    96000,
    #     "sl":           94500,
    #     "tp1":          98000,
    #     "tp2":          100000,
    #     "expires_date": "2026-08-15",
    #     "desc": "OB 4H | DC Aug 5 | SL 94500 | TP1 98000 | TP2 100000",
    # },
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
    now = datetime.now(timezone.utc)
    now_str = now.strftime("%d.%m %H:%M UTC")

    if not SETUPS:
        print(f"SETUPS пуст — нечего мониторить ({now_str})")
        return

    alerts = []

    for s in SETUPS:
        # Проверка срока жизни сетапа
        if "expires_date" in s:
            exp = datetime.fromisoformat(s["expires_date"]).replace(tzinfo=timezone.utc)
            if now > exp:
                print(f"  {s['symbol']} — истёк срок ({s['expires_date']}), пропускаем")
                send_tg(
                    f"⏰ <b>СЕТАП ИСТЁК</b> {s['symbol']} {s['direction']}\n"
                    f"Срок вышел {s['expires_date']} — удали из SETUPS\n"
                    f"⏰ {now_str}"
                )
                continue

        price, prev = get_okx(s["symbol"])
        if price is None:
            continue

        zl, zh = s["zone_low"], s["zone_high"]
        sl = s.get("sl", zl)
        tp1 = s.get("tp1")
        tp2 = s.get("tp2")

        in_zone  = zl <= price <= zh
        was_in   = zl <= prev  <= zh

        status = "IN ZONE ✅" if in_zone else f"price {price:.5f}"
        print(f"  {s['symbol']} {s['direction']}: {status} | prev15m={prev:.5f}")

        # ── Цена зашла в зону ──────────────────────────────────────────────────
        if in_zone and not was_in:
            send_tg(
                f"🟠 <b>ЗОНА!</b> {s['symbol']} {s['direction']}\n"
                f"Цена: <b>{price:.5f}</b> | Зона: {zl}–{zh}\n"
                f"📋 {s['desc']}\n"
                f"→ Открой TV, жди 15M слабость → 1M триггер!\n"
                f"⏰ {now_str}"
            )
            alerts.append(f"{s['symbol']} вошёл в зону")

        # ── LONG: отскок выше зоны (вход) ─────────────────────────────────────
        elif s["direction"] == "LONG" and was_in and price > zh:
            send_tg(
                f"⚡ <b>ВХОД LONG?</b> {s['symbol']}\n"
                f"Цена: <b>{price:.5f}</b> вышла выше зоны ({zh})\n"
                f"→ Проверь 1M close выше {zh}\n"
                f"⏰ {now_str}"
            )
            alerts.append(f"{s['symbol']} LONG пробой вверх")

        # ── LONG: SL пробит ────────────────────────────────────────────────────
        elif s["direction"] == "LONG" and price < sl and prev >= sl:
            send_tg(
                f"🔴 <b>SL ПРОБИТ!</b> {s['symbol']} LONG\n"
                f"Цена: <b>{price:.5f}</b> ниже SL {sl}\n"
                f"→ Закрой позицию! Удали сетап из SETUPS.\n"
                f"⏰ {now_str}"
            )
            alerts.append(f"{s['symbol']} SL HIT")

        # ── Достигнут TP1 ──────────────────────────────────────────────────────
        if tp1 and s["direction"] == "LONG" and price >= tp1 and prev < tp1:
            send_tg(
                f"✅ <b>TP1 ДОСТИГНУТ!</b> {s['symbol']}\n"
                f"Цена: <b>{price:.5f}</b> ≥ TP1 {tp1}\n"
                f"→ Зафиксируй 35–50%, двигай SL в б/у!\n"
                f"⏰ {now_str}"
            )
            alerts.append(f"{s['symbol']} TP1 HIT")

        # ── Достигнут TP2 ──────────────────────────────────────────────────────
        if tp2 and s["direction"] == "LONG" and price >= tp2 and prev < tp2:
            send_tg(
                f"🏆 <b>TP2 ДОСТИГНУТ!</b> {s['symbol']}\n"
                f"Цена: <b>{price:.5f}</b> ≥ TP2 {tp2}\n"
                f"→ Закрой остаток! Сетап выполнен, удали из SETUPS.\n"
                f"⏰ {now_str}"
            )
            alerts.append(f"{s['symbol']} TP2 HIT")

    if alerts:
        print(f"ALERTS: {', '.join(alerts)}")
    else:
        print(f"OK - no events ({now_str})")


if __name__ == "__main__":
    main()
