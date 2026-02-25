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
from datetime import datetime, timezone, timedelta, date

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
# LOGGING
# =========================
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("nba-bot")


# =========================
# ENV / CONFIG
# =========================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
if not TELEGRAM_TOKEN:
    raise RuntimeError("Missing TELEGRAM_TOKEN (Railway Variables / env var).")

POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "120"))
SEASON = os.environ.get("NBA_SEASON", "2025-26")

# files
ALERTS_STATE_FILE = "alerts_state.json"
IDS_CACHE_FILE = "player_ids_cache.json"
GLOG_CACHE_FILE = "gamelog_cache.json"

# scoring thresholds
FINAL_ALERT_THRESHOLD = 75
FINAL_ALERT_THRESHOLD_CLUTCH = 68
COOLDOWN_SECONDS = 8 * 60
BLOWOUT_IS = 20
BLOWOUT_STRONG = 22

# Near-line windows (LIVE)
THRESH_POINTS_OVER = (0.5, 4.0)
THRESH_REB_AST_OVER = (0.5, 1.5)

MIN_MINUTES_POINTS = 10.0
MIN_MINUTES_REB_AST = 14.0

# PRE scoring columns
STAT_COL = {"puntos": "PTS", "rebotes": "REB", "asistencias": "AST"}
STD_CAP = {"puntos": 8.0, "rebotes": 4.0, "asistencias": 3.0}
MARGIN_CAP = {"puntos": 8.0, "rebotes": 3.0, "asistencias": 3.0}

# Polymarket Gamma
GAMMA = "https://gamma-api.polymarket.com"
NBA_SERIES_ID_ENV = os.environ.get("PM_NBA_SERIES_ID", "").strip()  # opcional override

# Pregame
PREGAME_T90_MIN = 90
PREGAME_TOL_MIN = 8
PREGAME_STATE_FILE = "pregame_state.json"
PREGAME_TOP_K = 10  # top players por PRE para vigilar


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
    tipo: str          # "puntos" | "rebotes" | "asistencias"
    line: float
    side: str          # "over" | "under"
    event_id: Optional[str] = None
    event_name: Optional[str] = None
    start_time_utc: Optional[str] = None  # ISO string

def prop_key(p: Prop) -> str:
    return f"{p.player.lower()}|{p.tipo}|{p.side}|{p.line}"


# =========================
# Player ID cache
# =========================
def obtener_id_jugador(nombre: str) -> Optional[int]:
    time.sleep(0.2 + random.random() * 0.2)
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
# Gamelog cache + fetch
# =========================
GLOG_TTL_SECONDS = 6 * 60 * 60  # 6h

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
# PRE scoring (forma 5/10)
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
# LIVE score
# =========================
def should_gate_by_minutes(side: str, tipo: str, value: float, mins: float, elapsed_min, is_blowout: bool) -> bool:
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
# Alert state
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
# Pregame state
# =========================
def load_pregame_state():
    return load_json(PREGAME_STATE_FILE, {})

def save_pregame_state(st):
    save_json(PREGAME_STATE_FILE, st)

def in_window(m: float, target: float, tol: float) -> bool:
    return (target - tol) <= m <= (target + tol)


# =========================
# Polymarket discovery (ROBUST)
# =========================
def pm_get_json(path: str, params: dict) -> dict:
    url = f"{GAMMA}{path}"
    r = SESSION_PM.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json() if r.content else {}

