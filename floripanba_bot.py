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
from datetime import datetime, timezone, date

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from nba_api.live.nba.endpoints import scoreboard, boxscore
from nba_api.stats.static import players

# =========================
# LOGGING
# =========================
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("nba-bot")

# =========================
# CONFIG (Railway env vars)
# =========================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
if not TELEGRAM_TOKEN:
    raise RuntimeError("Missing TELEGRAM_TOKEN (Railway Variables)")

POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "120"))

# NBA season for gamelog (stats.nba.com)
SEASON = os.environ.get("NBA_SEASON", "2025-26").strip()

# Alert thresholds
FINAL_ALERT_THRESHOLD = int(os.environ.get("FINAL_ALERT_THRESHOLD", "75"))
FINAL_ALERT_THRESHOLD_CLUTCH = int(os.environ.get("FINAL_ALERT_THRESHOLD_CLUTCH", "68"))

COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN_SECONDS", str(8 * 60)))  # per prop key
BLOWOUT_IS = int(os.environ.get("BLOWOUT_IS", "20"))
BLOWOUT_STRONG = int(os.environ.get("BLOWOUT_STRONG", "22"))

# Near-line windows (LIVE)
THRESH_POINTS_OVER = (0.5, 4.0)
THRESH_REB_AST_OVER = (0.5, 1.5)

MIN_MINUTES_POINTS = 10.0
MIN_MINUTES_REB_AST = 14.0

# PRE scoring columns
STAT_COL = {"puntos": "PTS", "rebotes": "REB", "asistencias": "AST"}
STD_CAP = {"puntos": 8.0, "rebotes": 4.0, "asistencias": 3.0}
MARGIN_CAP = {"puntos": 8.0, "rebotes": 3.0, "asistencias": 3.0}

# Files
PROPS_FILE = "props.json"
ALERTS_STATE_FILE = "alerts_state.json"
IDS_CACHE_FILE = "player_ids_cache.json"
GLOG_CACHE_FILE = "gamelog_cache.json"
POLY_CACHE_FILE = "polymarket_cache.json"
STARTERS_STATE_FILE = "starters_state.json"
PREGAME_STATE_FILE = "pregame_state.json"

# PRE-game notifications
PREGAME_WINDOW_MINUTES = int(os.environ.get("PREGAME_WINDOW_MINUTES", "90"))
PREGAME_TOLERANCE_MIN = int(os.environ.get("PREGAME_TOLERANCE_MIN", "6"))
PREGAME_UPDATE_EVERY_SECONDS = int(os.environ.get("PREGAME_UPDATE_EVERY_SECONDS", str(60 * 60)))

# Polymarket
GAMMA = "https://gamma-api.polymarket.com"
POLY_GAME_TAG_ID = os.environ.get("POLY_GAME_TAG_ID", "100639").strip()  # “game bets” tag (común)
POLY_CACHE_TTL = int(os.environ.get("POLY_CACHE_TTL", "300"))  # 5 min
POLY_MAX_EVENTS = int(os.environ.get("POLY_MAX_EVENTS", "120"))

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

def clamp(x, lo=0, hi=100):
    return max(lo, min(hi, x))

# =========================
# Data model
# =========================
@dataclass
class Prop:
    player: str
    tipo: str          # puntos|rebotes|asistencias
    line: float
    side: str          # over|under
    source: str = "manual"  # manual|polymarket
    market_id: Optional[str] = None
    event_id: Optional[str] = None
    event_title: Optional[str] = None
    added_by: Optional[int] = None
    added_at: Optional[int] = None

# =========================
# Manual props (props.json)
# =========================
def load_manual_props() -> List[Prop]:
    raw = load_json(PROPS_FILE, {"props": []})
    out: List[Prop] = []
    for p in raw.get("props", []):
        try:
            out.append(Prop(**p))
        except Exception:
            continue
    return out

def save_manual_props(props: List[Prop]):
    save_json(PROPS_FILE, {"props": [asdict(p) for p in props]})

