import os
import re
import json
import time
import math
import random
import asyncio
import logging
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone, date, timedelta

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from nba_api.live.nba.endpoints import scoreboard, boxscore
from nba_api.stats.static import players

# =========================
# CONFIG
# =========================
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("nba-bot")

TELEGRAM_TOKEN = (os.environ.get("TELEGRAM_TOKEN") or "").strip()
if not TELEGRAM_TOKEN:
    raise RuntimeError("Missing TELEGRAM_TOKEN env var. Set it in Railway Variables or your shell.")

POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "120"))

PROPS_FILE = "props.json"              # opcional (manual)
ALERTS_STATE_FILE = "alerts_state.json"
IDS_CACHE_FILE = "player_ids_cache.json"
GLOG_CACHE_FILE = "gamelog_cache.json"

SEASON = os.environ.get("NBA_SEASON", "2025-26")

FINAL_ALERT_THRESHOLD = 75
FINAL_ALERT_THRESHOLD_CLUTCH = 68

COOLDOWN_SECONDS = 8 * 60
BLOWOUT_IS = 20
BLOWOUT_STRONG = 22

THRESH_POINTS_OVER = (0.5, 4.0)
THRESH_REB_AST_OVER = (0.5, 1.5)

MIN_MINUTES_POINTS = 10.0
MIN_MINUTES_REB_AST = 14.0

STAT_COL = {"puntos": "PTS", "rebotes": "REB", "asistencias": "AST"}
STD_CAP = {"puntos": 8.0, "rebotes": 4.0, "asistencias": 3.0}
MARGIN_CAP = {"puntos": 8.0, "rebotes": 3.0, "asistencias": 3.0}

# Polymarket Gamma API
GAMMA = "https://gamma-api.polymarket.com"
PM_NBA_SERIES_ID = (os.environ.get("PM_NBA_SERIES_ID") or "").strip()  # si lo pones, no adivina
PM_LOOKAHEAD_HOURS = int(os.environ.get("PM_LOOKAHEAD_HOURS", "24"))
PM_MAX_EVENTS = int(os.environ.get("PM_MAX_EVENTS", "20"))
PM_MAX_MARKETS = int(os.environ.get("PM_MAX_MARKETS", "500"))

# =========================
# HTTP sessions
# =========================
NBA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Referer": "https://www.nba.com/",
    "Origin": "https://www.nba.com",
    "Connection": "keep-alive",
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token": "true",
}

