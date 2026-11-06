#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
forward_robot.py — единый код-агент форвард-теста (крипта novaya-ts + металлы xau-xag-v00).

Запускается GitHub Actions по cron раз в час. Ни от Cowork-триггеров, ни от очереди
Claude-расписаний не зависит — отдельный независимый процесс.

Что делает за один запуск:

  КРИПТА (novaya-ts, модуль «Моментум D1 + режимный фильтр», 8 мажоров):
    - раз в сутки (после закрытия дневного бара, ~00:00 UTC) ищет новый сигнал —
      режимный фильтр + z-score(50) пробой перцентиля(60), ER не используется в этой
      версии (как в проверенном d1_watch.py); candidates пишутся в forward_calls.csv
      status=предложено;
    - КАЖДЫЙ запуск (раз в час) проверяет уже открытые позиции по ТЕКУЩЕЙ цене —
      чтобы не ждать closed дневного бара для того, чтобы узнать об исходе;
    - раз в сутки дополнительно подтверждает исход по дневному бару (high/low) —
      страховка на случай внутридневного прокола, который успел откатиться между
      часовыми проверками, и считает дни для тайм-стопа (30 дней).

  ЗОЛОТО/СЕРЕБРО (xau-xag-v00, модуль «свеча 03:00/04:00 МСК»):
    - каждый час: тянет часовые бары XAUUSD/XAGUSD (Yahoo Finance, GC=F/SI=F),
      ищет свип свечи candle3, регистрирует кандидата (status=предложено),
      проверяет подтверждение на candle(h3+1) (подтверждён/аннулирован),
      проверяет открытые подтверждённые позиции на SL/TP (закрыт).

Правило станка: арифметику считает этот файл. Числа — либо с биржи/Yahoo, либо
посчитаны здесь по формулам из fwd_d1.py / fwd_common.py / fwd_gold.py (оригиналы —
Cowork-версии тех же модулей, тождественная логика).