# =========================
# Player ID cache
# =========================
def obtener_id_jugador(nombre: str) -> Optional[int]:
    time.sleep(0.25 + random.random() * 0.25)
    res = players.find_players_by_full_name(nombre)
    if not res:
        return None
    exact = [p for p in res if (p.get("full_name") or "").lower() == nombre.lower()]
    pick = exact[0] if exact else res[0]
    try:
        return int(pick.get("id"))
    except Exception:
        return None

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
GLOG_TTL_SECONDS = int(os.environ.get("GLOG_TTL_SECONDS", str(6 * 60 * 60)))  # 6h

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

    time.sleep(0.6 + random.random() * 0.5)

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
        "hits5": h5, "n5": n5, "hits10": h10, "n10": n10,
        "avg5": round(avg5, 2), "avg10": round(avg10, 2),
        "std10": round(std10, 2), "w_margin": round(w_margin, 2),
    }
    return PRE, meta

# =========================
# NBA Live helpers
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
# LIVE scoring
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
# Alert state / cooldown
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
# Polymarket: discover NBA props
# =========================
def poly_get_nba_series_id() -> Optional[str]:
    """
    GET /sports => lista objetos con fields:
      sport, tags (csv), series (string id)
    """
    try:
        r = SESSION_PM.get(f"{GAMMA}/sports", timeout=20)
        r.raise_for_status()
        arr = r.json() or []
        for s in arr:
            sport = str(s.get("sport", "")).strip().lower()
            if sport == "nba" or "nba" in sport:
                series = str(s.get("series", "")).strip()
                if series:
                    return series
        return None
    except Exception:
        return None

def poly_cache_load():
    return load_json(POLY_CACHE_FILE, {"ts": 0, "props": [], "raw_count": 0})

def poly_cache_save(d):
    save_json(POLY_CACHE_FILE, d)

STAT_ALIASES = {
    "points": "puntos",
    "point": "puntos",
    "pts": "puntos",
    "puntos": "puntos",
    "rebounds": "rebotes",
    "rebound": "rebotes",
    "reb": "rebotes",
    "rebotes": "rebotes",
    "assists": "asistencias",
    "assist": "asistencias",
    "ast": "asistencias",
    "asistencias": "asistencias",
}

def parse_prop_from_question(q: str) -> Optional[Tuple[str, str, str, float]]:
    """
    Acepta cosas tipo:
      "Caris LeVert. Points. Over 6.5"
      "Chet Holmgren assists O1.5"
      "Duncan Robinson rebounds o2.5 u2.5"  (esto lo tratamos como 2 props)
    Pero Polymarket suele tener un market por lado (Over/Under).
    """
    if not q:
        return None
    s = " ".join(str(q).replace("\n", " ").split())
    sl = s.lower()

    # intenta capturar: player ... (points/rebounds/assists) ... (over/under/o/u) ... line
    # soporta separadores ".", "-", ":".
    m = re.search(
        r"^(?P<player>[A-Za-zÀ-ÿ' \-\.]+?)\s*[\.\:\-]\s*(?P<stat>points|puntos|rebounds|rebotes|assists|asistencias|pts|reb|ast)\s*[\.\:\-]?\s*(?P<side>over|under|o|u)\s*(?P<line>\d+(\.\d+)?)",
        s,
        flags=re.IGNORECASE,
    )
    if not m:
        # formato alterno: "Player points over/under 6.5"
        m = re.search(
            r"^(?P<player>[A-Za-zÀ-ÿ' \-\.]+?)\s+(?P<stat>points|puntos|rebounds|rebotes|assists|asistencias|pts|reb|ast)\s+(?P<side>over|under|o|u)\s*(?P<line>\d+(\.\d+)?)",
            s,
            flags=re.IGNORECASE,
        )
    if not m:
        return None

    player = m.group("player").strip().replace("  ", " ")
    stat_raw = m.group("stat").strip().lower()
    side_raw = m.group("side").strip().lower()
    line = float(m.group("line"))

    tipo = STAT_ALIASES.get(stat_raw, None)
    if not tipo:
        return None

    side = "over" if side_raw in ("over", "o") else "under"
    return player, tipo, side, line