def pm_find_nba_series_id() -> Optional[str]:
    # allow override
    if NBA_SERIES_ID_ENV:
        return NBA_SERIES_ID_ENV

    data = pm_get_json("/sports", {})
    # /sports suele devolver lista
    if isinstance(data, list):
        sports_list = data
    else:
        sports_list = data.get("sports", []) or data.get("data", []) or []

    best = None
    for s in sports_list:
        # keys típicas: sport, name, series, series_id, etc.
        sport_key = (s.get("sport") or s.get("key") or s.get("slug") or "").lower()
        name = (s.get("name") or "").lower()
        if "nba" in sport_key or name.strip() == "nba":
            best = s
            break

    if not best:
        # fallback: busca por "basketball" + "nba"
        for s in sports_list:
            name = (s.get("name") or "").lower()
            if "nba" in name:
                best = s
                break

    if not best:
        return None

    # series id puede venir en distintos campos
    sid = best.get("series_id") or best.get("seriesId")
    if sid:
        return str(sid)

    # a veces "series" es array; elige el que contenga nba
    series = best.get("series") or []
    if isinstance(series, list) and series:
        # intenta campo id
        for it in series:
            if isinstance(it, dict):
                _id = it.get("id") or it.get("series_id") or it.get("seriesId")
                if _id:
                    return str(_id)

    return None

def pm_events_next_24h(series_id: str, limit: int = 200) -> List[dict]:
    # Trae eventos activos del league, ordenados por startTime
    params = {
        "series_id": series_id,
        "active": "true",
        "closed": "false",
        "order": "startTime",
        "ascending": "true",
        "limit": str(limit),
        "offset": "0",
    }
    data = pm_get_json("/events", params)
    # /events suele devolver lista directamente
    if isinstance(data, list):
        return data
    return data.get("events", []) or data.get("data", []) or []

def parse_iso_dt(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        # suele venir con Z
        if s.endswith("Z"):
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None

def pm_filter_today_or_24h(events: List[dict]) -> List[dict]:
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=24)
    out = []
    for e in events:
        st = parse_iso_dt(e.get("startTime") or e.get("start_time") or e.get("start_time_utc") or "")
        if not st:
            continue
        if now - timedelta(hours=2) <= st <= horizon:
            out.append(e)
    return out

_STAT_MAP = {
    "points": "puntos",
    "point": "puntos",
    "rebounds": "rebotes",
    "rebound": "rebotes",
    "assists": "asistencias",
    "assist": "asistencias",
    "pts": "puntos",
    "reb": "rebotes",
    "ast": "asistencias",
}

def _norm_stat_from_text(t: str) -> Optional[str]:
    tl = (t or "").lower()
    for k, v in _STAT_MAP.items():
        if re.search(rf"\b{k}\b", tl):
            return v
    return None

