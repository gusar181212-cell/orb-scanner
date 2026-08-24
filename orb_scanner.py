#!/usr/bin/env python3
"""
ORB NY Scanner — JW-bear SHORT only
v01 (24.08.2026): ORB-бар выбирается ПО МЕТКЕ ВРЕМЕНИ, а не по индексу.

Причина правки: cron GitHub Actions стабильно опаздывает на 13-34 минуты
(#37 +34, #38 +32, #39 +13, #40 +16). При старте позже 14:00 UTC последний
закрытый 15M-бар — это уже 13:45, а не 13:30, и прежняя строка
    bar = candles_15m[-2]
молча анализировала не ту свечу. Теперь бар ищется по timestamp открытия
NYSE (09:30 ET), поэтому задержка запуска перестала что-либо значить.

Дополнительно:
  * переход на зимнее/летнее время считается автоматически через zoneinfo
    (правило «в ноябре поменять cron» больше не нужно);
  * учитывается флаг confirm OKX — незакрытый бар не анализируется;
  * в лог и в Telegram пишется, какая именно свеча взята;
  * ошибки больше не молчаливые: если ORB-бар не найден, это видно в сообщении.
"""

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

NL = chr(10)

# —— Список символов ———————————————————————————————————————————
SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT",
    "DOGEUSDT", "LINKUSDT", "AVAXUSDT", "DOTUSDT", "LTCUSDT",
    "BCHUSDT", "ATOMUSDT", "NEARUSDT", "APTUSDT", "SUIUSDT",
    "OPUSDT", "ARBUSDT", "TONUSDT", "FILUSDT", "INJUSDT",
    "TIAUSDT", "SEIUSDT", "UNIUSDT", "AAVEUSDT", "HBARUSDT",
    "HYPEUSDT", "TRXUSDT", "ZECUSDT", "ETCUSDT",
    "RENDERUSDT", "TAOUSDT", "FETUSDT", "WLDUSDT", "ENAUSDT",
    "ONDOUSDT", "JUPUSDT", "STXUSDT", "ICPUSDT", "XLMUSDT",
    "KASUSDT", "PENGUUSDT", "VIRTUALUSDT", "KAITOUSDT",
    "PEPEUSDT", "WIFUSDT", "BONKUSDT", "TRUMPUSDT",
]

ATR_THRESHOLD = 0.15   # тело бара >= 15% дневного ATR(14)
                       # бэктест 19.07.26: тело 10-15% ATR = WR 8%, -9R (зона смерти)
BARS_15M = 24          # 6 часов истории — ORB-бар найдётся даже при задержке в 2 часа

# Индексы полей свечи
TS, O, H, L, C, CONF = 0, 1, 2, 3, 4, 5


# —— Время открытия NY ——————————————————————————————————————————

def orb_bar_ts_ms(now_utc: datetime) -> int:
    """
    Метка времени 15M-бара открытия NYSE (09:30 ET) для текущей даты, в мс.
    Летом это 13:30 UTC, зимой 14:30 UTC — zoneinfo решает сам.
    """
    et_now = now_utc.astimezone(ZoneInfo("America/New_York"))
    et_open = et_now.replace(hour=9, minute=30, second=0, microsecond=0)
    return int(et_open.astimezone(timezone.utc).timestamp() * 1000)


def fmt_ts(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# —— API-запросы ———————————————————————————————————————————————

def to_okx_inst(symbol: str) -> str:
    """BTCUSDT -> BTC-USDT-SWAP"""
    return f"{symbol.replace('USDT', '')}-USDT-SWAP"


def okx_klines(symbol: str, bar: str, limit: int) -> list:
    """
    Список свечей от старой к новой: [ts_ms, open, high, low, close, confirm].
    bar: '15m' | '1D'
    """
    url = ("https://www.okx.com/api/v5/market/history-candles"
           if bar == "1D" else
           "https://www.okx.com/api/v5/market/candles")
    r = requests.get(url,
                     params={"instId": to_okx_inst(symbol), "bar": bar, "limit": limit},
                     timeout=10)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != "0":
        raise ValueError(f"OKX error: {data.get('msg')}")
    rows = data["data"][::-1]          # OKX отдаёт новейший первым
    # [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
    return [[int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), str(x[8])]
            for x in rows]


# —— Расчёты ———————————————————————————————————————————————————

def calc_atr14(daily: list) -> float:
    trs = []
    for i in range(1, len(daily)):
        h, l, pc = daily[i][H], daily[i][L], daily[i - 1][C]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if not trs:
        return 0.0
    return sum(trs[-14:]) / min(len(trs), 14)


