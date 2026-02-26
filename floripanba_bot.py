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
from datetime import date

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

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
if not TELEGRAM_TOKEN:
    raise RuntimeError("Missing TELEGRAM_TOKEN env var")

POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "120"))

PROPS_FILE = "props.json"
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

GAMMA = "https://gamma-api.polymarket.com"

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

PM_HEADERS = {
    "User-Agent": NBA_HEADERS["User-Agent"],
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Origin": "https://polymarket.com",
    "Referer": "https://polymarket.com/",
    "Connection": "keep-alive",
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
SESSION_PM = build_session(PM_HEADERS)

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
    tipo: str                # "puntos" | "rebotes" | "asistencias"
    line: float
    side: str                # "over" | "under"
    source: str = "manual"
    game_slug: Optional[str] = None
    market_id: Optional[str] = None
    added_by: Optional[int] = None
    added_at: Optional[int] = None

def load_props() -> List[Prop]:
    raw = load_json(PROPS_FILE, {"props": []})
    out = []
    for p in raw.get("props", []):
        try:
            out.append(Prop(**p))
        except Exception:
            continue
    return out

def save_props(props: List[Prop]):
    save_json(PROPS_FILE, {"props": [asdict(p) for p in props]})

# =========================
# Player ID cache
# =========================
def obtener_id_jugador(nombre: str) -> Optional[int]:
    time.sleep(0.2 + random.random() * 0.1)
    res = players.find_players_by_full_name(nombre)
    if not res:
        return None
    exact = [p for p in res if (p.get("full_name") or "").lower() == nombre.lower()]
    pick = exact[0] if exact else res[0]
    return int(pick.get("id"))

def load_ids_cache() -> Dict[str, int]:
    return load_json(IDS_CACHE_FILE, {})

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

    time.sleep(0.5 + random.random() * 0.25)

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
# PRE SCORE
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
        "hit5": round(hit5, 2), "hit10": round(hit10, 2),
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
# LIVE SCORE
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
# Polymarket — helpers de matching
# =========================
PM_CACHE = {"ts": 0, "date": None, "props": []}
PM_TTL_SECONDS = 8 * 60

_TIPO_MAP = {"points": "puntos", "rebounds": "rebotes", "assists": "asistencias"}

# Regex: "Isaiah Hartenstein: Assists O/U 3.5"  OR  "Player Name Points O/U 22.5"
_PM_Q_RE = re.compile(
    r"^(?P<player>.+?)(?::\s*|\s+)(?P<stat>Points|Rebounds|Assists)\s*O\/U\s*(?P<line>\d+(?:\.\d+)?)",
    re.IGNORECASE,
)

# Mapa tricode NBA → nombres posibles en slugs de Polymarket
_TRICODE_TO_SLUG_NAMES = {
    "ATL": ["atlanta", "hawks", "atl"],
    "BOS": ["boston", "celtics", "bos"],
    "BKN": ["brooklyn", "nets", "bkn", "bk"],
    "CHA": ["charlotte", "hornets", "cha"],
    "CHI": ["chicago", "bulls", "chi"],
    "CLE": ["cleveland", "cavaliers", "cavs", "cle"],
    "DAL": ["dallas", "mavericks", "mavs", "dal"],
    "DEN": ["denver", "nuggets", "den"],
    "DET": ["detroit", "pistons", "det"],
    "GSW": ["golden-state", "golden_state", "warriors", "gsw", "gs"],
    "HOU": ["houston", "rockets", "hou"],
    "IND": ["indiana", "pacers", "ind"],
    "LAC": ["la-clippers", "clippers", "lac"],
    "LAL": ["la-lakers", "lakers", "lal", "la"],
    "MEM": ["memphis", "grizzlies", "mem"],
    "MIA": ["miami", "heat", "mia"],
    "MIL": ["milwaukee", "bucks", "mil"],
    "MIN": ["minnesota", "timberwolves", "wolves", "min"],
    "NOP": ["new-orleans", "pelicans", "nop", "no"],
    "NYK": ["new-york", "knicks", "nyk", "ny"],
    "OKC": ["oklahoma", "thunder", "okc"],
    "ORL": ["orlando", "magic", "orl"],
    "PHI": ["philadelphia", "76ers", "sixers", "phi"],
    "PHX": ["phoenix", "suns", "phx", "phx"],
    "POR": ["portland", "trail-blazers", "blazers", "por"],
    "SAC": ["sacramento", "kings", "sac"],
    "SAS": ["san-antonio", "spurs", "sas", "sa"],
    "TOR": ["toronto", "raptors", "tor"],
    "UTA": ["utah", "jazz", "uta"],
    "WAS": ["washington", "wizards", "was"],
}

def _slug_from_scoreboard_game(g: dict) -> str:
    away = (g.get("awayTeam", {}) or {}).get("teamTricode", "").lower()
    home = (g.get("homeTeam", {}) or {}).get("teamTricode", "").lower()
    d = date.today().isoformat()
    return f"nba-{away}-{home}-{d}"

def _event_matches_game(ev_slug: str, ev_title: str, away_tri: str, home_tri: str) -> bool:
    """Verifica si un evento de Polymarket corresponde a un partido NBA específico."""
    slug_l = ev_slug.lower()
    title_l = ev_title.lower()
    combined = slug_l + " " + title_l

    away_names = _TRICODE_TO_SLUG_NAMES.get(away_tri.upper(), [away_tri.lower()])
    home_names = _TRICODE_TO_SLUG_NAMES.get(home_tri.upper(), [home_tri.lower()])

    away_found = any(name in combined for name in away_names)
    home_found = any(name in combined for name in home_names)

    return away_found and home_found

def polymarket_fetch_all_nba_events() -> List[dict]:
    """Obtiene todos los eventos NBA activos de Polymarket usando paginación."""
    all_events = []
    limit = 100
    offset = 0

    # Intentamos varios tag slugs que Polymarket puede usar para NBA
    tag_slugs = ["nba", "basketball", "sports"]

    for tag in tag_slugs:
        offset = 0
        while True:
            url = f"{GAMMA}/events"
            params = {
                "tag_slug": tag,
                "closed": "false",
                "limit": limit,
                "offset": offset,
            }
            try:
                r = SESSION_PM.get(url, params=params, timeout=25)
                log.info(f"Polymarket /events tag={tag} offset={offset} → status {r.status_code}")
                if r.status_code != 200:
                    break

                data = r.json()
                events = data if isinstance(data, list) else data.get("events", [])

                if not events:
                    break

                all_events.extend(events)
                log.info(f"  → {len(events)} eventos (total acumulado: {len(all_events)})")

                if len(events) < limit:
                    break
                offset += limit

            except Exception as e:
                log.warning(f"Error fetching Polymarket events tag={tag}: {e}")
                break

        # Si ya encontramos eventos con nba, no seguimos
        if all_events:
            break

    # Dedupe por id
    seen_ids = set()
    unique = []
    for ev in all_events:
        eid = ev.get("id") or ev.get("slug")
        if eid not in seen_ids:
            seen_ids.add(eid)
            unique.append(ev)

    log.info(f"Total eventos únicos Polymarket: {len(unique)}")
    return unique

def polymarket_event_by_slug(slug: str) -> Optional[dict]:
    url = f"{GAMMA}/events/slug/{slug}"
    try:
        r = SESSION_PM.get(url, timeout=20)
        log.info(f"Polymarket slug lookup '{slug}' → status {r.status_code}")
        if r.status_code != 200:
            return None
        return r.json()
    except Exception as e:
        log.warning(f"polymarket_event_by_slug error: {e}")
        return None

def polymarket_event_markets(event_id: str) -> List[dict]:
    """Obtiene markets de un evento específico por ID."""
    url = f"{GAMMA}/markets"
    params = {"event_id": event_id, "limit": 200}
    try:
        r = SESSION_PM.get(url, params=params, timeout=20)
        if r.status_code != 200:
            return []
        data = r.json()
        return data if isinstance(data, list) else data.get("markets", [])
    except Exception as e:
        log.warning(f"polymarket_event_markets error: {e}")
        return []

def _parse_player_stat_from_market(m: dict) -> Tuple[Optional[str], Optional[str], Optional[float]]:
    """
    Extrae (player, stat_type, line) de un market de Polymarket.
    Retorna (None, None, None) si no aplica.
    """
    # Intentamos sportsMarketType primero
    smt = (m.get("sportsMarketType") or m.get("sport_market_type") or "").lower()

    # Si no hay sportsMarketType, intentamos inferir del título/pregunta
    q = (m.get("question") or m.get("title") or "").strip()

    if not smt:
        q_lower = q.lower()
        if "point" in q_lower:
            smt = "points"
        elif "rebound" in q_lower:
            smt = "rebounds"
        elif "assist" in q_lower:
            smt = "assists"

    if smt not in ("points", "rebounds", "assists"):
        return None, None, None

    # Intentamos parsear línea y jugador del campo "line"
    line_raw = m.get("line", None)
    player = None
    line_val = None

    # Caso 1: tiene campo "line" numérico
    if line_raw is not None:
        try:
            line_val = float(line_raw)
        except Exception:
            pass

    # Intentamos extraer jugador y línea de la pregunta
    mm = _PM_Q_RE.match(q)
    if mm:
        player = mm.group("player").strip()
        if line_val is None:
            try:
                line_val = float(mm.group("line"))
            except Exception:
                pass

    # Si no encontramos jugador con el regex, intentamos groupItemTitle
    if not player:
        git = (m.get("groupItemTitle") or m.get("group_item_title") or "").strip()
        if git:
            # groupItemTitle suele ser el nombre del jugador directamente
            player = git

    if not player or line_val is None:
        return None, None, None

    return player, smt, line_val

def polymarket_props_from_event(event_json: dict, fallback_slug: str = "") -> List[Prop]:
    out: List[Prop] = []
    event_slug = event_json.get("slug") or fallback_slug or str(event_json.get("id", "unknown"))

    # Los markets pueden estar embebidos en el evento o hay que buscarlos
    markets = event_json.get("markets", []) or []

    # Si el evento no tiene markets embebidos, los buscamos por event_id
    if not markets and event_json.get("id"):
        markets = polymarket_event_markets(str(event_json["id"]))

    log.info(f"Evento '{event_slug}': {len(markets)} markets a procesar")

    for m in markets:
        player, smt, line_val = _parse_player_stat_from_market(m)
        if not player or not smt or line_val is None:
            continue

        tipo = _TIPO_MAP.get(smt)
        if not tipo:
            continue

        market_id = str(m.get("id") or "")

        out.append(Prop(
            player=player, tipo=tipo, side="over", line=line_val,
            source="polymarket", game_slug=event_slug, market_id=market_id
        ))
        out.append(Prop(
            player=player, tipo=tipo, side="under", line=line_val,
            source="polymarket", game_slug=event_slug, market_id=market_id
        ))

    return out

# =========================
# FALLBACK PROPS (hoy: 2026-02-25)
# =========================
_FALLBACK_DATE = date.today().isoformat()   # Siempre activo como último recurso

FALLBACK_PROPS: List[Prop] = [
    # BOS @ DEN
    Prop("Jaylen Brown", "puntos", 28.5, "over", source="fallback", game_slug="nba-bos-den-2026-02-25"),
    Prop("Jaylen Brown", "puntos", 28.5, "under", source="fallback", game_slug="nba-bos-den-2026-02-25"),
    Prop("Nikola Jokić", "puntos", 27.5, "over", source="fallback", game_slug="nba-bos-den-2026-02-25"),
    Prop("Nikola Jokić", "puntos", 27.5, "under", source="fallback", game_slug="nba-bos-den-2026-02-25"),
    Prop("Jamal Murray", "puntos", 23.5, "over", source="fallback", game_slug="nba-bos-den-2026-02-25"),
    Prop("Jamal Murray", "puntos", 23.5, "under", source="fallback", game_slug="nba-bos-den-2026-02-25"),
    Prop("Payton Pritchard", "puntos", 18.5, "over", source="fallback", game_slug="nba-bos-den-2026-02-25"),
    Prop("Payton Pritchard", "puntos", 18.5, "under", source="fallback", game_slug="nba-bos-den-2026-02-25"),
    Prop("Derrick White", "puntos", 17.5, "over", source="fallback", game_slug="nba-bos-den-2026-02-25"),
    Prop("Derrick White", "puntos", 17.5, "under", source="fallback", game_slug="nba-bos-den-2026-02-25"),
    # CLE @ MIL
    Prop("Donovan Mitchell", "puntos", 26.5, "over", source="fallback", game_slug="nba-cle-mil-2026-02-25"),
    Prop("Donovan Mitchell", "puntos", 26.5, "under", source="fallback", game_slug="nba-cle-mil-2026-02-25"),
    Prop("James Harden", "puntos", 20.5, "over", source="fallback", game_slug="nba-cle-mil-2026-02-25"),
    Prop("James Harden", "puntos", 20.5, "under", source="fallback", game_slug="nba-cle-mil-2026-02-25"),
    Prop("Jarrett Allen", "puntos", 15.5, "over", source="fallback", game_slug="nba-cle-mil-2026-02-25"),
    Prop("Jarrett Allen", "puntos", 15.5, "under", source="fallback", game_slug="nba-cle-mil-2026-02-25"),
    Prop("Sam Merrill", "puntos", 11.5, "over", source="fallback", game_slug="nba-cle-mil-2026-02-25"),
    Prop("Sam Merrill", "puntos", 11.5, "under", source="fallback", game_slug="nba-cle-mil-2026-02-25"),
    Prop("Jaylon Tyson", "puntos", 11.5, "over", source="fallback", game_slug="nba-cle-mil-2026-02-25"),
    Prop("Jaylon Tyson", "puntos", 11.5, "under", source="fallback", game_slug="nba-cle-mil-2026-02-25"),
    # GSW @ MEM
    Prop("Al Horford", "rebotes", 6.5, "over", source="fallback", game_slug="nba-gsw-mem-2026-02-25"),
    Prop("Al Horford", "rebotes", 6.5, "under", source="fallback", game_slug="nba-gsw-mem-2026-02-25"),
    Prop("Moses Moody", "puntos", 18.5, "over", source="fallback", game_slug="nba-gsw-mem-2026-02-25"),
    Prop("Moses Moody", "puntos", 18.5, "under", source="fallback", game_slug="nba-gsw-mem-2026-02-25"),
    Prop("Brandin Podziemski", "puntos", 17.5, "over", source="fallback", game_slug="nba-gsw-mem-2026-02-25"),
    Prop("Brandin Podziemski", "puntos", 17.5, "under", source="fallback", game_slug="nba-gsw-mem-2026-02-25"),
    Prop("Ty Jerome", "puntos", 16.5, "over", source="fallback", game_slug="nba-gsw-mem-2026-02-25"),
    Prop("Ty Jerome", "puntos", 16.5, "under", source="fallback", game_slug="nba-gsw-mem-2026-02-25"),
    Prop("GG Jackson II", "puntos", 14.5, "over", source="fallback", game_slug="nba-gsw-mem-2026-02-25"),
    Prop("GG Jackson II", "puntos", 14.5, "under", source="fallback", game_slug="nba-gsw-mem-2026-02-25"),
    # OKC @ DET
    Prop("Isaiah Hartenstein", "asistencias", 3.5, "over", source="fallback", game_slug="nba-okc-det-2026-02-25"),
    Prop("Isaiah Hartenstein", "asistencias", 3.5, "under", source="fallback", game_slug="nba-okc-det-2026-02-25"),
    Prop("Daniss Jenkins", "asistencias", 2.5, "over", source="fallback", game_slug="nba-okc-det-2026-02-25"),
    Prop("Daniss Jenkins", "asistencias", 2.5, "under", source="fallback", game_slug="nba-okc-det-2026-02-25"),
    Prop("Chet Holmgren", "puntos", 17.5, "over", source="fallback", game_slug="nba-okc-det-2026-02-25"),
    Prop("Chet Holmgren", "puntos", 17.5, "under", source="fallback", game_slug="nba-okc-det-2026-02-25"),
    Prop("Isaiah Joe", "puntos", 14.5, "over", source="fallback", game_slug="nba-okc-det-2026-02-25"),
    Prop("Isaiah Joe", "puntos", 14.5, "under", source="fallback", game_slug="nba-okc-det-2026-02-25"),
    Prop("Cason Wallace", "puntos", 11.5, "over", source="fallback", game_slug="nba-okc-det-2026-02-25"),
    Prop("Cason Wallace", "puntos", 11.5, "under", source="fallback", game_slug="nba-okc-det-2026-02-25"),
    # SAC @ HOU
    Prop("Tari Eason", "rebotes", 7.5, "over", source="fallback", game_slug="nba-sac-hou-2026-02-25"),
    Prop("Tari Eason", "rebotes", 7.5, "under", source="fallback", game_slug="nba-sac-hou-2026-02-25"),
    Prop("Precious Achiuwa", "rebotes", 6.5, "over", source="fallback", game_slug="nba-sac-hou-2026-02-25"),
    Prop("Precious Achiuwa", "rebotes", 6.5, "under", source="fallback", game_slug="nba-sac-hou-2026-02-25"),
    Prop("Kevin Durant", "rebotes", 5.5, "over", source="fallback", game_slug="nba-sac-hou-2026-02-25"),
    Prop("Kevin Durant", "rebotes", 5.5, "under", source="fallback", game_slug="nba-sac-hou-2026-02-25"),
    Prop("Keegan Murray", "rebotes", 5.5, "over", source="fallback", game_slug="nba-sac-hou-2026-02-25"),
    Prop("Keegan Murray", "rebotes", 5.5, "under", source="fallback", game_slug="nba-sac-hou-2026-02-25"),
    Prop("Dorian Finney-Smith", "rebotes", 3.5, "over", source="fallback", game_slug="nba-sac-hou-2026-02-25"),
    Prop("Dorian Finney-Smith", "rebotes", 3.5, "under", source="fallback", game_slug="nba-sac-hou-2026-02-25"),
    # SAS @ TOR
    Prop("Scottie Barnes", "asistencias", 4.5, "over", source="fallback", game_slug="nba-sas-tor-2026-02-25"),
    Prop("Scottie Barnes", "asistencias", 4.5, "under", source="fallback", game_slug="nba-sas-tor-2026-02-25"),
    Prop("Brandon Ingram", "puntos", 21.5, "over", source="fallback", game_slug="nba-sas-tor-2026-02-25"),
    Prop("Brandon Ingram", "puntos", 21.5, "under", source="fallback", game_slug="nba-sas-tor-2026-02-25"),
    Prop("RJ Barrett", "puntos", 17.5, "over", source="fallback", game_slug="nba-sas-tor-2026-02-25"),
    Prop("RJ Barrett", "puntos", 17.5, "under", source="fallback", game_slug="nba-sas-tor-2026-02-25"),
    Prop("Scottie Barnes", "puntos", 17.5, "over", source="fallback", game_slug="nba-sas-tor-2026-02-25"),
    Prop("Scottie Barnes", "puntos", 17.5, "under", source="fallback", game_slug="nba-sas-tor-2026-02-25"),
    Prop("Scottie Barnes", "rebotes", 8.5, "over", source="fallback", game_slug="nba-sas-tor-2026-02-25"),
    Prop("Scottie Barnes", "rebotes", 8.5, "under", source="fallback", game_slug="nba-sas-tor-2026-02-25"),
]

# =========================
# Polymarket: función principal de carga
# =========================
def polymarket_props_today_from_scoreboard() -> List[Prop]:
    today = date.today().isoformat()
    now = now_ts()

    if PM_CACHE["date"] == today and (now - PM_CACHE["ts"]) < PM_TTL_SECONDS:
        log.info(f"PM_CACHE hit: {len(PM_CACHE['props'])} props")
        return PM_CACHE["props"]

    # Obtener partidos NBA de hoy
    try:
        games = scoreboard.ScoreBoard().get_dict()["scoreboard"]["games"]
    except Exception as e:
        log.warning(f"Scoreboard error: {e}")
        games = []

    if not games:
        log.warning("No hay partidos en el scoreboard de hoy")

    # Construir pares de tricodes para matching
    team_pairs = []
    for g in games:
        away_tri = (g.get("awayTeam", {}) or {}).get("teamTricode", "").upper()
        home_tri = (g.get("homeTeam", {}) or {}).get("teamTricode", "").upper()
        local_slug = _slug_from_scoreboard_game(g)
        if away_tri and home_tri:
            team_pairs.append((away_tri, home_tri, local_slug))

    log.info(f"Partidos hoy: {team_pairs}")

    props_all: List[Prop] = []

    # === ESTRATEGIA 1: Buscar por slug exacto ===
    for away_tri, home_tri, local_slug in team_pairs:
        ev = polymarket_event_by_slug(local_slug)
        if ev:
            log.info(f"✅ Slug exacto encontrado: {local_slug}")
            new_props = polymarket_props_from_event(ev, fallback_slug=local_slug)
            log.info(f"   → {len(new_props)//2} props de jugadores")
            props_all.extend(new_props)

    # === ESTRATEGIA 2: Buscar todos los eventos NBA y hacer matching por nombre ===
    if not props_all:
        log.info("Slug exacto falló, buscando por matching de nombres de equipos...")
        all_events = polymarket_fetch_all_nba_events()

        for ev in all_events:
            ev_slug = (ev.get("slug") or "").lower()
            ev_title = (ev.get("title") or ev.get("name") or "").lower()

            # Solo eventos de hoy (fecha en slug o titulo)
            today_short = today.replace("-", "")
            date_in_slug = today in ev_slug or today_short in ev_slug

            matched_local_slug = None
            for away_tri, home_tri, local_slug in team_pairs:
                if _event_matches_game(ev_slug, ev_title, away_tri, home_tri):
                    matched_local_slug = local_slug
                    log.info(f"✅ Match por nombre: '{ev_slug}' → {local_slug}")
                    break

            if not matched_local_slug:
                continue

            new_props = polymarket_props_from_event(ev, fallback_slug=matched_local_slug)
            log.info(f"   → {len(new_props)//2} props")
            props_all.extend(new_props)

    # === ESTRATEGIA 3: Buscar slugs alternativos para cada partido ===
    if not props_all:
        log.info("Probando slugs alternativos...")
        for away_tri, home_tri, local_slug in team_pairs:
            away_names = _TRICODE_TO_SLUG_NAMES.get(away_tri, [away_tri.lower()])
            home_names = _TRICODE_TO_SLUG_NAMES.get(home_tri, [home_tri.lower()])

            slug_candidates = []
            for an in away_names[:2]:
                for hn in home_names[:2]:
                    for separator in ["-", "_", ""]:
                        for prefix in ["nba-", ""]:
                            slug_candidates.append(f"{prefix}{an}{separator}{hn}{separator}{today}")
                            slug_candidates.append(f"{prefix}{an}{separator}vs{separator}{hn}{separator}{today}")
                            slug_candidates.append(f"{prefix}{an}{separator}at{separator}{hn}{separator}{today}")

            for slug_try in slug_candidates[:20]:  # límite para no abusar
                ev = polymarket_event_by_slug(slug_try)
                if ev:
                    log.info(f"✅ Slug alternativo encontrado: {slug_try}")
                    new_props = polymarket_props_from_event(ev, fallback_slug=local_slug)
                    props_all.extend(new_props)
                    break
                time.sleep(0.1)

    # Dedupe
    seen = set()
    uniq = []
    for p in props_all:
        k = (p.game_slug, p.player.lower(), p.tipo, p.side, float(p.line))
        if k not in seen:
            seen.add(k)
            uniq.append(p)

    # === FALLBACK FINAL: props hardcodeados ===
    if not uniq:
        log.warning("⚠️  Sin props de Polymarket — usando fallback hardcodeado")
        uniq = list(FALLBACK_PROPS)

    PM_CACHE["date"] = today
    PM_CACHE["ts"] = now
    PM_CACHE["props"] = uniq
    log.info(f"Props cargados: {len(uniq)} ({len(uniq)//2} jugadores/lineas)")
    return uniq

# =========================
# Commands
# =========================
HELP_TEXT = (
    "🧠 *NBA Interactive Bot*\n\n"
    "Comandos:\n"
    "• `/games`  → programación NBA de hoy\n"
    "• `/today`  → alias de /games\n"
    "• `/odds`   → props P/R/A de Polymarket (auto)\n"
    "   - `/odds nba-okc-det-2026-02-25`  (solo ese partido)\n"
    "   - `/odds Nikola Jokic`           (filtra por jugador)\n"
    "• `/live`   → top props en vivo (auto) + scoring\n"
    "• `/debug`  → info de debug de Polymarket\n\n"
    "Opcional manual:\n"
    "• `/add Nombre | tipo | side | linea`\n"
    "   Ej: `/add Jalen Duren | puntos | over | 15.5`\n"
    "   tipo: puntos / rebotes / asistencias\n"
    "   side: over / under\n"
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

    props = load_props()
    for existing in props:
        if (existing.player.lower() == p.player.lower()
            and existing.tipo == p.tipo and existing.side == p.side and float(existing.line) == float(p.line)):
            await update.message.reply_text("✅ Ya estaba agregado.")
            return

    props.append(p)
    save_props(props)
    await update.message.reply_text(f"✅ Agregado (manual):\n• {p.player} — {p.tipo.upper()} {p.side.upper()} {p.line}")

async def cmd_games(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        games = scoreboard.ScoreBoard().get_dict()["scoreboard"]["games"]
    except Exception as e:
        await update.message.reply_text(f"⚠️ No pude leer scoreboard: {e}")
        return

    if not games:
        await update.message.reply_text("No hay juegos detectados hoy.")
        return

    lines = ["📅 *NBA hoy*"]
    for g in games:
        away = g.get("awayTeam", {})
        home = g.get("homeTeam", {})
        at = away.get("teamTricode", "AWAY")
        ht = home.get("teamTricode", "HOME")
        ar = away.get("wins", None)
        al = away.get("losses", None)
        hr = home.get("wins", None)
        hl = home.get("losses", None)
        status = g.get("gameStatusText", "")
        rec_away = f"{ar}-{al}" if ar is not None and al is not None else ""
        rec_home = f"{hr}-{hl}" if hr is not None and hl is not None else ""
        slug = _slug_from_scoreboard_game(g)
        lines.append(f"• {at} ({rec_away}) @ {ht} ({rec_home}) — {status}\n  `slug: {slug}`")

    msg = "\n".join(lines)
    if len(msg) > 3800:
        msg = msg[:3800] + "\n…(recortado)"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando de debug para ver qué está pasando con Polymarket."""
    lines = ["🔍 *DEBUG Polymarket*\n"]

    # Partidos de hoy
    try:
        games = scoreboard.ScoreBoard().get_dict()["scoreboard"]["games"]
        lines.append(f"📅 Partidos NBA hoy: {len(games)}")
        for g in games:
            slug = _slug_from_scoreboard_game(g)
            lines.append(f"  • `{slug}`")
    except Exception as e:
        lines.append(f"❌ Error scoreboard: {e}")
        games = []

    # Test slug exacto con el primer partido
    if games:
        test_slug = _slug_from_scoreboard_game(games[0])
        lines.append(f"\n🔗 Test slug exacto: `{test_slug}`")
        ev = polymarket_event_by_slug(test_slug)
        if ev:
            mkt_count = len(ev.get("markets", []) or [])
            lines.append(f"  ✅ Encontrado! {mkt_count} markets")
        else:
            lines.append(f"  ❌ No encontrado en Polymarket")

    # Test búsqueda general
    lines.append(f"\n🌐 Buscando eventos NBA en Polymarket...")
    try:
        url = f"{GAMMA}/events"
        r = SESSION_PM.get(url, params={"tag_slug": "nba", "closed": "false", "limit": 5}, timeout=15)
        lines.append(f"  Status: {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            evs = data if isinstance(data, list) else data.get("events", [])
            lines.append(f"  Eventos encontrados: {len(evs)}")
            for ev in evs[:3]:
                lines.append(f"  • `{ev.get('slug', 'sin-slug')}`")
        else:
            lines.append(f"  Body: {r.text[:200]}")
    except Exception as e:
        lines.append(f"  ❌ Error: {e}")

    # Props cargados en cache
    props = PM_CACHE.get("props", [])
    lines.append(f"\n📦 Props en cache: {len(props)}")
    if props:
        fuentes = {}
        for p in props:
            fuentes[p.source] = fuentes.get(p.source, 0) + 1
        for src, cnt in fuentes.items():
            lines.append(f"  • {src}: {cnt}")

    msg = "\n".join(lines)
    if len(msg) > 3800:
        msg = msg[:3800] + "\n…"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

def _group_props_pretty(props_pm: List[Prop]) -> Dict[str, Dict[str, Dict[Tuple[str, float], Dict[str, bool]]]]:
    out: Dict[str, Dict[str, Dict[Tuple[str, float], Dict[str, bool]]]] = {}
    for p in props_pm:
        slug = p.game_slug or "unknown"
        out.setdefault(slug, {})
        out[slug].setdefault(p.player, {})
        key = (p.tipo, float(p.line))
        out[slug][p.player].setdefault(key, {"over": False, "under": False})
        if p.side in ("over", "under"):
            out[slug][p.player][key][p.side] = True
    return out

async def cmd_odds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Cargando props de Polymarket...", parse_mode=ParseMode.MARKDOWN)

    props_pm = polymarket_props_today_from_scoreboard()
    if not props_pm:
        await update.message.reply_text(
            "❌ No pude obtener props de Polymarket ni del fallback.\n"
            "Usa `/debug` para ver qué está pasando.\n"
            "O agrega props manualmente con `/add`.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    args = context.args or []
    slug_filter = None
    player_filter = None

    if args:
        q = " ".join(args).strip()
        if q.lower().startswith("nba-"):
            slug_filter = q.lower()
        else:
            player_filter = q.lower()

    filtered = props_pm
    if slug_filter:
        filtered = [p for p in props_pm if (p.game_slug or "").lower() == slug_filter]
        if not filtered:
            await update.message.reply_text(
                f"No encontré props para ese partido.\n`{slug_filter}`\n\n"
                f"Slugs disponibles:\n" + "\n".join(set(f"`{p.game_slug}`" for p in props_pm[:20])),
                parse_mode=ParseMode.MARKDOWN
            )
            return

    if player_filter:
        filtered = [p for p in props_pm if player_filter in (p.player or "").lower()]
        if not filtered:
            await update.message.reply_text(
                f"No encontré props para ese jugador: `{player_filter}`",
                parse_mode=ParseMode.MARKDOWN
            )
            return

    # Contar fuente
    sources = {}
    for p in filtered:
        sources[p.source] = sources.get(p.source, 0) + 1
    source_info = ", ".join(f"{s}:{n}" for s, n in sources.items())

    grouped = _group_props_pretty(filtered)

    lines = [f"🟣 *Polymarket — Props P/R/A* ({source_info})"]
    tipo_order = {"puntos": 0, "rebotes": 1, "asistencias": 2}

    for slug in sorted(grouped.keys()):
        lines.append(f"\n*{slug}*")
        players_sorted = sorted(grouped[slug].keys())
        for pl in players_sorted:
            lines.append(f"👤 *{pl}*")
            entries = grouped[slug][pl]
            for (tipo, ln) in sorted(entries.keys(), key=lambda x: (tipo_order.get(x[0], 9), x[1])):
                flags = entries[(tipo, ln)]
                o = "O" if flags.get("over") else "-"
                u = "U" if flags.get("under") else "-"
                lines.append(f"  • {tipo}: {o} {ln} | {u} {ln}")

    msg = "\n".join(lines)
    if len(msg) > 3800:
        msg = msg[:3800] + "\n…(recortado)"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def cmd_live(update: Update, context: ContextTypes.DEFAULT_TYPE):
    props_manual = load_props()
    props_pm = polymarket_props_today_from_scoreboard()
    all_props = (props_manual or []) + (props_pm or [])

    if not all_props:
        await update.message.reply_text(
            "No tengo props cargados.\n"
            "• Intenta `/odds` (auto)\n"
            "• O agrega manual con `/add ...`\n",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    try:
        games = scoreboard.ScoreBoard().get_dict()["scoreboard"]["games"]
    except Exception as e:
        await update.message.reply_text(f"⚠️ No pude leer scoreboard: {e}")
        return

    live_games = [g for g in games if g.get("gameStatus") == 2]
    if not live_games:
        await update.message.reply_text(
            "No hay partidos en vivo ahora.\nUsa `/games` o `/today` para ver la cartelera.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    by_pid: Dict[int, List[Prop]] = {}
    for p in all_props:
        pid = get_pid_for_name(p.player)
        if pid:
            by_pid.setdefault(pid, []).append(p)

    scored_rows = []

    for g in live_games:
        gid = g.get("gameId")
        status = g.get("gameStatusText", "")
        period = int(g.get("period", 0) or 0)
        game_clock = g.get("gameClock", "") or ""
        clock_sec = clock_to_seconds(game_clock)

        home = g.get("homeTeam", {})
        away = g.get("awayTeam", {})
        diff = abs(int(home.get("score", 0)) - int(away.get("score", 0)))
        is_clutch = diff <= 8
        is_blowout = diff >= BLOWOUT_IS
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
                        if should_gate_by_minutes("over", pr.tipo, faltante, mins, elapsed_min, is_blowout):
                            continue
                        if diff >= BLOWOUT_STRONG:
                            if (pr.tipo == "puntos" and faltante > 1.0) or (pr.tipo != "puntos" and faltante > 0.8):
                                continue

                        live = compute_over_score(pr.tipo, faltante, mins, pf, period, clock_sec, diff, is_clutch, is_blowout)
                        final = int(clamp(0.55 * live + 0.45 * pre, 0, 100))
                        scored_rows.append((final, live, pre, pr, actual, faltante, status, period, game_clock, mins, pf, diff, meta))

                    else:
                        margin_under = float(pr.line) - float(actual)
                        if should_gate_by_minutes("under", pr.tipo, margin_under, mins, elapsed_min, is_blowout):
                            continue

                        live = compute_under_score(pr.tipo, margin_under, mins, pf, period, clock_sec, diff, is_clutch, is_blowout)
                        final = int(clamp(0.65 * live + 0.35 * pre, 0, 100))
                        scored_rows.append((final, live, pre, pr, actual, margin_under, status, period, game_clock, mins, pf, diff, meta))

    if not scored_rows:
        await update.message.reply_text("No encontré props con señal en vivo (cerca de la línea + minutos OK).")
        return

    scored_rows.sort(key=lambda x: x[0], reverse=True)
    top = scored_rows[:18]

    out = ["🔥 *TOP LIVE (score final 1-100)*"]
    for (final, live, pre, pr, actual, delta, status, period, clock, mins, pf, diff, meta) in top:
        side_tag = "OVER" if pr.side == "over" else "UNDER"
        extra = f"faltan {delta:.1f}" if pr.side == "over" else f"colchón {delta:.1f}"
        out.append(
            f"\n👤 *{pr.player}*\n"
            f"• {pr.tipo} {side_tag} {pr.line}\n"
            f"FINAL `{final}` (LIVE {live} | PRE {pre}) | actual={actual} ({extra})\n"
            f"{status} | Q{period} {clock} | MIN {mins:.1f} PF {pf} Diff {diff}\n"
            f"Forma: 5={meta['hits5']}/{meta['n5']} 10={meta['hits10']}/{meta['n10']} | fuente={pr.source}"
        )

    msg = "\n".join(out)
    if len(msg) > 3800:
        msg = msg[:3800] + "\n…(recortado)"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

# =========================
# Background scan
# =========================
async def background_scan(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id

    props_manual = load_props()
    props_pm = polymarket_props_today_from_scoreboard()
    props = (props_manual or []) + (props_pm or [])
    if not props:
        return

    state = load_alert_state()

    by_pid: Dict[int, List[Prop]] = {}
    for p in props:
        pid = get_pid_for_name(p.player)
        if pid:
            by_pid.setdefault(pid, []).append(p)

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
        diff = abs(int(home.get("score", 0)) - int(away.get("score", 0)))

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
                                msg = (
                                    f"🎯 *ALERTA OVER* | *FINAL* `{final}/100` (LIVE {live} | PRE {pre})\n"
                                    f"👤 *{name}*\n"
                                    f"📊 {pr.tipo.upper()} {actual}/{pr.line} (faltan {faltante:.1f})\n"
                                    f"⏱️ {status} | Q{period} {game_clock}\n"
                                    f"🧠 MIN {mins:.1f} | PF {pf} | Diff {diff}\n"
                                    f"📈 Forma: 5={meta['hits5']}/{meta['n5']} | 10={meta['hits10']}/{meta['n10']}\n"
                                    f"🔌 Fuente: {pr.source}\n"
                                )
                                await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)

                    else:
                        margin_under = float(pr.line) - float(actual)
                        if should_gate_by_minutes("under", pr.tipo, margin_under, mins, elapsed_min, is_blowout):
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
                                    f"📈 Forma: 5={meta['hits5']}/{meta['n5']} | 10={meta['hits10']}/{meta['n10']}\n"
                                    f"🔌 Fuente: {pr.source}\n"
                                )
                                await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)

    save_alert_state(state)

# =========================
# Main
# =========================
async def on_startup(app: Application):
    log.info("Bot arrancado.")

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
    app.add_handler(CommandHandler("games", cmd_games))
    app.add_handler(CommandHandler("today", cmd_games))
    app.add_handler(CommandHandler("odds", cmd_odds))
    app.add_handler(CommandHandler("live", cmd_live))
    app.add_handler(CommandHandler("debug", cmd_debug))   # ← nuevo

    app.post_init = on_startup
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