def pm_market_to_props(m: dict, event_meta: dict) -> List[Prop]:
    """
    Soporta dos formatos:
      A) question incluye Over/Under y línea
      B) question incluye stat + outcomes incluyen Over X / Under X
    """
    q = (m.get("question") or m.get("title") or "").strip()
    ql = q.lower()

    event_id = str(event_meta.get("id") or "")
    event_name = (event_meta.get("title") or event_meta.get("name") or "").strip()
    st = (event_meta.get("startTime") or "").strip()

    out: List[Prop] = []

    # A) explícito: "Chet Holmgren assists over 1.5"
    rx = re.compile(
        r"^(?P<player>.+?)\s+(?P<stat>points|rebounds|assists)\s+(?P<side>over|under)\s+(?P<line>\d+(?:\.\d+)?)\b",
        re.IGNORECASE,
    )
    m1 = rx.search(q)
    if m1:
        player = m1.group("player").strip()
        tipo = _STAT_MAP.get(m1.group("stat").lower())
        side = m1.group("side").lower()
        line = float(m1.group("line"))
        if tipo:
            out.append(Prop(player=player, tipo=tipo, side=side, line=line, event_id=event_id, event_name=event_name, start_time_utc=st))
        return out

    # otro estilo con guiones: "Player - Assists - Over 1.5"
    rx2 = re.compile(
        r"^(?P<player>.+?)\s*[-:]\s*(?P<stat>points|rebounds|assists)\s*[-:]\s*(?P<side>over|under)\s*(?P<line>\d+(?:\.\d+)?)\b",
        re.IGNORECASE,
    )
    m2 = rx2.search(q)
    if m2:
        player = m2.group("player").strip()
        tipo = _STAT_MAP.get(m2.group("stat").lower())
        side = m2.group("side").lower()
        line = float(m2.group("line"))
        if tipo:
            out.append(Prop(player=player, tipo=tipo, side=side, line=line, event_id=event_id, event_name=event_name, start_time_utc=st))
        return out

    # B) outcomes contienen Over/Under X; q contiene stat
    tipo = _norm_stat_from_text(q)
    outcomes_raw = m.get("outcomes")
    try:
        outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else (outcomes_raw or [])
    except Exception:
        outcomes = outcomes_raw or []

    if tipo and isinstance(outcomes, list) and outcomes:
        # busca "Over 1.5" / "Under 1.5"
        rx_out = re.compile(r"^(Over|Under)\s*(\d+(?:\.\d+)?)$", re.IGNORECASE)
        parsed = []
        for o in outcomes:
            if not isinstance(o, str):
                continue
            mo = rx_out.match(o.strip())
            if mo:
                side = mo.group(1).lower()
                line = float(mo.group(2))
                parsed.append((side, line))
        if parsed:
            # player: intenta derivarlo del question removiendo el stat
            player = q
            # recorta por stat word
            player = re.sub(r"\b(points|rebounds|assists)\b.*$", "", player, flags=re.IGNORECASE).strip(" -:")
            if not player:
                player = q.split(" ")[0].strip()

            for side, line in parsed:
                out.append(Prop(player=player, tipo=tipo, side=side, line=line, event_id=event_id, event_name=event_name, start_time_utc=st))
            return out

    return out

def polymarket_props_pra_next24h() -> List[Prop]:
    sid = pm_find_nba_series_id()
    if not sid:
        return []
    events = pm_events_next_24h(sid, limit=200)
    events = pm_filter_today_or_24h(events)

    props: List[Prop] = []
    for e in events:
        markets = e.get("markets") or []
        if not isinstance(markets, list):
            continue
        for m in markets:
            props.extend(pm_market_to_props(m, e))

    # dedupe
    seen = set()
    uniq = []
    for p in props:
        k = prop_key(p)
        if k in seen:
            continue
        seen.add(k)
        uniq.append(p)
    return uniq


# =========================
# NBA schedule helper (records if present)
# =========================
def nba_games_today_str() -> str:
    try:
        games = scoreboard.ScoreBoard().get_dict()["scoreboard"]["games"]
    except Exception:
        return "No pude leer el scoreboard de NBA."

    lines = []
    for g in games:
        # status 1 pregame, 2 live, 3 final
        home = g.get("homeTeam", {}) or {}
        away = g.get("awayTeam", {}) or {}
        hn = home.get("teamName", "HOME")
        an = away.get("teamName", "AWAY")
        hs = home.get("score", 0)
        as_ = away.get("score", 0)
        st = g.get("gameStatusText", "")
        # a veces vienen wins/losses
        hw = home.get("wins")
        hl = home.get("losses")
        aw = away.get("wins")
        al = away.get("losses")
        rec_h = f" ({hw}-{hl})" if hw is not None and hl is not None else ""
        rec_a = f" ({aw}-{al})" if aw is not None and al is not None else ""
        if int(g.get("gameStatus", 0) or 0) == 2:
            lines.append(f"🏀 {an}{rec_a} @ {hn}{rec_h} — *LIVE* {as_}-{hs} | {st}")
        else:
            lines.append(f"🏀 {an}{rec_a} @ {hn}{rec_h} — {st}")

    if not lines:
        return "No aparecen partidos hoy en el scoreboard."
    return "📅 *NBA hoy*\n" + "\n".join(lines)