def fetch_polymarket_nba_props_today() -> List[Prop]:
    """
    Estrategia robusta:
      1) GET /sports -> NBA series_id
      2) GET /events?series_id=NBA&tag_id=GAME&active=true&closed=false&order=startDate&ascending=true
      3) Recorrer event.markets[] y parsear questions a props P/R/A
    """
    cache = poly_cache_load()
    if (now_ts() - int(cache.get("ts", 0))) < POLY_CACHE_TTL:
        props = []
        for p in cache.get("props", []):
            try:
                props.append(Prop(**p))
            except Exception:
                pass
        return props

    series_id = os.environ.get("POLY_SERIES_ID", "").strip() or poly_get_nba_series_id()
    if not series_id:
        # fallback: sin NBA series_id no podemos filtrar bien
        poly_cache_save({"ts": now_ts(), "props": [], "raw_count": 0, "err": "no_series_id"})
        return []

    params = {
        "active": "true",
        "closed": "false",
        "series_id": series_id,
        "limit": str(POLY_MAX_EVENTS),
        "order": "startDate",
        "ascending": "true",
    }
    if POLY_GAME_TAG_ID:
        params["tag_id"] = POLY_GAME_TAG_ID

    try:
        r = SESSION_PM.get(f"{GAMMA}/events", params=params, timeout=25)
        r.raise_for_status()
        events = r.json() or []
    except Exception as e:
        poly_cache_save({"ts": now_ts(), "props": [], "raw_count": 0, "err": str(e)})
        return []

    today_utc = datetime.now(timezone.utc).date()
    out: List[Prop] = []
    raw_markets_count = 0

    for ev in events:
        # filtro "hoy" usando startDate si existe, si no lo dejamos pasar
        start = ev.get("startDate") or ev.get("start_date") or ev.get("startTime") or ev.get("start_time")
        if start:
            try:
                dt = datetime.fromisoformat(str(start).replace("Z", "+00:00")).astimezone(timezone.utc)
                if dt.date() != today_utc:
                    continue
            except Exception:
                pass

        ev_id = str(ev.get("id") or "")
        ev_title = str(ev.get("title") or ev.get("slug") or "NBA Event")

        markets = ev.get("markets") or []
        if not isinstance(markets, list):
            continue

        for m in markets:
            raw_markets_count += 1
            q = m.get("question") or m.get("title") or ""
            mid = str(m.get("id") or "")

            parsed = parse_prop_from_question(str(q))
            if not parsed:
                continue

            player, tipo, side, line = parsed
            if tipo not in ("puntos", "rebotes", "asistencias"):
                continue

            out.append(
                Prop(
                    player=player,
                    tipo=tipo,
                    side=side,
                    line=float(line),
                    source="polymarket",
                    market_id=mid,
                    event_id=ev_id,
                    event_title=ev_title,
                )
            )

    # dedupe
    dedup: Dict[str, Prop] = {}
    for p in out:
        k = f"{p.player.lower()}|{p.tipo}|{p.side}|{p.line}|{p.event_id}"
        if k not in dedup:
            dedup[k] = p

    props = list(dedup.values())
    poly_cache_save({"ts": now_ts(), "props": [asdict(p) for p in props], "raw_count": raw_markets_count})
    return props

# =========================
# Combined props source
# =========================
def get_all_props() -> List[Prop]:
    manual = load_manual_props()
    poly = fetch_polymarket_nba_props_today()
    # si quieres SOLO polymarket, deja manual fuera
    return manual + poly

# =========================
# Pregame / starters states
# =========================
def load_pregame_state():
    return load_json(PREGAME_STATE_FILE, {})

def save_pregame_state(st):
    save_json(PREGAME_STATE_FILE, st)

def load_starters_state():
    return load_json(STARTERS_STATE_FILE, {})

def save_starters_state(st):
    save_json(STARTERS_STATE_FILE, st)