def build_session(headers: dict) -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=6,
        connect=6,
        read=6,
        backoff_factor=1.2,
        status_forcelist=(403, 408, 429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update(headers)
    return s

SESSION_NBA = build_session(NBA_HEADERS)
SESSION_PM = build_session({"User-Agent": NBA_HEADERS["User-Agent"], "Accept": "application/json"})


# =========================
# Persistence helpers
# =========================
def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path: str, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def now_ts() -> int:
    return int(time.time())


# =========================
# Data model
# =========================
@dataclass
class Prop:
    player: str
    tipo: str              # puntos | rebotes | asistencias
    line: float
    side: str              # over | under
    source: str = "manual" # manual | polymarket
    event: Optional[str] = None
    market_id: Optional[str] = None


def load_props() -> List[Prop]:
    raw = load_json(PROPS_FILE, {"props": []})
    out = []
    for p in raw.get("props", []):
        try:
            out.append(Prop(**p))
        except Exception:
            continue
    return out


# =========================
# Player ID cache
# =========================
def obtener_id_jugador(nombre: str) -> Optional[int]:
    time.sleep(0.15 + random.random() * 0.15)
    res = players.find_players_by_full_name(nombre)
    if not res:
        return None
    exact = [p for p in res if (p.get("full_name") or "").lower() == nombre.lower()]
    pick = exact[0] if exact else res[0]
    pid = pick.get("id")
    return int(pid) if pid else None

def load_ids_cache() -> Dict[str, int]:
    return load_json(IDS_CACHE_FILE, {})  # name -> id

def save_ids_cache(c: Dict[str, int]):
    save_json(IDS_CACHE_FILE, c)

def get_pid_for_name(name: str) -> Optional[int]:
    cache = load_ids_cache()
    if name in cache:
        return int(cache[name])
    pid = obtener_id_jugador(name)
    if pid:
        cache[name] = int(pid)
        save_ids_cache(cache)
    return pid


# =========================
# Gamelog cache + fetch (stats.nba.com)
# =========================
GLOG_TTL_SECONDS = 6 * 60 * 60

def load_glog_cache():
    return load_json(GLOG_CACHE_FILE, {})

def save_glog_cache(c):
    save_json(GLOG_CACHE_FILE, c)

def get_gamelog_table(pid: int) -> Tuple[List[str], List[list]]:
    cache = load_glog_cache()
    k = str(pid)
    now = now_ts()

    if k in cache and (now - int(cache[k].get("ts", 0))) < GLOG_TTL_SECONDS:
        return cache[k].get("headers", []), cache[k].get("rows", [])

    time.sleep(0.5 + random.random() * 0.4)

    url = "https://stats.nba.com/stats/playergamelog"
    params = {
        "DateFrom": "",
        "DateTo": "",
        "LeagueID": "00",
        "PlayerID": str(pid),
        "Season": SEASON,
        "SeasonType": "Regular Season",
    }

    try:
        resp = SESSION_NBA.get(url, params=params, timeout=(12, 90))
        if resp.status_code != 200:
            return cache.get(k, {}).get("headers", []), cache.get(k, {}).get("rows", [])

        data = resp.json()
        rs = data.get("resultSets") or []
        if not rs:
            single = data.get("resultSet")
            rs = [single] if single else []

        if not rs or not isinstance(rs[0], dict):
            return cache.get(k, {}).get("headers", []), cache.get(k, {}).get("rows", [])

        headers = rs[0].get("headers", []) or []
        rows = rs[0].get("rowSet", []) or []

        cache[k] = {"ts": now, "headers": headers, "rows": rows}
        save_glog_cache(cache)
        return headers, rows

    except Exception:
        return cache.get(k, {}).get("headers", []), cache.get(k, {}).get("rows", [])


# =========================
# PRE score (forma 5/10)
# =========================
def clamp(x, lo=0, hi=100):
    return max(lo, min(hi, x))

def stdev(vals: List[float]) -> float:
    if not vals or len(vals) < 2:
        return 0.0
    mu = sum(vals) / len(vals)
    var = sum((x - mu) ** 2 for x in vals) / (len(vals) - 1)
    return math.sqrt(var)

def last_n_values(pid: int, tipo: str, n: int = 10) -> List[float]:
    headers, rows = get_gamelog_table(pid)
    if not headers or not rows:
        return []
    idx = {h: i for i, h in enumerate(headers)}
    col = STAT_COL.get(tipo)
    i = idx.get(col)
    if i is None:
        return []
    rows_n = rows[:n] if len(rows) >= n else rows
    vals = []
    for r in rows_n:
        if i < len(r):
            try:
                vals.append(float(r[i]))
            except Exception:
                pass
    return vals

def hit_counts(values: List[float], line: float, side: str) -> Tuple[int, int]:
    if not values:
        return 0, 0
    if side == "over":
        hits = sum(1 for v in values if v > line)
    else:
        hits = sum(1 for v in values if v < line)
    return hits, len(values)

def margin_values(values: List[float], line: float, side: str) -> List[float]:
    if side == "over":
        return [v - line for v in values]
    return [line - v for v in values]

def pre_score(pid: int, tipo: str, line: float, side: str) -> Tuple[int, dict]:
    v5 = last_n_values(pid, tipo, 5)
    v10 = last_n_values(pid, tipo, 10)

    h5, n5 = hit_counts(v5, line, side)
    h10, n10 = hit_counts(v10, line, side)

    hit5 = (h5 / n5) if n5 else 0.0
    hit10 = (h10 / n10) if n10 else 0.0

    m5 = margin_values(v5, line, side)
    m10 = margin_values(v10, line, side)

    avg5 = (sum(v5) / len(v5)) if v5 else 0.0
    avg10 = (sum(v10) / len(v10)) if v10 else 0.0

    w_margin = (0.65 * (sum(m10) / len(m10) if m10 else 0.0)) + (0.35 * (sum(m5) / len(m5) if m5 else 0.0))
    w_margin = max(0.0, w_margin)

    HitScore = 100.0 * (0.65 * hit10 + 0.35 * hit5)

    cap_m = MARGIN_CAP.get(tipo, 3.0)
    MarginScore = clamp((w_margin / cap_m) * 100.0, 0, 100)

    std10 = stdev(v10)
    std_cap = STD_CAP.get(tipo, 4.0)
    VolPenalty = clamp((std10 / std_cap) * 60.0, 0, 60)
    ConsistencyScore = 100.0 - VolPenalty

    PRE = int(clamp(0.55 * HitScore + 0.25 * MarginScore + 0.20 * ConsistencyScore, 0, 100))

    meta = {
        "hits5": h5, "n5": n5,
        "hits10": h10, "n10": n10,
        "avg5": round(avg5, 2), "avg10": round(avg10, 2),
    }
    return PRE, meta


# =========================
# Live helpers
# =========================
def parse_minutes(min_str) -> float:
    if not min_str:
        return 0.0
    try:
        mm, ss = str(min_str).split(":")
        return float(mm) + float(ss) / 60.0
    except Exception:
        return 0.0

def clock_to_seconds(game_clock: str) -> Optional[int]:
    if not game_clock:
        return None
    gc = str(game_clock)
    if gc.startswith("PT") and "M" in gc:
        try:
            mm = gc.split("PT")[1].split("M")[0]
            ss = gc.split("M")[1].replace("S", "").split(".")[0]
            return int(mm) * 60 + int(ss)
        except Exception:
            return None
    if ":" in gc:
        try:
            mm, ss = gc.split(":")
            return int(mm) * 60 + int(ss)
        except Exception:
            return None
    return None

def game_elapsed_minutes(period: int, clock_seconds: Optional[int]) -> Optional[float]:
    if clock_seconds is None or period <= 0:
        return None
    if period <= 4:
        total_before = (period - 1) * 720
        elapsed_in_period = 720 - clock_seconds
        return (total_before + elapsed_in_period) / 60.0
    total_before = 4 * 720 + (period - 5) * 300
    elapsed_in_period = 300 - min(clock_seconds, 300)
    return (total_before + elapsed_in_period) / 60.0


# =========================
# LIVE score (compacto)
# =========================
def should_gate_by_minutes(side: str, tipo: str, mins: float, elapsed_min, is_blowout: bool) -> bool:
    if side == "over":
        if elapsed_min is not None and elapsed_min >= 18:
            return False
        min_req = MIN_MINUTES_POINTS if tipo == "puntos" else MIN_MINUTES_REB_AST
        return mins < min_req
    else:
        if elapsed_min is None:
            return True
        if is_blowout and elapsed_min >= 16:
            return False
        return elapsed_min < 22

def compute_over_score(tipo, faltante, mins, pf, period, clock_seconds, diff, is_clutch, is_blowout) -> int:
    if tipo == "puntos":
        near_max, ideal_max = 4.0, 2.0
        close_weight, ideal_bonus = 60, 10
        min_floor = 10.0
        foul_mult, blow_mult = 1.0, 1.0
    else:
        near_max, ideal_max = 1.5, 0.9
        close_weight, ideal_bonus = 65, 12
        min_floor = 14.0
        foul_mult, blow_mult = 1.25, 1.35

    if faltante < 0.5 or faltante > near_max:
        return 0

    base = close_weight * clamp((near_max - faltante) / (near_max - 0.5), 0, 1)
    if faltante <= ideal_max:
        base += ideal_bonus

    spot = 0
    if period >= 4: spot += 12
    elif period == 3: spot += 7
    elif period == 2: spot += 3

    if clock_seconds is not None:
        if period >= 4:
            spot += clamp((720 - clock_seconds) / 720 * 9, 0, 9)
        elif period == 3:
            spot += clamp((720 - clock_seconds) / 720 * 5, 0, 5)

    if is_clutch:
        spot += 11

    min_score = clamp((mins - min_floor) / 18 * 12, 0, 12)

    foul_pen = 0
    if pf >= 5: foul_pen = 18
    elif pf == 4: foul_pen = 10
    elif pf == 3: foul_pen = 4
    foul_pen *= foul_mult

    blow_pen = 0
    if is_blowout:
        if diff >= 25: blow_pen = 18
        elif diff >= 20: blow_pen = 12
    blow_pen *= blow_mult

    score = base + spot + min_score - foul_pen - blow_pen
    return int(clamp(score, 0, 100))

def compute_under_score(tipo, margin_under, mins, pf, period, clock_seconds, diff, is_clutch, is_blowout) -> int:
    if tipo == "puntos":
        min_margin, good_margin = 3.0, 6.0
        blow_bonus, clutch_pen = 20, 10
    else:
        min_margin, good_margin = 2.0, 3.5
        blow_bonus, clutch_pen = 24, 14

    if margin_under < min_margin:
        return 0

    elapsed_min = game_elapsed_minutes(period, clock_seconds) if clock_seconds is not None else None
    time_score = clamp(((elapsed_min or 0) - 20) / 28 * 28, 0, 28)

    cushion = clamp((margin_under - min_margin) / (good_margin - min_margin) * 40, 0, 40)

    blow = 0
    if is_blowout:
        blow = blow_bonus + (6 if diff >= 25 else 0)

    foul_pen = 0
    if pf >= 5: foul_pen = 6
    elif pf == 4: foul_pen = 3

    min_bonus = 0
    if elapsed_min is not None and elapsed_min >= 30:
        if mins < 24: min_bonus = 8
        if mins < 18: min_bonus = 12

    score = cushion + time_score + blow + min_bonus - (clutch_pen if is_clutch else 0) - foul_pen
    return int(clamp(score, 0, 100))


# =========================
# Alert cooldown per prop
# =========================
def load_alert_state():
    return load_json(ALERTS_STATE_FILE, {})

def save_alert_state(st):
    save_json(ALERTS_STATE_FILE, st)

def can_send_alert(state: dict, key: str) -> bool:
    now = now_ts()
    last = int(state.get(key, 0))
    if now - last >= COOLDOWN_SECONDS:
        state[key] = now
        return True
    return False


# =========================
# Polymarket Gamma helpers
# =========================
def pm_get_json(path: str, params: dict) -> dict:
    url = f"{GAMMA}{path}"
    r = SESSION_PM.get(url, params=params, timeout=25)
    r.raise_for_status()
    return r.json()

def pm_find_nba_series_id() -> Optional[str]:
    # if user pinned it, best option
    if PM_NBA_SERIES_ID:
        return PM_NBA_SERIES_ID

    data = pm_get_json("/sports", {})
    sports_list = data if isinstance(data, list) else (data.get("sports", []) or data.get("data", []) or [])

    best = None
    for s in sports_list:
        sport_key = (s.get("sport") or s.get("key") or s.get("slug") or "").lower()
        name = (s.get("name") or "").lower()
        if sport_key == "nba" or name.strip() == "nba":
            best = s
            break

    if not best:
        for s in sports_list:
            if "nba" in (s.get("sport") or "").lower() or "nba" in (s.get("name") or "").lower():
                best = s
                break

    if not best:
        return None

    # ✅ en tu payload viene como "series":"10345"
    sid = best.get("series_id") or best.get("seriesId") or best.get("series")
    if sid:
        return str(sid)

    # fallback: si "series" fuese lista
    series = best.get("series") or []
    if isinstance(series, list) and series:
        for it in series:
            if isinstance(it, dict):
                _id = it.get("id") or it.get("series_id") or it.get("seriesId")
                if _id:
                    return str(_id)

    return None

def pm_events_next_window(series_id: str, lookahead_hours: int = 24, limit: int = 20) -> List[dict]:
    now = datetime.now(timezone.utc)
    end = now + timedelta(hours=lookahead_hours)

    # intentamos con ambos params (series_id y series) por robustez
    params = {
        "series_id": series_id,
        "series": series_id,
        "active": "true",
        "closed": "false",
        "archived": "false",
        "order": "startDate",
        "ascending": "true",
        "limit": str(limit),
        "offset": "0",
    }

    data = pm_get_json("/events", params)
    events = data if isinstance(data, list) else (data.get("events", []) or data.get("data", []) or [])

    out = []
    for e in events:
        dt_s = e.get("startDate") or e.get("start_date") or e.get("startTime") or e.get("start_time")
        if not dt_s:
            continue
        try:
            dt = datetime.fromisoformat(dt_s.replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            continue
        if now <= dt <= end:
            out.append(e)
    return out

def pm_markets_for_event(event_id: str, limit: int = 500) -> List[dict]:
    params = {
        "event_id": event_id,
        "active": "true",
        "closed": "false",
        "archived": "false",
        "limit": str(limit),
        "offset": "0",
    }
    data = pm_get_json("/markets", params)
    markets = data if isinstance(data, list) else (data.get("markets", []) or data.get("data", []) or [])
    return markets

# --- Parse props P/R/A desde market strings ---
# intentamos detectar:
# - player: al inicio
# - stat: points/rebounds/assists
# - line: float
STAT_ALIASES = {
    "points": "puntos",
    "point": "puntos",
    "pts": "puntos",
    "rebounds": "rebotes",
    "rebound": "rebotes",
    "reb": "rebotes",
    "assists": "asistencias",
    "assist": "asistencias",
    "ast": "asistencias",
}

def normalize_stat(s: str) -> Optional[str]:
    s = s.lower().strip()
    return STAT_ALIASES.get(s)

def try_parse_prop_from_text(text: str) -> Optional[Tuple[str, str, float]]:
    """
    Devuelve (player, tipo, line) o None.
    """
    t = " ".join((text or "").split())
    if not t:
        return None

    # patrones comunes
    # 1) "Cade Cunningham points 25.5"
    m = re.search(r"^(?P<player>[A-Za-zÀ-ÿ.'\- ]+?)\s+(?P<stat>points|pts|rebounds|reb|assists|ast)\s+(?P<line>\d+(?:\.\d+)?)\b", t, re.I)
    if m:
        player = m.group("player").strip()
        tipo = normalize_stat(m.group("stat"))
        line = float(m.group("line"))
        if tipo:
            return player, tipo, line

    # 2) "Cade Cunningham - Points (25.5)"
    m = re.search(r"^(?P<player>[A-Za-zÀ-ÿ.'\- ]+?)\s*[-:]\s*(?P<stat>points|pts|rebounds|reb|assists|ast)\s*\(?(?P<line>\d+(?:\.\d+)?)\)?", t, re.I)
    if m:
        player = m.group("player").strip()
        tipo = normalize_stat(m.group("stat"))
        line = float(m.group("line"))
        if tipo:
            return player, tipo, line

    # 3) "Points - Cade Cunningham - 25.5" (menos común)
    m = re.search(r"(?P<stat>points|pts|rebounds|reb|assists|ast).{0,15}(?P<player>[A-Za-zÀ-ÿ.'\- ]+?).{0,10}(?P<line>\d+(?:\.\d+)?)", t, re.I)
    if m:
        player = m.group("player").strip()
        tipo = normalize_stat(m.group("stat"))
        line = float(m.group("line"))
        if tipo:
            return player, tipo, line

    return None

def polymarket_props_today() -> Tuple[List[Prop], List[dict]]:
    """
    Devuelve (props, events_info).
    props: lista Prop con over+under para cada market parseado.
    events_info: lista con {title, startDate, id} de los eventos.
    """
    sid = pm_find_nba_series_id()
    if not sid:
        return [], []

    events = pm_events_next_window(sid, lookahead_hours=PM_LOOKAHEAD_HOURS, limit=PM_MAX_EVENTS)
    if not events:
        return [], []

    props: List[Prop] = []
    ev_info = []
    seen = set()

    for e in events:
        eid = str(e.get("id") or "")
        title = e.get("title") or e.get("name") or e.get("ticker") or f"event {eid}"
        start = e.get("startDate") or e.get("start_date") or e.get("startTime") or e.get("start_time") or ""
        ev_info.append({"id": eid, "title": title, "startDate": start})

        if not eid:
            continue

        try:
            markets = pm_markets_for_event(eid, limit=PM_MAX_MARKETS)
        except Exception:
            continue

        for m in markets:
            # strings típicos: question / title
            text = m.get("question") or m.get("title") or m.get("subtitle") or ""
            parsed = try_parse_prop_from_text(text)
            if not parsed:
                continue

            player, tipo, line = parsed
            market_id = str(m.get("id") or "")

            # over+under
            for side in ("over", "under"):
                key = (player.lower(), tipo, float(line), side, eid, market_id)
                if key in seen:
                    continue
                seen.add(key)
                props.append(Prop(
                    player=player,
                    tipo=tipo,
                    line=float(line),
                    side=side,
                    source="polymarket",
                    event=title,
                    market_id=market_id,
                ))

    return props, ev_info


# =========================
# Commands
# =========================
HELP_TEXT = (
    "🧠 *NBA Interactive Bot*\n\n"
    "Comandos:\n"
    "• `/odds`  → trae props P/R/A desde Polymarket y los devuelve en JSON\n"
    "• `/live`  → estado en vivo (usa Polymarket; si no hay, usa props.json)\n"
    "• `/start` → activa background scan cada 120s\n"
)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)

async def cmd_odds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        props, events = polymarket_props_today()
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error Polymarket: {e}")
        return

    if not props:
        await update.message.reply_text("No pude parsear props P/R/A desde Polymarket (0 encontrados).")
        return

    # agrupa JSON por evento
    by_event: Dict[str, List[dict]] = {}
    for p in props:
        ev = p.event or "NBA"
        by_event.setdefault(ev, []).append({
            "player": p.player,
            "tipo": p.tipo,
            "line": p.line,
            "side": p.side,
            "market_id": p.market_id,
        })

    payload = {
        "series_id": pm_find_nba_series_id(),
        "events": events[:25],
        "props": by_event,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "note": "Precios omitidos a propósito (cambian).",
    }

    txt = json.dumps(payload, ensure_ascii=False, indent=2)
    # Telegram puede truncar: dividimos si hace falta
    if len(txt) <= 3500:
        await update.message.reply_text(f"```json\n{txt}\n```", parse_mode=ParseMode.MARKDOWN)
    else:
        # manda en chunks
        chunks = [txt[i:i+3000] for i in range(0, len(txt), 3000)]
        await update.message.reply_text("📦 JSON muy largo — lo mando en partes.", parse_mode=ParseMode.MARKDOWN)
        for c in chunks[:6]:  # hard cap por seguridad
            await update.message.reply_text(f"```json\n{c}\n```", parse_mode=ParseMode.MARKDOWN)

async def cmd_live(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 1) intentar Polymarket (auto)
    props: List[Prop] = []
    try:
        props, _ = polymarket_props_today()
    except Exception:
        props = []

    # 2) fallback manual
    if not props:
        props = load_props()

    if not props:
        await update.message.reply_text("No pude cargar props (ni props.json ni Polymarket).")
        return

    # mapa player->props
    by_player: Dict[str, List[Prop]] = {}
    for p in props:
        by_player.setdefault(p.player, []).append(p)

    # games live
    try:
        games = scoreboard.ScoreBoard().get_dict()["scoreboard"]["games"]
    except Exception as e:
        await update.message.reply_text(f"⚠️ No pude leer scoreboard: {e}")
        return

    # mapa pid -> name (solo para jugadores en props)
    pid_map: Dict[int, str] = {}
    for name in by_player.keys():
        pid = get_pid_for_name(name)
        if pid:
            pid_map[pid] = name

    out = []
    for g in games:
        if g.get("gameStatus") != 2:
            continue

        gid = g.get("gameId")
        status = g.get("gameStatusText", "")
        period = int(g.get("period", 0) or 0)
        clock = g.get("gameClock", "") or ""

        try:
            box = boxscore.BoxScore(gid).get_dict()["game"]
        except Exception:
            continue

        for team_key in ["homeTeam", "awayTeam"]:
            for pl in box.get(team_key, {}).get("players", []):
                pid = pl.get("personId")
                if pid not in pid_map:
                    continue

                name = pid_map[pid]
                stats = pl.get("statistics", {})
                pts = stats.get("points", 0) or 0
                reb = stats.get("reboundsTotal", 0) or 0
                ast = stats.get("assists", 0) or 0
                mins = parse_minutes(stats.get("minutes", ""))

                ps = by_player.get(name, [])
                if not ps:
                    continue

                out.append(f"🏀 *{name}* — {status} (Q{period} {clock}) | MIN {mins:.1f}")
                for pr in ps:
                    actual = pts if pr.tipo == "puntos" else (reb if pr.tipo == "rebotes" else ast)
                    if pr.side == "over":
                        need = pr.line - actual
                        out.append(f"  • {pr.tipo} OVER {pr.line}: {actual} (faltan {need:.1f})")
                    else:
                        cushion = pr.line - actual
                        out.append(f"  • {pr.tipo} UNDER {pr.line}: {actual} (colchón {cushion:.1f})")
                out.append("")

    if not out:
        await update.message.reply_text("No hay partidos en vivo o ninguno de esos jugadores está jugando ahora.")
        return

    msg = "\n".join(out)
    if len(msg) > 3500:
        msg = msg[:3500] + "\n…(recortado)"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


# =========================
# Background scan + alerts (usa Polymarket o manual)
# =========================
async def background_scan(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id

    # props auto
    props: List[Prop] = []
    try:
        props, _ = polymarket_props_today()
    except Exception:
        props = []

    # fallback manual
    if not props:
        props = load_props()

    if not props:
        return

    state = load_alert_state()

    # pid -> props
    by_pid: Dict[int, List[Prop]] = {}
    for p in props:
        pid = get_pid_for_name(p.player)
        if pid:
            by_pid.setdefault(pid, []).append(p)

    # games
    try:
        games = scoreboard.ScoreBoard().get_dict()["scoreboard"]["games"]
    except Exception:
        return

    for g in games:
        if g.get("gameStatus") != 2:
            continue

        gid = g.get("gameId")
        status = g.get("gameStatusText", "")

        home = g.get("homeTeam", {})
        away = g.get("awayTeam", {})
        try:
            diff = abs(int(home.get("score", 0)) - int(away.get("score", 0)))
        except Exception:
            diff = 0

        is_clutch = diff <= 8
        is_blowout = diff >= BLOWOUT_IS

        period = int(g.get("period", 0) or 0)
        game_clock = g.get("gameClock", "") or ""
        clock_sec = clock_to_seconds(game_clock)
        elapsed_min = game_elapsed_minutes(period, clock_sec)

        try:
            box = boxscore.BoxScore(gid).get_dict()["game"]
        except Exception:
            continue

        for team_key in ["homeTeam", "awayTeam"]:
            for pl in box.get(team_key, {}).get("players", []):
                pid = pl.get("personId")
                if pid not in by_pid:
                    continue

                name = by_pid[pid][0].player
                s = pl.get("statistics", {})
                pts = s.get("points", 0) or 0
                reb = s.get("reboundsTotal", 0) or 0
                ast = s.get("assists", 0) or 0
                pf = s.get("foulsPersonal", 0) or 0
                mins = parse_minutes(s.get("minutes", ""))

                for pr in by_pid[pid]:
                    actual = pts if pr.tipo == "puntos" else (reb if pr.tipo == "rebotes" else ast)

                    pre, meta = pre_score(pid, pr.tipo, pr.line, pr.side)

                    if pr.side == "over":
                        faltante = float(pr.line) - float(actual)
                        lo_over, hi_over = THRESH_POINTS_OVER if pr.tipo == "puntos" else THRESH_REB_AST_OVER

                        if not (lo_over <= faltante <= hi_over):
                            continue
                        if should_gate_by_minutes("over", pr.tipo, mins, elapsed_min, is_blowout):
                            continue
                        if diff >= BLOWOUT_STRONG:
                            if (pr.tipo == "puntos" and faltante > 1.0) or (pr.tipo != "puntos" and faltante > 0.8):
                                continue

                        live = compute_over_score(pr.tipo, faltante, mins, pf, period, clock_sec, diff, is_clutch, is_blowout)
                        final = int(clamp(0.55 * live + 0.45 * pre, 0, 100))
                        key = f"{gid}|{pid}|{pr.tipo}|{pr.side}|{pr.line}"

                        if final >= FINAL_ALERT_THRESHOLD or (is_clutch and final >= FINAL_ALERT_THRESHOLD_CLUTCH):
                            if can_send_alert(state, key):
                                msg = (
                                    f"🎯 *ALERTA OVER* | *FINAL* `{final}/100` (LIVE {live} | PRE {pre})\n"
                                    f"👤 *{name}*\n"
                                    f"📊 {pr.tipo.upper()} {actual}/{pr.line} (faltan {faltante:.1f})\n"
                                    f"⏱️ {status} | Q{period} {game_clock}\n"
                                    f"🧠 MIN {mins:.1f} | PF {pf} | Diff {diff}\n"
                                    f"📈 Forma: hit5 {meta['hits5']}/{meta['n5']} | hit10 {meta['hits10']}/{meta['n10']}\n"
                                )
                                await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)

                    else:
                        margin_under = float(pr.line) - float(actual)
                        if should_gate_by_minutes("under", pr.tipo, mins, elapsed_min, is_blowout):
                            continue

                        live = compute_under_score(pr.tipo, margin_under, mins, pf, period, clock_sec, diff, is_clutch, is_blowout)
                        final = int(clamp(0.65 * live + 0.35 * pre, 0, 100))
                        key = f"{gid}|{pid}|{pr.tipo}|{pr.side}|{pr.line}"

                        if final >= FINAL_ALERT_THRESHOLD or (is_blowout and final >= FINAL_ALERT_THRESHOLD_CLUTCH):
                            if can_send_alert(state, key):
                                msg = (
                                    f"🧊 *ALERTA UNDER* | *FINAL* `{final}/100` (LIVE {live} | PRE {pre})\n"
                                    f"👤 *{name}*\n"
                                    f"📊 {pr.tipo.upper()} {actual}/{pr.line} (colchón {margin_under:.1f})\n"
                                    f"⏱️ {status} | Q{period} {game_clock}\n"
                                    f"🧠 MIN {mins:.1f} | PF {pf} | Diff {diff}\n"
                                    f"📈 Forma: hit5 {meta['hits5']}/{meta['n5']} | hit10 {meta['hits10']}/{meta['n10']}\n"
                                )
                                await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)

    save_alert_state(state)


# =========================
# Main
# =========================
async def on_startup(app: Application):
    log.info("Bot listo. /start para activar scan background.")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # /start registra job + muestra help
    async def register_job(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        jobs = context.job_queue.get_jobs_by_name(f"scan:{chat_id}")
        if not jobs:
            context.job_queue.run_repeating(
                background_scan,
                interval=POLL_SECONDS,
                first=5,
                chat_id=chat_id,
                name=f"scan:{chat_id}",
            )
            await update.message.reply_text(f"✅ Background scan activado (cada {POLL_SECONDS}s).")
        await cmd_start(update, context)

    app.add_handler(CommandHandler("start", register_job), group=0)
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("odds", cmd_odds))
    app.add_handler(CommandHandler("live", cmd_live))

    app.post_init = on_startup
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