# =========================
# Commands
# =========================
HELP_TEXT = (
    "🧠 *NBA Interactive Bot*\n\n"
    "Comandos:\n"
    "• `/odds`  → props P/R/A (Polymarket) para próximas 24h\n"
    "• `/live`  → estado en vivo de esos props (si hay juegos live)\n"
    "• `/today` → programación NBA de hoy (y records si vienen)\n\n"
    "Auto:\n"
    "• Scan cada 120s → alerta si FINAL ≥ 75\n"
    "• Pregame T-90 → partido destacado + jugadores en racha\n"
)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)

async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(nba_games_today_str(), parse_mode=ParseMode.MARKDOWN)

async def cmd_odds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    props = polymarket_props_pra_next24h()
    if not props:
        await update.message.reply_text("No pude parsear props P/R/A desde Polymarket (0 encontrados).")
        return

    # agrupa por evento y limita
    by_event: Dict[str, List[Prop]] = {}
    for p in props:
        by_event.setdefault(p.event_name or "NBA", []).append(p)

    lines = ["🟣 *Polymarket — Props P/R/A (próx. 24h)*"]
    shown = 0
    for ev, plist in list(by_event.items())[:8]:
        lines.append(f"\n*{ev}*")
        # muestra hasta 10 por evento
        for p in plist[:10]:
            stat = "PTS" if p.tipo == "puntos" else ("REB" if p.tipo == "rebotes" else "AST")
            lines.append(f"• {p.player} — {stat} {p.side.upper()} {p.line}")
            shown += 1
            if shown >= 60:
                break
        if shown >= 60:
            break

    msg = "\n".join(lines)
    if len(msg) > 3800:
        msg = msg[:3800] + "\n…(recortado)"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def cmd_live(update: Update, context: ContextTypes.DEFAULT_TYPE):
    props = polymarket_props_pra_next24h()
    if not props:
        await update.message.reply_text("No pude cargar props de Polymarket (0).")
        return

    # pid -> list props
    by_pid: Dict[int, List[Prop]] = {}
    for p in props:
        pid = get_pid_for_name(p.player)
        if pid:
            by_pid.setdefault(pid, []).append(p)

    if not by_pid:
        await update.message.reply_text("No pude resolver IDs de jugadores para esos props.")
        return

    try:
        games = scoreboard.ScoreBoard().get_dict()["scoreboard"]["games"]
    except Exception as e:
        await update.message.reply_text(f"⚠️ No pude leer scoreboard: {e}")
        return

    out = []
    for g in games:
        if int(g.get("gameStatus", 0) or 0) != 2:
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
                if pid not in by_pid:
                    continue

                stats = pl.get("statistics", {})
                pts = stats.get("points", 0) or 0
                reb = stats.get("reboundsTotal", 0) or 0
                ast = stats.get("assists", 0) or 0
                mins = parse_minutes(stats.get("minutes", ""))

                plist = by_pid[pid]
                name = plist[0].player
                out.append(f"🏀 *{name}* — {status} (Q{period} {clock}) | MIN {mins:.1f}")
                for pr in plist[:6]:
                    actual = pts if pr.tipo == "puntos" else (reb if pr.tipo == "rebotes" else ast)
                    if pr.side == "over":
                        need = pr.line - actual
                        out.append(f"  • {pr.tipo.upper()} OVER {pr.line}: {actual} (faltan {need:.1f})")
                    else:
                        cushion = pr.line - actual
                        out.append(f"  • {pr.tipo.upper()} UNDER {pr.line}: {actual} (colchón {cushion:.1f})")
                out.append("")

    if not out:
        await update.message.reply_text("No hay partidos en vivo ahora (o ningún jugador con props está jugando).")
        return

    msg = "\n".join(out)
    if len(msg) > 3800:
        msg = msg[:3800] + "\n…(recortado)"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


# =========================
# Pregame notifications (T-90)
# =========================
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