def parse_game_start_utc(game_obj: dict) -> Optional[datetime]:
    candidates = [
        game_obj.get("gameTimeUTC"),
        game_obj.get("gameTimeUtc"),
        game_obj.get("gameTimeISO"),
        game_obj.get("gameTime"),
        game_obj.get("gameTimeUTCString"),
    ]
    for c in candidates:
        if not c:
            continue
        s = str(c)
        try:
            if s.endswith("Z"):
                return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            continue
    return None

def minutes_to_start(game_obj: dict) -> Optional[float]:
    start = parse_game_start_utc(game_obj)
    if not start:
        return None
    now = datetime.now(timezone.utc)
    return (start - now).total_seconds() / 60.0

def in_window(m: float, target: float, tol: float):
    return (target - tol) <= m <= (target + tol)

def should_send_hourly_update(state: dict, key: str, every_seconds: int):
    now = int(time.time())
    last = int(state.get(key, 0))
    if last == 0 or (now - last) >= every_seconds:
        state[key] = now
        return True
    return False

# =========================
# Commands
# =========================
HELP_TEXT = (
    "🧠 *NBA Interactive Bot*\n\n"
    "Comandos:\n"
    "• `/today` → partidos NBA de hoy (status + record si está)\n"
    "• `/odds`  → props P/R/A de Polymarket (NBA hoy)\n"
    "• `/live`  → estado en vivo + top oportunidades (auto Polymarket)\n\n"
    "Opcional (manual):\n"
    "• `/add Nombre | tipo | side | linea`\n"
    "   Ej: `/add Nikola Jokic | asistencias | under | 9.5`\n"
)

def parse_add(text: str) -> Optional[Prop]:
    body = text.strip()
    body = re.sub(r"^/add(@\w+)?\s*", "", body).strip()
    if "|" not in body:
        return None
    parts = [p.strip() for p in body.split("|")]
    if len(parts) != 4:
        return None
    name, tipo, side, line_s = parts
    tipo = tipo.lower()
    side = side.lower()
    if tipo not in ("puntos", "rebotes", "asistencias"):
        return None
    if side not in ("over", "under"):
        return None
    try:
        line = float(line_s)
    except Exception:
        return None
    return Prop(player=name, tipo=tipo, side=side, line=line, source="manual")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    p = parse_add(update.message.text or "")
    if not p:
        await update.message.reply_text("Formato inválido.\n\n" + HELP_TEXT, parse_mode=ParseMode.MARKDOWN)
        return

    pid = get_pid_for_name(p.player)
    if not pid:
        await update.message.reply_text(f"⚠️ No pude encontrar al jugador: {p.player}")
        return

    p.added_by = update.effective_user.id if update.effective_user else None
    p.added_at = now_ts()

    props = load_manual_props()
    for e in props:
        if (e.player.lower() == p.player.lower() and e.tipo == p.tipo and e.side == p.side and float(e.line) == float(p.line)):
            await update.message.reply_text("✅ Ya estaba agregado.")
            return
    props.append(p)
    save_manual_props(props)
    await update.message.reply_text(f"✅ Agregado (manual): {p.player} — {p.tipo.upper()} {p.side.upper()} {p.line}")