ENV: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
Файлы репозитория: forward/forward_calls.csv (общий журнал), forward/robot_state.json (стейт)
"""
import json, os, sys, csv, datetime, statistics, urllib.request, urllib.parse, urllib.error

try:
    from zoneinfo import ZoneInfo
    MSK = ZoneInfo("Europe/Moscow")
except Exception:
    MSK = datetime.timezone(datetime.timedelta(hours=3))

REPO_ROOT = os.environ.get("GITHUB_WORKSPACE", os.path.dirname(os.path.abspath(__file__)))
CSV_PATH = os.path.join(REPO_ROOT, "forward", "forward_calls.csv")
STATE_PATH = os.path.join(REPO_ROOT, "forward", "robot_state.json")

CSV_HEADER = ["id", "ts_msk", "skill", "instrument", "direction", "tf_setup", "entry", "sl", "tp1",
              "tp2", "risk_pct", "costs_r", "status", "decline_reason", "trigger", "rationale",
              "exit", "result_r", "mfe_r", "mae_r", "held_h", "overlap_with", "conflict_with", "notes"]

UA = {"User-Agent": "Mozilla/5.0 (compatible; forward-test-robot/1.0)"}


# ================================================================== общие утилиты
def http_get_json(url, headers=None):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def costs_r(entry, sl, fee_side, spread, fund_per_period, periods):
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    return (fee_side * 2 + spread + fund_per_period * periods) / (risk / entry)


def load_csv():
    if not os.path.exists(CSV_PATH):
        return []
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_csv(rows):
    os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_HEADER)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in CSV_HEADER})


def next_id(rows):
    ids = [int(r["id"]) for r in rows if r.get("id", "").isdigit()]
    return (max(ids) + 1) if ids else 1


def load_state():
    if os.path.exists(STATE_PATH):
        return json.load(open(STATE_PATH, encoding="utf-8"))
    return {"crypto": {"otkrytye": {}, "posledniy_den": 0}, "gold": {"pending": {}}}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    json.dump(state, open(STATE_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def tg_send(text):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("[telegram] TELEGRAM_BOT_TOKEN/CHAT_ID не заданы — сообщение не отправлено")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = json.dumps({"chat_id": chat, "text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15)
        print("[telegram] отправлено")
    except urllib.error.URLError as e:
        print(f"[telegram] ошибка: {e}")


# ================================================================== КРИПТА (novaya-ts)
COINS = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX"]
Z_OKNO, PCT_OKNO = 50, 60
PCT_VERH, PCT_NIZ = 0.90, 0.10
ATR_OKNO = 14
SL_MULT, RR = 1.5, 2.0
TAIM_STOP = 30
VOL_POROG, DISP_POROG = 0.0168, 0.0439
KOM_RT, SPRED, FAND_8H, FAND_DNEY = 0.00016, 0.00008, 0.0003, 6
IZD_POROG = 0.20
MAX_POZICIY = 1


def okx_http(url):
    req = urllib.request.Request(url, headers=UA)
    for _ in range(3):
        try:
            return json.loads(urllib.request.urlopen(req, timeout=15).read().decode())
        except Exception:
            pass
    return None


def svechi_d1(moneta, limit=200):
    d = okx_http(f"https://www.okx.com/api/v5/market/candles?instId={moneta}-USDT&bar=1Dutc&limit={limit}")
    if not d or d.get("code") != "0":
        return []
    out = []
    for r in d["data"][::-1]:
        if r[8] != "1":
            continue
        out.append([int(r[0]) // 1000, float(r[1]), float(r[2]), float(r[3]), float(r[4])])
    return out


def cena_seychas(moneta):
    d = okx_http(f"https://www.okx.com/api/v5/market/ticker?instId={moneta}-USDT")
    try:
        return float(d["data"][0]["last"])
    except Exception:
        return None


def crypto_atr_arr(b, w=ATR_OKNO):
    n = len(b)
    out = [0.0] * n
    s = 0.0
    for i in range(1, n):
        tr = max(b[i][2] - b[i][3], abs(b[i][2] - b[i - 1][4]), abs(b[i][3] - b[i - 1][4]))
        s += tr
        if i > w:
            j = i - w
            s -= max(b[j][2] - b[j][3], abs(b[j][2] - b[j - 1][4]), abs(b[j][3] - b[j - 1][4]))
        out[i] = s / min(i, w)
    return out


def crypto_zs(P, i, w=Z_OKNO):
    s = P[i - w + 1:i + 1]
    m = sum(s) / w
    sd = (sum((y - m) ** 2 for y in s) / w) ** .5
    return (P[i] - m) / sd if sd else 0.0


def crypto_pctl(seg, q):
    s = sorted(seg)
    idx = q * (len(s) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (idx - lo)


def crypto_rezhim(D):
    dni = sorted(set.intersection(*[set(x[0] for x in v) for v in D.values()]))
    if len(dni) < 30:
        return None, None, None
    po = {c: {x[0]: x[4] for x in D[c]} for c in D}
    i = len(dni) - 2
    btc = [po["BTC"][d] for d in dni]
    vol = statistics.mean(abs(btc[j] / btc[j - 1] - 1) for j in range(i - 19, i + 1))
    disp = statistics.pstdev([po[c][dni[i]] / po[c][dni[i - 7]] - 1 for c in D])
    return vol, disp, dni[i]


def crypto_signal(b):
    n = len(b)
    if n < Z_OKNO + PCT_OKNO + 5:
        return None
    Cc = [r[4] for r in b]
    osc = [0.0] * n
    for i in range(Z_OKNO, n):
        osc[i] = crypto_zs(Cc, i)
    i = n - 1
    seg = osc[i - PCT_OKNO:i]
    verh, niz = crypto_pctl(seg, PCT_VERH), crypto_pctl(seg, PCT_NIZ)
    if osc[i] > verh and osc[i - 1] <= verh:
        return (1, i)
    if osc[i] < niz and osc[i - 1] >= niz:
        return (-1, i)
    return None


def crypto_izderzhki_r(vhod, risk):
    nachisleniy = FAND_DNEY * 3
    dolya = KOM_RT + SPRED + FAND_8H * nachisleniy
    return vhod * dolya / risk


def crypto_update_row(rows, rid, **fields):
    for r in rows:
        if r.get("id") == str(rid):
            r.update(fields)
            return True
    return False


def crypto_close_position(rows, state, coin, exit_px, reason, events):
    p = state["crypto"]["otkrytye"].pop(coin, None)
    if not p:
        return
    d = p["napravlenie"]
    risk = abs(p["vhod"] - p["sl"])
    gross = d * (exit_px - p["vhod"]) / risk
    netto = gross - crypto_izderzhki_r(p["vhod"], risk)
    held_h = int((_utc_now_naive() - datetime.datetime.fromtimestamp(p["ts_vhoda"], tz=datetime.timezone.utc).replace(tzinfo=None)).total_seconds() // 3600)
    crypto_update_row(rows, p["csv_id"], status="закрыт", exit=reason,
                       result_r=f"{netto:+.4f}", held_h=str(held_h))
    events.append(("crypto_close", coin, d, p["vhod"], exit_px, reason, netto))


def _utc_now_naive():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def crypto_check_exits_live(rows, state, events):
    """Каждый час: проверка открытых позиций по ТЕКУЩЕЙ цене (не ждём дневного бара)."""
    for coin in list(state["crypto"]["otkrytye"].keys()):
        p = state["crypto"]["otkrytye"][coin]
        px = cena_seychas(coin)
        if px is None:
            continue
        d = p["napravlenie"]
        if d > 0:
            if px <= p["sl"]:
                crypto_close_position(rows, state, coin, p["sl"], "СТОП", events)
            elif px >= p["tp"]:
                crypto_close_position(rows, state, coin, p["tp"], "ТЕЙК", events)
        else:
            if px >= p["sl"]:
                crypto_close_position(rows, state, coin, p["sl"], "СТОП", events)
            elif px <= p["tp"]:
                crypto_close_position(rows, state, coin, p["tp"], "ТЕЙК", events)


def crypto_daily_cycle(rows, state, events):
    """Раз в сутки: страховка по дневному бару (high/low, тайм-стоп) + поиск нового сигнала."""
    D = {}
    for c in COINS:
        b = svechi_d1(c)
        if len(b) < Z_OKNO + PCT_OKNO + 5:
            print(f"[crypto] мало данных по {c} ({len(b)} баров)")
            return
        D[c] = b

    posl_den = D["BTC"][-1][0]
    if posl_den <= state["crypto"].get("posledniy_den", 0):
        return  # этот день уже обработан

    # страховка по дневному бару для открытых позиций (на случай внутридневного прокола
    # между часовыми проверками, который к моменту часовой проверки уже откатился назад)
    for coin in list(state["crypto"]["otkrytye"].keys()):
        p = state["crypto"]["otkrytye"][coin]
        b = D.get(coin)
        if not b:
            continue
        novye = [x for x in b if x[0] > p.get("ts_proveren", 0)]
        for bar in novye:
            p["ts_proveren"] = bar[0]
            p["barov"] = p.get("barov", 0) + 1
            d = p["napravlenie"]
            if coin not in state["crypto"]["otkrytye"]:
                break
            if d > 0:
                if bar[3] <= p["sl"]:
                    crypto_close_position(rows, state, coin, p["sl"], "СТОП", events); break
                if bar[2] >= p["tp"]:
                    crypto_close_position(rows, state, coin, p["tp"], "ТЕЙК", events); break
            else:
                if bar[2] >= p["sl"]:
                    crypto_close_position(rows, state, coin, p["sl"], "СТОП", events); break
                if bar[3] <= p["tp"]:
                    crypto_close_position(rows, state, coin, p["tp"], "ТЕЙК", events); break
            if p.get("barov", 0) >= TAIM_STOP:
                crypto_close_position(rows, state, coin, bar[4], f"ТАЙМ-СТОП {TAIM_STOP} дней", events)
                break

    vol, disp, _ = crypto_rezhim(D)
    if vol is None:
        state["crypto"]["posledniy_den"] = posl_den
        return
    rezhim_ok = vol < VOL_POROG and disp < DISP_POROG

    if rezhim_ok:
        for c in COINS:
            if c in state["crypto"]["otkrytye"]:
                continue
            s = crypto_signal(D[c])
            if not s:
                continue
            d, i = s
            b = D[c]
            A = crypto_atr_arr(b)
            a = A[i]
            if a <= 0:
                continue
            vhod = b[i][4]
            risk = SL_MULT * a
            sl = vhod - d * risk
            tp = vhod + d * RR * risk
            izd = crypto_izderzhki_r(vhod, risk)
            notes = []
            if izd > IZD_POROG:
                notes.append(f"издержки {izd:.2f}R выше порога {IZD_POROG}R — бэктест такие сетапы пропускает")
            uzhe = [k for k in state["crypto"]["otkrytye"] if k != c]
            if len(uzhe) >= MAX_POZICIY:
                notes.append(f"риск-правило (макс {MAX_POZICIY} позиция) нарушено, уже открыто: {', '.join(uzhe)} — бэктест допускал несколько монет одновременно, записываем как есть")
            if ("BTC" in uzhe and c == "ETH") or ("ETH" in uzhe and c == "BTC"):
                notes.append("BTC и ETH одновременно — правило корреляции нарушено")

            rid = next_id(rows)
            date_str = datetime.datetime.fromtimestamp(b[i][0], tz=datetime.timezone.utc).astimezone(MSK).strftime("%Y-%m-%dT%H:%M:%S%z")
            row = {
                "id": str(rid), "ts_msk": date_str, "skill": "novaya-ts",
                "instrument": f"{c}USDT", "direction": "LONG" if d > 0 else "SHORT", "tf_setup": "D1",
                "entry": f"{vhod:.8g}", "sl": f"{sl:.8g}", "tp1": f"{tp:.8g}", "tp2": "",
                "risk_pct": f"{risk / vhod * 100:.2f}", "costs_r": f"{izd:.4f}",
                "status": "предложено", "decline_reason": "", "trigger": "z-score пробой перцентиля",
                "rationale": f"режим РАЗРЕШЁН (вол {vol*100:.2f}%, дисп {disp*100:.2f}%)",
                "exit": "", "result_r": "", "mfe_r": "", "mae_r": "", "held_h": "",
                "overlap_with": "", "conflict_with": "",
                "notes": "автокод (forward_robot.py)" + ("; " + "; ".join(notes) if notes else ""),
            }
            rows.append(row)
            state["crypto"]["otkrytye"][c] = {"napravlenie": d, "vhod": vhod, "sl": sl, "tp": tp,
                                               "ts_vhoda": b[i][0], "ts_proveren": b[i][0],
                                               "barov": 0, "csv_id": rid}
            events.append(("crypto_signal", c, d, vhod, sl, tp, izd, rid))

    state["crypto"]["posledniy_den"] = posl_den


# ================================================================== ЗОЛОТО/СЕРЕБРО (xau-xag-v00)
YAHOO_SYMBOLS = {"XAUUSD": "GC=F", "XAGUSD": "SI=F"}
GOLD_FEE, GOLD_SPREAD, GOLD_FUND_DAY = 0.000035, 0.000035, 0.00048


def fetch_hourly_yahoo(yahoo_symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(yahoo_symbol)}?interval=60m&range=7d"
    data = http_get_json(url)
    result = data["chart"]["result"][0]
    ts = result["timestamp"]
    q = result["indicators"]["quote"][0]
    out = []
    for i, t in enumerate(ts):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c):
            continue
        out.append({"ts": datetime.datetime.fromtimestamp(t, tz=datetime.timezone.utc), "o": o, "h": h, "l": l, "c": c})
    out.sort(key=lambda b: b["ts"])
    return out


def gold_by_hour(bars):
    d = {}
    for b in bars:
        t = b["ts"].astimezone(MSK)
        d[(t.date(), t.hour)] = b
    return d


def gold_shift_for(date):
    return 1 if date >= datetime.date(2026, 11, 1) else 0


def gold_find_new(instrument, bars, state, events):
    bh = gold_by_hour(bars)
    now_msk = bars[-1]["ts"].astimezone(MSK) if bars else None
    if not now_msk:
        return
    date = now_msk.date()
    sh = gold_shift_for(date)
    h2, h3 = 2 + sh, 3 + sh
    c2, c3 = bh.get((date, h2)), bh.get((date, h3))
    if not c2 or not c3:
        return
    short = c3["h"] > c2["h"] and c3["c"] < c2["h"]
    long_ = c3["l"] < c2["l"] and c3["c"] > c2["l"]
    if not (short or long_):
        return
    direction = "SHORT" if short else "LONG"
    key = f"{instrument}:{c3['ts'].isoformat()}"
    if key in state["gold"]["pending"]:
        return
    sl = c3["h"] if short else c3["l"]
    entry = c3["c"]
    risk = abs(entry - sl)
    if risk <= 0:
        return
    tp = entry - 2 * risk if short else entry + 2 * risk
    wed = now_msk.weekday() == 2
    cr = costs_r(entry, sl, GOLD_FEE, GOLD_SPREAD, GOLD_FUND_DAY * (3 if wed else 1), 1)
    cand = dict(instrument=instrument, direction=direction, entry=entry, sl=sl, tp1=tp,
                costs_r=round(cr, 4) if cr else "", candle3_ts=c3["ts"].isoformat(),
                confirm_deadline_hour=h3 + 1, confirm_date=date.isoformat(),
                threshold=(c3["l"] if short else c3["h"]))
    state["gold"]["pending"][key] = cand
    events.append(("gold_register", key, cand))


def gold_check_pending(instrument, bars, state, events):
    bh = gold_by_hour(bars)
    for key, cand in list(state["gold"]["pending"].items()):
        if cand["instrument"] != instrument or "entered" in cand:
            continue
        cdate = datetime.date.fromisoformat(cand["confirm_date"])
        h = cand["confirm_deadline_hour"]
        conf = bh.get((cdate, h))
        if not conf:
            continue
        if cand["direction"] == "SHORT":
            confirmed = conf["c"] < cand["threshold"]
        else:
            confirmed = conf["c"] > cand["threshold"]
        cand["confirmed"] = confirmed
        cand["real_entry"] = conf["c"] if confirmed else None
        cand["entered"] = True
        cand["confirm_ts"] = conf["ts"].isoformat()
        events.append(("gold_confirm", key, cand))


def gold_check_open(instrument, bars, state, events):
    for key, cand in list(state["gold"]["pending"].items()):
        if cand["instrument"] != instrument or not cand.get("entered") or not cand.get("confirmed") or cand.get("closed"):
            continue
        confirm_ts = datetime.datetime.fromisoformat(cand["confirm_ts"])
        for b in bars:
            if b["ts"] <= confirm_ts:
                continue
            sl, tp1, direction = cand["sl"], cand["tp1"], cand["direction"]
            if direction == "SHORT":
                hit_sl, hit_tp = b["h"] >= sl, b["l"] <= tp1
            else:
                hit_sl, hit_tp = b["l"] <= sl, b["h"] >= tp1
            if hit_sl or hit_tp:
                costs = cand["costs_r"] or 0.0
                r = (2.0 - costs) if (hit_tp and not hit_sl) else (-1.0 - costs)
                cand["closed"] = True
                cand["exit_ts"] = b["ts"].isoformat()
                cand["result_r"] = round(r, 4)
                cand["outcome"] = "TP" if (hit_tp and not hit_sl) else "SL"
                events.append(("gold_close", key, cand))
                break


def gold_sync_csv(rows, state, events):
    for key, cand in state["gold"]["pending"].items():
        if cand.get("csv_id"):
            continue
        rid = next_id(rows)
        row = {
            "id": str(rid), "ts_msk": cand["candle3_ts"], "skill": "xau-xag-v00",
            "instrument": cand["instrument"], "direction": cand["direction"], "tf_setup": "1H",
            "entry": f"{cand['entry']:.5g}", "sl": f"{cand['sl']:.5g}", "tp1": f"{cand['tp1']:.5g}",
            "tp2": "", "risk_pct": f"{100*abs(cand['entry']-cand['sl'])/cand['entry']:.2f}",
            "costs_r": cand["costs_r"], "status": "предложено", "decline_reason": "", "trigger": "",
            "rationale": "", "exit": "", "result_r": "", "mfe_r": "", "mae_r": "", "held_h": "",
            "overlap_with": "", "conflict_with": "",
            "notes": f"автокод (forward_robot.py), подтверждение по закрытию h{cand['confirm_deadline_hour']:02d}:00 МСК выше/ниже {cand['threshold']:.5g}",
        }
        rows.append(row)
        cand["csv_id"] = rid

    by_id = {int(r["id"]): r for r in rows if r.get("id", "").isdigit()}
    for ev in list(events):
        # в events лежат и крипто-события (7-8 полей), и золотые (3 поля): разбирать только золотые
        if ev[0] not in ("gold_confirm", "gold_close"):
            continue
        kind, key, cand = ev
        rid = cand.get("csv_id")
        if not rid or rid not in by_id:
            continue
        r = by_id[rid]
        if kind == "gold_confirm":
            if cand["confirmed"]:
                r["status"] = "подтверждён"
                r["entry"] = f"{cand['real_entry']:.5g}"
            else:
                r["status"] = "аннулирован"
                r["result_r"] = "0"
        elif kind == "gold_close":
            r["status"] = "закрыт"
            r["exit"] = cand["outcome"]
            r["result_r"] = str(cand["result_r"])


def gold_cycle(rows, state, events):
    for instrument, ysym in YAHOO_SYMBOLS.items():
        try:
            bars = fetch_hourly_yahoo(ysym)
        except Exception as e:
            print(f"[gold:{instrument}] ошибка получения данных Yahoo ({ysym}): {e}")
            continue
        if not bars:
            print(f"[gold:{instrument}] нет баров")
            continue
        gold_check_pending(instrument, bars, state, events)
        gold_check_open(instrument, bars, state, events)
        gold_find_new(instrument, bars, state, events)
    gold_sync_csv(rows, state, events)


# ================================================================== main
def format_telegram(events):
    lines = []
    for ev in events:
        kind = ev[0]
        if kind == "crypto_signal":
            _, c, d, vhod, sl, tp, izd, rid = ev
            lines.append(f"📈 КРИПТА {c} {'ЛОНГ' if d>0 else 'ШОРТ'}: вход {vhod:.6g} SL {sl:.6g} TP {tp:.6g} издержки {izd:.3f}R (id {rid})")
        elif kind == "crypto_close":
            _, c, d, vhod, exit_px, reason, netto = ev
            znak = "🟢" if netto > 0 else "🔴"
            lines.append(f"{znak} КРИПТА ВЫХОД {c} {'ЛОНГ' if d>0 else 'ШОРТ'}: {reason}, вход {vhod:.6g} -> выход {exit_px:.6g}, нетто {netto:+.3f}R")
        elif kind == "gold_register":
            _, key, cand = ev
            lines.append(f"🥇 Новый кандидат {cand['instrument']} {cand['direction']}: вход~{cand['entry']:.5g} SL {cand['sl']:.5g} TP {cand['tp1']:.5g} издержки {cand['costs_r']}R (предложено автоматом)")
        elif kind == "gold_confirm":
            _, key, cand = ev
            if cand["confirmed"]:
                lines.append(f"🥇 ПОДТВЕРЖДЕНО {cand['instrument']} {cand['direction']} — вход {cand['real_entry']:.5g}")
            else:
                lines.append(f"🥇 Аннулирован {cand['instrument']} {cand['direction']} — цена не подтвердила")
        elif kind == "gold_close":
            _, key, cand = ev
            znak = "🟢" if cand["result_r"] > 0 else "🔴"
            lines.append(f"{znak} ЗОЛОТО ЗАКРЫТО {cand['instrument']} {cand['direction']} по {cand['outcome']}: {cand['result_r']:+.3f}R")
    return lines


def main():
    rows = load_csv()
    state = load_state()
    events = []

    # крипта: живая проверка открытых позиций — каждый запуск (раз в час)
    crypto_check_exits_live(rows, state, events)
    # крипта: суточный цикл (страховка по бару + поиск нового сигнала) — не чаще раза в сутки
    crypto_daily_cycle(rows, state, events)

    # золото/серебро: полный цикл каждый час
    gold_cycle(rows, state, events)

    save_csv(rows)
    save_state(state)

    lines = format_telegram(events)
    if lines:
        tg_send("\n".join(lines) + "\n\nЧисла из forward_robot.py (автокод, без Cowork). Решение о входе — только твоё.")
    else:
        print("Без изменений — ничего не отправлено в Telegram.")


if __name__ == "__main__":
    main()