async def pregame_t90_if_needed(context: ContextTypes.DEFAULT_TYPE):
    # toma juegos pregame del scoreboard
    try:
        games = scoreboard.ScoreBoard().get_dict()["scoreboard"]["games"]
    except Exception:
        return

    st = load_pregame_state()
    props = polymarket_props_pra_next24h()

    # si no hay props, igual manda partidos del día
    for g in games:
        if int(g.get("gameStatus", 0) or 0) != 1:
            continue

        m = minutes_to_start(g)
        if m is None:
            continue

        if not in_window(m, PREGAME_T90_MIN, PREGAME_TOL_MIN):
            continue

        gid = g.get("gameId", "")
        key = f"{gid}:t90"
        if key in st:
            continue
        st[key] = now_ts()

        home = (g.get("homeTeam") or {}).get("teamName", "HOME")
        away = (g.get("awayTeam") or {}).get("teamName", "AWAY")
        status_text = g.get("gameStatusText", "")

        # "partido destacado": el event con más props (proxy)
        featured = None
        if props:
            # heurística: el evento con más props
            counts: Dict[str, int] = {}
            for p in props:
                if p.event_name:
                    counts[p.event_name] = counts.get(p.event_name, 0) + 1
            if counts:
                featured = sorted(counts.items(), key=lambda x: x[1], reverse=True)[0][0]

        # top racha: PRE score alto en próximos props
        hot_lines = []
        if props:
            # calcula PRE para muestra (cap)
            scored = []
            for p in props[:80]:  # limita para no matar rate limits
                pid = get_pid_for_name(p.player)
                if not pid:
                    continue
                pre, meta = pre_score(pid, p.tipo, p.line, p.side)
                scored.append((pre, p, meta))
                time.sleep(0.05)
            scored.sort(key=lambda x: x[0], reverse=True)
            for pre, p, meta in scored[:PREGAME_TOP_K]:
                stat = "PTS" if p.tipo == "puntos" else ("REB" if p.tipo == "rebotes" else "AST")
                hot_lines.append(f"• {p.player} — {stat} {p.side.upper()} {p.line} | PRE {pre}/100 | hit10 {meta['hits10']}/{meta['n10']}")

        msg = (
            f"⏳ *PRE-GAME T-90*\n"
            f"🏀 *{away} @ {home}*\n"
            f"🕒 {status_text}\n"
        )
        if featured:
            msg += f"\n⭐ *Partido destacado (hoy):* {featured}\n"
        if hot_lines:
            msg += "\n🔥 *Jugadores en racha (según props Polymarket + últimos 10):*\n" + "\n".join(hot_lines[:10])

        await context.bot.send_message(chat_id=context.job.chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)

    save_pregame_state(st)