async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        games = scoreboard.ScoreBoard().get_dict()["scoreboard"]["games"]
    except Exception as e:
        await update.message.reply_text(f"⚠️ No pude leer scoreboard: {e}")
        return

    if not games:
        await update.message.reply_text("No hay partidos hoy (o el endpoint no devolvió juegos).")
        return

    lines = ["📅 *NBA hoy*"]
    for g in games:
        home = g.get("homeTeam", {}) or {}
        away = g.get("awayTeam", {}) or {}
        ht = home.get("teamName") or home.get("teamCity") or "HOME"
        at = away.get("teamName") or away.get("teamCity") or "AWAY"

        # records (si existen en el payload)
        hw = home.get("wins")
        hl = home.get("losses")
        aw = away.get("wins")
        al = away.get("losses")
        hrec = f"{hw}-{hl}" if hw is not None and hl is not None else "?"
        arec = f"{aw}-{al}" if aw is not None and al is not None else "?"

        status = g.get("gameStatusText", "") or ""
        lines.append(f"• *{at}* ({arec}) @ *{ht}* ({hrec}) — `{status}`")

    msg = "\n".join(lines)
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def cmd_odds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    props = fetch_polymarket_nba_props_today()
    if not props:
        await update.message.reply_text("No pude parsear props P/R/A desde Polymarket (0 encontrados).")
        return

    # agrupa por evento
    by_event: Dict[str, List[Prop]] = {}
    for p in props:
        by_event.setdefault(p.event_title or "NBA", []).append(p)

    # output (limita)
    lines = ["🟣 *Polymarket — Props P/R/A (NBA hoy)*"]
    shown = 0
    for ev, ps in list(by_event.items())[:12]:
        lines.append(f"\n🏀 *{ev}*")
        ps_sorted = sorted(ps, key=lambda x: (x.player.lower(), x.tipo, x.side, x.line))
        for p in ps_sorted[:30]:
            lines.append(f"• {p.player} — {p.tipo} {p.side} {p.line}")
            shown += 1
            if shown >= 120:
                break
        if shown >= 120:
            break

    msg = "\n".join(lines)
    if len(msg) > 3800:
        msg = msg[:3800] + "\n…(recortado)"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def cmd_live(update: Update, context: ContextTypes.DEFAULT_TYPE):
    props = get_all_props()
    if not props:
        await update.message.reply_text("No pude cargar props (ni props.json ni Polymarket).")
        return

    # games live
    try:
        games = scoreboard.ScoreBoard().get_dict()["scoreboard"]["games"]
    except Exception as e:
        await update.message.reply_text(f"⚠️ No pude leer scoreboard: {e}")
        return

    # map pid -> props
    by_pid: Dict[int, List[Prop]] = {}
    for p in props:
        pid = get_pid_for_name(p.player)
        if pid:
            by_pid.setdefault(pid, []).append(p)

    out = []
    scored_candidates = []  # (final, line)

    for g in games:
        if g.get("gameStatus") != 2:
            continue

        gid = g.get("gameId")
        status = g.get("gameStatusText", "") or ""
        period = int(g.get("period", 0) or 0)
        clock = g.get("gameClock", "") or ""

        home = g.get("homeTeam", {}) or {}
        away = g.get("awayTeam", {}) or {}
        diff = abs(int(home.get("score", 0) or 0) - int(away.get("score", 0) or 0))
        is_clutch = diff <= 8
        is_blowout = diff >= BLOWOUT_IS
        clock_sec = clock_to_seconds(clock)
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

                s = pl.get("statistics", {}) or {}
                pts = s.get("points", 0) or 0
                reb = s.get("reboundsTotal", 0) or 0
                ast = s.get("assists", 0) or 0
                pf = s.get("foulsPersonal", 0) or 0
                mins = parse_minutes(s.get("minutes", ""))

                name = by_pid[pid][0].player
                out.append(f"🏀 *{name}* — {status} (Q{period} {clock}) | MIN {mins:.1f}")

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
                        scored_candidates.append((final, f"• {name} {pr.tipo} OVER {pr.line} | FINAL {final}/100 (LIVE {live} PRE {pre}) | faltan {faltante:.1f} | hit10 {meta['hits10']}/{meta['n10']}"))

                    else:
                        margin_under = float(pr.line) - float(actual)
                        if should_gate_by_minutes("under", pr.tipo, mins, elapsed_min, is_blowout):
                            continue

                        live = compute_under_score(pr.tipo, margin_under, mins, pf, period, clock_sec, diff, is_clutch, is_blowout)
                        final = int(clamp(0.65 * live + 0.35 * pre, 0, 100))
                        scored_candidates.append((final, f"• {name} {pr.tipo} UNDER {pr.line} | FINAL {final}/100 (LIVE {live} PRE {pre}) | colchón {margin_under:.1f} | hit10 {meta['hits10']}/{meta['n10']}"))

                out.append("")

    if not out:
        # no games live: devolvemos watchlist pregame top PRE
        watch = []
        poly_props = fetch_polymarket_nba_props_today()
        # score solo pre (forma) para top
        for pr in poly_props[:250]:
            pid = get_pid_for_name(pr.player)
            if not pid:
                continue
            pre, meta = pre_score(pid, pr.tipo, pr.line, pr.side)
            if pre >= 70:
                watch.append((pre, f"• {pr.player} {pr.tipo} {pr.side} {pr.line} | PRE {pre}/100 | hit10 {meta['hits10']}/{meta['n10']}"))
        watch.sort(key=lambda x: x[0], reverse=True)
        lines = ["🧠 *No hay partidos en vivo ahora.*", "👀 *Watchlist PRE (forma 5/10)*"]
        for _, s in watch[:25]:
            lines.append(s)
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
        return

    # además: top oportunidades live
    scored_candidates.sort(key=lambda x: x[0], reverse=True)
    top = [s for _, s in scored_candidates[:15]]
    msg = "\n".join(out)
    if top:
        msg = msg + "\n\n🔥 *Top oportunidades (FINAL)*\n" + "\n".join(top)

    if len(msg) > 3800:
        msg = msg[:3800] + "\n…(recortado)"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