def is_jw_bear(bars: list) -> bool:
    """3 медвежьих свечи подряд (close < open)."""
    if len(bars) < 3:
        return False
    return all(b[C] < b[O] for b in bars[-3:])


def pick_orb_bar(candles: list, orb_ts: int):
    """
    Возвращает (bar, index, error). Бар ищется строго по метке времени.
    """
    for i, c in enumerate(candles):
        if c[TS] == orb_ts:
            if c[CONF] != "1":
                return None, i, "бар ещё не закрыт (confirm=0)"
            if i < 3:
                return None, i, "меньше 3 свечей до ORB-бара в выдаче"
            return c, i, None
    return None, -1, f"бар {fmt_ts(orb_ts)} отсутствует в выдаче"


# —— Основной скан ——————————————————————————————————————————————

def scan():
    now = datetime.now(timezone.utc)
    orb_ts = orb_bar_ts_ms(now)
    delay = int((now.timestamp() * 1000 - orb_ts) / 60000) - 15

    print(f"[{now.strftime('%Y-%m-%d %H:%M UTC')}] ORB Scanner — {len(SYMBOLS)} монет")
    print(f"Целевой ORB-бар: {fmt_ts(orb_ts)} | запуск через {delay} мин после его закрытия")

    signals, problems = [], []

    for sym in SYMBOLS:
        try:
            candles = okx_klines(sym, "15m", BARS_15M)
            daily = okx_klines(sym, "1D", 15)
        except Exception as e:
            print(f"[!] {sym}: {e}")
            problems.append(f"{sym}: {e}")
            continue

        orb, idx, err = pick_orb_bar(candles, orb_ts)
        if err:
            print(f"[!] {sym}: {err}")
            problems.append(f"{sym}: {err}")
            continue

        atr = calc_atr14(daily)
        if atr == 0:
            problems.append(f"{sym}: ATR = 0")
            continue

        body = abs(orb[C] - orb[O])

        # Фильтр 1: импульс — тело >= 15% дневного ATR
        if body < ATR_THRESHOLD * atr:
            continue
        # Фильтр 2: ORB-бар медвежий
        if orb[C] >= orb[O]:
            continue
        # Фильтр 3: JW-bear — 3 медвежьих свечи НЕПОСРЕДСТВЕННО перед ORB-баром
        if not is_jw_bear(candles[idx - 3:idx]):
            continue

        signals.append({
            "symbol": sym,
            "orb_ts": fmt_ts(orb[TS]),
            "orb_open": orb[O], "orb_high": orb[H],
            "orb_low": orb[L], "orb_close": orb[C],
            "atr": round(atr, 6),
            "body_pct": round(body / atr * 100, 1),
        })
        print(f"[СИГНАЛ] {sym} [{fmt_ts(orb[TS])}]: "
              f"O={orb[O]} H={orb[H]} L={orb[L]} C={orb[C]} | тело={round(body / atr * 100, 1)}% ATR")

    return signals, problems, orb_ts


# —— Telegram ——————————————————————————————————————————————————

def send_telegram(signals: list, problems: list, orb_ts: int):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print("[!] TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не заданы")
        return

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    head = f"ORB-бар: <b>{fmt_ts(orb_ts)}</b>"

    if signals:
        lines = [f"<b>ORB NY Scanner</b> [{now}] — {len(signals)} сигнал(ов) SHORT", head, ""]
        for s in signals:
            lines.append(f"<b>{s['symbol']}</b>")
            lines.append(f"   O={s['orb_open']} H={s['orb_high']} L={s['orb_low']} C={s['orb_close']}")
            lines.append(f"   Тело={s['body_pct']}% ATR | ATR={s['atr']}")
            lines.append(f"   Вход: пробой {s['orb_low']} | SL: {s['orb_high']}")
    else:
        lines = [f"<b>ORB NY Scanner</b> [{now}]", head, "",
                 "Сигналов нет — рынок не подходит для входа."]

    if problems:
        lines.append("")
        lines.append(f"Проблем: {len(problems)} из {len(SYMBOLS)}")
        for p in problems[:5]:
            lines.append(f"   - {p}")
        if len(problems) > 5:
            lines.append(f"   - ещё {len(problems) - 5}")

    text = NL.join(lines)
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                      timeout=10)
    print(f"Telegram: отправлено ({len(signals)} сигнал(ов))" if r.ok
          else f"Telegram error: {r.text}")


if __name__ == "__main__":
    sig, prob, ts = scan()
    send_telegram(sig, prob, ts)