# =========================
# Background scan
# =========================
async def background_scan(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id

    props = polymarket_props_pra_next24h()
    if not props:
        return

    state = load_alert_state()

    # pid -> props
    by_pid: Dict[int, List[Prop]] = {}
    for p in props[:120]:  # cap total props monitoreados por scan
        pid = get_pid_for_name(p.player)
        if pid:
            by_pid.setdefault(pid, []).append(p)

    if not by_pid:
        return

    # Pregame T-90 (1 vez por juego)
    await pregame_t90_if_needed(context)

    # games
    try:
        games = scoreboard.ScoreBoard().get_dict()["scoreboard"]["games"]
    except Exception:
        return

    for g in games:
        if int(g.get("gameStatus", 0) or 0) != 2:
            continue

        gid = g.get("gameId")
        status = g.get("gameStatusText", "")

        home = g.get("homeTeam", {}) or {}
        away = g.get("awayTeam", {}) or {}
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

                s = pl.get("statistics", {}) or {}
                pts = s.get("points", 0) or 0
                reb = s.get("reboundsTotal", 0) or 0
                ast = s.get("assists", 0) or 0
                pf = s.get("foulsPersonal", 0) or 0
                mins = parse_minutes(s.get("minutes", ""))

                for pr in by_pid[pid]:
                    actual = pts if pr.tipo == "puntos" else (reb if pr.tipo == "rebotes" else ast)

                    # PRE score (forma)
                    pre, meta = pre_score(pid, pr.tipo, pr.line, pr.side)

                    if pr.side == "over":
                        faltante = float(pr.line) - float(actual)
                        lo_over, hi_over = THRESH_POINTS_OVER if pr.tipo == "puntos" else THRESH_REB_AST_OVER

                        if not (lo_over <= faltante <= hi_over):
                            continue
                        if should_gate_by_minutes("over", pr.tipo, faltante, mins, elapsed_min, is_blowout):
                            continue
                        if diff >= BLOWOUT_STRONG:
                            if (pr.tipo == "puntos" and faltante > 1.0) or (pr.tipo != "puntos" and faltante > 0.8):
                                continue

                        live = compute_over_score(pr.tipo, faltante, mins, pf, period, clock_sec, diff, is_clutch, is_blowout)
                        final = int(clamp(0.55 * live + 0.45 * pre, 0, 100))
                        key = f"{gid}|{pid}|{pr.tipo}|{pr.side}|{pr.line}"

                        if final >= FINAL_ALERT_THRESHOLD or (is_clutch and final >= FINAL_ALERT_THRESHOLD_CLUTCH):
                            if can_send_alert(state, key):
                                stat = "PTS" if pr.tipo == "puntos" else ("REB" if pr.tipo == "rebotes" else "AST")
                                msg = (
                                    f"🎯 *ALERTA OVER* | *FINAL* `{final}/100` (LIVE {live} | PRE {pre})\n"
                                    f"👤 *{pr.player}*\n"
                                    f"📊 {stat} {actual}/{pr.line} (faltan {faltante:.1f})\n"
                                    f"⏱️ {status} | Q{period} {game_clock}\n"
                                    f"🧠 MIN {mins:.1f} | PF {pf} | Diff {diff}\n"
                                    f"📈 Forma: hit5 {meta['hits5']}/{meta['n5']} | hit10 {meta['hits10']}/{meta['n10']}\n"
                                )
                                await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)

                    else:  # UNDER
                        margin_under = float(pr.line) - float(actual)
                        if should_gate_by_minutes("under", pr.tipo, margin_under, mins, elapsed_min, is_blowout):
                            continue
                        live = compute_under_score(pr.tipo, margin_under, mins, pf, period, clock_sec, diff, is_clutch, is_blowout)
                        final = int(clamp(0.65 * live + 0.35 * pre, 0, 100))
                        key = f"{gid}|{pid}|{pr.tipo}|{pr.side}|{pr.line}"

                        if final >= FINAL_ALERT_THRESHOLD or (is_blowout and final >= FINAL_ALERT_THRESHOLD_CLUTCH):
                            if can_send_alert(state, key):
                                stat = "PTS" if pr.tipo == "puntos" else ("REB" if pr.tipo == "rebotes" else "AST")
                                msg = (
                                    f"🧊 *ALERTA UNDER* | *FINAL* `{final}/100` (LIVE {live} | PRE {pre})\n"
                                    f"👤 *{pr.player}*\n"
                                    f"📊 {stat} {actual}/{pr.line} (colchón {margin_under:.1f})\n"
                                    f"⏱️ {status} | Q{period} {game_clock}\n"
                                    f"🧠 MIN {mins:.1f} | PF {pf} | Diff {diff}\n"
                                    f"📈 Forma: hit5 {meta['hits5']}/{meta['n5']} | hit10 {meta['hits10']}/{meta['n10']}\n"
                                )
                                await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)

    save_alert_state(state)


# =========================
# Startup / main
# =========================
async def on_startup(app: Application):
    log.info("Bot arrancado.")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # /start registers the repeating job for this chat
    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
            await update.message.reply_text("✅ Background scan activado (cada 120s).")
        await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("odds", cmd_odds))
    app.add_handler(CommandHandler("live", cmd_live))

    app.post_init = on_startup
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
