#!/usr/bin/env python3
"""
ORB NY Scanner — JW-bear SHORT only
Запускается в 13:45 UTC, проверяет закрытый 13:30 бар по всем 47 монетам.
OKX public API (без ключей).
"""

import os
import time
import requests
from datetime import datetime, timezone

# —— Список символов ———————————————————————————————————————————
SYMBOLS = [
    "BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","ADAUSDT",
    "DOGEUSDT","LINKUSDT","AVAXUSDT","DOTUSDT","LTCUSDT",
    "BCHUSDT","ATOMUSDT","NEARUSDT","APTUSDT","SUIUSDT",
    "OPUSDT","ARBUSDT","TONUSDT","FILUSDT","INJUSDT",
    "TIAUSDT","SEIUSDT","UNIUSDT","AAVEUSDT","HBARUSDT",
    "HYPEUSDT","TRXUSDT","ZECUSDT","ETCUSDT",
    "RENDERUSDT","TAOUSDT","FETUSDT","WLDUSDT","ENAUSDT",
    "ONDOUSDT","JUPUSDT","STXUSDT","ICPUSDT","XLMUSDT",
    "KASUSDT","PENGUUSDT","VIRTUALUSDT","KAITOUSDT",
    "PEPEUSDT","WIFUSDT","BONKUSDT","TRUMPUSDT",
]

ATR_THRESHOLD = 0.15  # бар >= 15% дневного ATR(14); бэктест 19.07.26: тело 10-15% ATR = WR 8%, -9R

# —— API-запросы ———————————————————————————————————————————————

def to_okx_inst(symbol: str) -> str:
    """BTCUSDT -> BTC-USDT-SWAP"""
    base = symbol.replace("USDT", "")
    return f"{base}-USDT-SWAP"

def okx_klines(symbol: str, bar: str, limit: int) -> list:
    """
    Возвращает список [open, high, low, close] — от старого к новому.
    bar: '15m' | '1D'
    """
    inst_id = to_okx_inst(symbol)
    # Для истории дневных свечей используем history-candles
    if bar == "1D":
        url = "https://www.okx.com/api/v5/market/history-candles"
    else:
        url = "https://www.okx.com/api/v5/market/candles"
    params = {"instId": inst_id, "bar": bar, "limit": limit}
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != "0":
        raise ValueError(f"OKX error: {data.get('msg')}")
    rows = data["data"][::-1]  # OKX отдаёт новейший первым — разворачиваем
    # [ts, open, high, low, close, vol, volCcy, volCcyQuote, confirm]
    return [[float(row[1]), float(row[2]), float(row[3]), float(row[4])] for row in rows]

def get_15m(symbol: str) -> list:
    """Последние 6 свечей 15M [open, high, low, close]"""
    return okx_klines(symbol, "15m", 6)

def get_daily(symbol: str) -> list:
    """Последние 15 дневных свечей [open, high, low, close]"""
    return okx_klines(symbol, "1D", 15)

# —— Расчёты ———————————————————————————————————————————————————

def calc_atr14(daily: list) -> float:
    """ATR(14) по дневным данным"""
    trs = []
    for i in range(1, len(daily)):
        h, l, pc = daily[i][1], daily[i][2], daily[i-1][3]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if not trs:
        return 0.0
    return sum(trs[-14:]) / min(len(trs), 14)

def is_jw_bear(candles: list) -> bool:
    """
    JW-bear паттерн: последние 3 свечи перед ORB-баром.
    Свеча = (open, high, low, close)
    Условие: 3 медвежьих свечи подряд (close < open)
    """
    if len(candles) < 3:
        return False
    for c in candles[-3:]:
        if c[3] >= c[0]:  # close >= open => бычья
            return False
    return True

def find_orb_bar(candles_15m: list) -> dict | None:
    """
    ORB-бар = предпоследняя свеча (индекс -2).
    Возвращает dict с ключами или None.
    """
    if len(candles_15m) < 2:
        return None
    bar = candles_15m[-2]  # закрытый бар 13:30
    o, h, l, c = bar
    return {"open": o, "high": h, "low": l, "close": c, "body": abs(c - o)}

# —— Основной скан ——————————————————————————————————————————————

def scan() -> list:
    now = datetime.now(timezone.utc)
    print(f"[{now.strftime('%Y-%m-%d %H:%M UTC')}] ORB Scanner запущен — {len(SYMBOLS)} монет")

    signals = []
    for sym in SYMBOLS:
        try:
            candles_15m = get_15m(sym)
            daily = get_daily(sym)
        except Exception as e:
            print(f"❌ {sym}: {e}")
            continue

        orb = find_orb_bar(candles_15m)
        if orb is None:
            continue

        atr = calc_atr14(daily)
        if atr == 0:
            continue

        # Фильтр 1: ATR — бар достаточно большой
        if orb["body"] < ATR_THRESHOLD * atr:
            continue

        # Фильтр 2: JW-bear — 3 медвежьих свечи до ORB-бара
        pre_bars = candles_15m[:-2]  # свечи до ORB-бара
        if not is_jw_bear(pre_bars):
            continue

        # Фильтр 3: ORB-бар медвежий
        if orb["close"] >= orb["open"]:
            continue

        signals.append({
            "symbol": sym,
            "orb_open": orb["open"],
            "orb_high": orb["high"],
            "orb_low": orb["low"],
            "orb_close": orb["close"],
            "atr": round(atr, 4),
            "body_pct": round(orb["body"] / atr * 100, 1),
        })
        print(f"✅ СИГНАЛ {sym}: O={orb['open']} H={orb['high']} L={orb['low']} C={orb['close']} | тело={round(orb['body']/atr*100,1)}% ATR")

    return signals

# —— Telegram ——————————————————————————————————————————————————

def send_telegram(signals: list):
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not bot_token or not chat_id:
        print("⚠️ TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не заданы")
        return

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if not signals:
        text = f"🔍 ORB NY Scanner [{now}]\n\nСигналов нет — рынок не подходит для входа."
    else:
        lines = [f"🚨 ORB NY Scanner [{now}] — {len(signals)} сигнал(ов) SHORT:\n"]
        for s in signals:
            lines.append(
                f"📉 <b>{s['symbol']}</b>\n"
                f"   O={s['orb_open']} H={s['orb_high']} L={s['orb_low']} C={s['orb_close']}\n"
                f"   Тело={s['body_pct']}% ATR | ATR={s['atr']}\n"
                f"   Вход: пробой {s['orb_low']} | SL: {s['orb_high']}"
            )
        text = "\n".join(lines)

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    r = requests.post(url, json=payload, timeout=10)
    if r.ok:
        print(f"✅ Telegram: отправлено ({len(signals)} сигнал(ов))")
    else:
        print(f"❌ Telegram error: {r.text}")

# —— Точка входа ————————————————————————————————————————————————

if __name__ == "__main__":
    signals = scan()
    send_telegram(signals)