# =========================
# Background scan: polymarket + manual
# =========================
async def background_scan(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    props = get_all_props()
    if not props:
        return

    state = load_alert_state()
    starters_state = load_starters_state()
    pregame_state = load_pregame_state()

    try:
        games = scoreboard.ScoreBoard().get_dict()["scoreboard"]["games"]
    except Exception:
        return

    # PRE-GAME notifications (T-90)
    for g in games:
        if g.get("gameStatus") != 1:
            continue
        m = minutes_to_start(g)
        if m is None:
            continue
        if not (0 < m <= PREGAME_WINDOW_MINUTES):
            continue

        game_id = g.get("gameId")
        home = (g.get("homeTeam") or {}).get("teamName", "HOME")
        away = (g.get("awayTeam") or {}).get("teamName", "AWAY")
        status_text = g.get("gameStatusText", "")

        # destacado (simple): el primero que caiga en ventana lo marcamos
        key_feature = f"{game_id}:featured"
        if key_feature not in pregame_state:
            pregame_state[key_feature] = now_ts()
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"⭐ *Partido destacado (ventana pregame)*\n🏀 *{away} @ {home}*\n🕒 {status_text}",
                    parse_mode=ParseMode.MARKDOWN
                )
            except Exception:
                pass

        # T-90
        if in_window(m, 90, PREGAME_TOLERANCE_MIN):
            key90 = f"{game_id}:t90"
            if key90 not in pregame_state:
                pregame_state[key90] = now_ts()
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"⏳ *PRE-GAME T-90*\n🏀 *{away} @ {home}*\n🕒 {status_text}\n📌 Empieza en ~1h30.",
                        parse_mode=ParseMode.MARKDOWN
                    )
                except Exception:
                    pass

        # hourly update in window
        keyH = f"{game_id}:hourly"
        if should_send_hourly_update(pregame_state, keyH, PREGAME_UPDATE_EVERY_SECONDS):
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"📣 *PRE-GAME UPDATE*\n🏀 *{away} @ {home}*\n🕒 {status_text}\n⏱️ Faltan aprox: `{int(round(m))} min`",
                    parse_mode=ParseMode.MARKDOWN
                )
            except Exception:
                pass

    save_pregame_state(pregame_state)

    # pid -> props
    by_pid: Dict[int, List[Prop]] = {}
    for p in props:
        pid = get_pid_for_name(p.player)
        if pid:
            by_pid.setdefault(pid, []).append(p)

    for g in games:
        if g.get("gameStatus") != 2:
            continue

        gid = g.get("gameId")
        status = g.get("gameStatusText", "") or ""
        period = int(g.get("period", 0) or 0)
        clock = g.get("gameClock", "") or ""

        home = g.get("homeTeam", {}) or {}
        away = g.get("awayTeam", {}) or {}
        diff = abs(int(home.get("score", 0) or 0) - int(away.get("score", 0) or 0))

        is_clutch = diff <= 8
        is_blowout = diff >= BLOWOUT_IS
        clock_sec = clock_to_seconds(clock)
        elapsed_min = game_elapsed_minutes(period, clock_sec)

        try:
            box = boxscore.BoxScore(gid).get_dict()["game"]
        except Exception:
            continue

        # STARTERS alert once per game (cuando ya hay boxscore)
        st_key = f"{gid}:starters"
        if st_key not in starters_state:
            starters_state[st_key] = now_ts()
            try:
                starters_msg = [f"🧾 *Starters detectados* — `{status}`"]
                for tk in ["awayTeam", "homeTeam"]:
                    team = box.get(tk, {}) or {}
                    tname = team.get("teamName", tk)
                    pls = team.get("players", []) or []
                    starters = []
                    for p in pls:
                        if str(p.get("starter", "0")) == "1":
                            starters.append(p.get("name") or p.get("firstName") or "")
                    if starters:
                        starters_msg.append(f"• *{tname}*: " + ", ".join(starters[:5]))
                await context.bot.send_message(chat_id=chat_id, text="\n".join(starters_msg), parse_mode=ParseMode.MARKDOWN)
            except Exception:
                pass

        for team_key in ["homeTeam", "awayTeam"]:
            for pl in box.get(team_key, {}).get("players", []):
                pid = pl.get("personId")
                if pid not in by_pid:
                    continue

                s = pl.get("statistics", {}) or {}
                pts = s.get("points", 0) or 0
                reb = s.get("reboundsTotal", 0) or 0
                ast = s.get("assists", 0) or 0
                pf = s.get("foulsPersonal", 0) or 0
                mins = parse_minutes(s.get("minutes", ""))

                name = by_pid[pid][0].player

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

                        key = f"{gid}|{pid}|{pr.tipo}|{pr.side}|{pr.line}|{pr.source}|{pr.market_id or ''}"
                        if final >= FINAL_ALERT_THRESHOLD or (is_clutch and final >= FINAL_ALERT_THRESHOLD_CLUTCH):
                            if can_send_alert(state, key):
                                src = "PM" if pr.source == "polymarket" else "MAN"
                                msg = (
                                    f"🎯 *ALERTA OVER* | *FINAL* `{final}/100` (LIVE {live} | PRE {pre}) [{src}]\n"
                                    f"👤 *{name}*\n"
                                    f"📊 {pr.tipo.upper()} {actual}/{pr.line} (faltan {faltante:.1f})\n"
                                    f"⏱️ {status} | Q{period} {clock}\n"
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

                        key = f"{gid}|{pid}|{pr.tipo}|{pr.side}|{pr.line}|{pr.source}|{pr.market_id or ''}"
                        if final >= FINAL_ALERT_THRESHOLD or (is_blowout and final >= FINAL_ALERT_THRESHOLD_CLUTCH):
                            if can_send_alert(state, key):
                                src = "PM" if pr.source == "polymarket" else "MAN"
                                msg = (
                                    f"🧊 *ALERTA UNDER* | *FINAL* `{final}/100` (LIVE {live} | PRE {pre}) [{src}]\n"
                                    f"👤 *{name}*\n"
                                    f"📊 {pr.tipo.upper()} {actual}/{pr.line} (colchón {margin_under:.1f})\n"
                                    f"⏱️ {status} | Q{period} {clock}\n"
                                    f"🧠 MIN {mins:.1f} | PF {pf} | Diff {diff}\n"
                                    f"📈 Forma: hit5 {meta['hits5']}/{meta['n5']} | hit10 {meta['hits10']}/{meta['n10']}\n"
                                )
                                await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)

    save_alert_state(state)
    save_starters_state(starters_state)

# =========================
# Startup / job registration
# =========================
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
    await cmd_help(update, context)

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", register_job))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("add", cmd_add))

    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("odds", cmd_odds))
    app.add_handler(CommandHandler("live", cmd_live))

    log.info("Bot running…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
