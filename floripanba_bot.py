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
from nba_api.stats.static import players, teams as nba_teams_static
from nba_api.stats.endpoints import commonteamroster

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
    "🧠 *NBA Props Bot*\n\n"

    "*📋 Info & Programación*\n"
    "• `/games` `/today` → partidos de hoy\n"
    "• `/lineup` → alineaciones + injury report\n"
    "   `/lineup BOS` → filtrar por equipo\n\n"

    "*📊 Props & Análisis*\n"
    "• `/odds` → todas las props con score 1-100\n"
    "   `/odds nba-bos-den-...` → un partido\n"
    "   `/odds Jokic` → un jugador\n"
    "• `/alertas` → ranking mejores props hoy (PRE≥55)\n"
    "• `/analisis Jugador | tipo | side | linea`\n"
    "   → análisis profundo: tendencia, racha, H/A splits,\n"
    "     matchup histórico vs rival, veredicto\n"
    "   Ej: `/analisis Nikola Jokic | puntos | over | 27.5`\n\n"

    "*🔴 En vivo*\n"
    "• `/live` → top props en vivo con scoring\n\n"

    "*💰 Tracking de apuestas*\n"
    "• `/bet Jugador | tipo | side | linea | monto`\n"
    "   Ej: `/bet Jokic | puntos | over | 27.5 | 50`\n"
    "• `/misapuestas` → ver apuestas pendientes\n"
    "• `/resultado ID WIN|LOSS|PUSH stat`\n"
    "   Ej: `/resultado A3F2B1 WIN 29`\n"
    "• `/historial` → ROI, win rate, rachas, top jugadores\n"
    "   `/historial 7` → últimos 7 días\n\n"

    "*⚙️ Manual & Debug*\n"
    "• `/add Jugador | tipo | side | linea` → prop manual\n"
    "• `/debug` → estado técnico de Polymarket\n"
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

def _pre_rating_emoji(score: int) -> str:
    """Convierte PRE score en emoji de rating visual."""
    if score >= 75:
        return "🔥"
    elif score >= 60:
        return "✅"
    elif score >= 45:
        return "🟡"
    elif score >= 30:
        return "🟠"
    else:
        return "❄️"

def _pre_bar(score: int, length: int = 8) -> str:
    """Barra de progreso visual para el score."""
    filled = round(score / 100 * length)
    return "█" * filled + "░" * (length - filled)

def _pre_label(score: int) -> str:
    if score >= 75: return "FUERTE"
    elif score >= 60: return "BUENA"
    elif score >= 45: return "MEDIA"
    elif score >= 30: return "DÉBIL"
    else: return "BAJA"

def _slug_to_matchup(slug: str) -> str:
    """Convierte 'nba-bos-den-2026-02-25' en 'BOS @ DEN'."""
    parts = slug.replace("nba-", "").split("-")
    if len(parts) >= 2:
        away = parts[0].upper()
        home = parts[1].upper()
        return f"{away} @ {home}"
    return slug

# =========================
# PRE score cache (evita recalcular en /odds repetidos)
# =========================
PRE_SCORE_CACHE: Dict[str, Tuple[int, int, dict]] = {}  # key → (pre_over, pre_under, meta)
PRE_SCORE_CACHE_TTL = 3 * 60 * 60  # 3 horas

def _pre_cache_key(pid: int, tipo: str, line: float) -> str:
    return f"{pid}:{tipo}:{line}"

def pre_score_cached(pid: int, tipo: str, line: float) -> Tuple[int, int, dict]:
    """Calcula PRE score over+under con cache en memoria."""
    key = _pre_cache_key(pid, tipo, line)
    if key in PRE_SCORE_CACHE:
        return PRE_SCORE_CACHE[key]
    po, meta_o = pre_score(pid, tipo, line, "over")
    pu, _      = pre_score(pid, tipo, line, "under")
    PRE_SCORE_CACHE[key] = (po, pu, meta_o)
    return po, pu, meta_o

def _compute_pre_for_player(player_name: str, tipo: str, line: float, source: str) -> dict:
    """
    Función bloqueante que calcula PRE score para un jugador.
    Se ejecuta en un thread separado para no bloquear el event loop.
    """
    pid = get_pid_for_name(player_name)
    if not pid:
        return {
            "tipo": tipo, "line": line, "source": source,
            "pre_over": 0, "pre_under": 0, "meta_over": {},
            "pid": None,
        }
    po, pu, meta = pre_score_cached(pid, tipo, line)
    return {
        "tipo": tipo, "line": line, "source": source,
        "pre_over": po, "pre_under": pu, "meta_over": meta,
        "pid": pid,
    }

def _build_game_message(slug: str, players_data: Dict[str, List[dict]]) -> str:
    """Construye el mensaje formateado de un partido con sus props y scores."""
    tipo_order = {"puntos": 0, "rebotes": 1, "asistencias": 2}
    tipo_icon  = {"puntos": "🏀", "rebotes": "💪", "asistencias": "🎯"}

    matchup = _slug_to_matchup(slug)
    lines = [f"🟣 *{matchup}*\n`{slug}`\n{'─'*28}"]

    def best_score(entries):
        return max((max(e["pre_over"], e["pre_under"]) for e in entries), default=0)

    players_sorted = sorted(players_data.keys(), key=lambda pl: best_score(players_data[pl]), reverse=True)

    for pl in players_sorted:
        entries = sorted(players_data[pl], key=lambda e: tipo_order.get(e["tipo"], 9))
        lines.append(f"\n👤 *{pl}*")

        for e in entries:
            tipo = e["tipo"]
            ln   = e["line"]
            po   = e["pre_over"]
            pu   = e["pre_under"]
            icon = tipo_icon.get(tipo, "•")
            meta = e.get("meta_over", {})

            h5   = meta.get("hits5", "?")
            n5   = meta.get("n5",    "?")
            h10  = meta.get("hits10","?")
            n10  = meta.get("n10",   "?")
            avg10= meta.get("avg10", None)
            avg_str = f"prom10: *{avg10:.1f}*" if avg10 is not None else ""

            lines.append(
                f"{icon} *{tipo.upper()}* — línea `{ln}`\n"
                f"  OVER  {_pre_rating_emoji(po)} `{po:>3}/100` {_pre_bar(po)} _{_pre_label(po)}_\n"
                f"  UNDER {_pre_rating_emoji(pu)} `{pu:>3}/100` {_pre_bar(pu)} _{_pre_label(pu)}_\n"
                f"  📊 `{h5}/{n5}` últ.5 | `{h10}/{n10}` últ.10  {avg_str}"
            )

    return "\n".join(lines)

async def cmd_odds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tipo_order = {"puntos": 0, "rebotes": 1, "asistencias": 2}

    msg_loading = await update.message.reply_text(
        "⏳ *Cargando props de Polymarket...*",
        parse_mode=ParseMode.MARKDOWN
    )

    # ── 1. Cargar props (puede hacer requests a Polymarket) ──
    props_pm = await asyncio.to_thread(polymarket_props_today_from_scoreboard)

    if not props_pm:
        await msg_loading.edit_text(
            "❌ No pude obtener props.\nUsa `/debug` o agrega con `/add`.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # ── 2. Filtros opcionales ──
    args = context.args or []
    slug_filter   = None
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
            slugs_avail = "\n".join(set(f"`{p.game_slug}`" for p in props_pm[:20]))
            await msg_loading.edit_text(
                f"❌ Sin props para `{slug_filter}`\n\nDisponibles:\n{slugs_avail}",
                parse_mode=ParseMode.MARKDOWN
            )
            return
    if player_filter:
        filtered = [p for p in props_pm if player_filter in (p.player or "").lower()]
        if not filtered:
            await msg_loading.edit_text(
                f"❌ Sin props para jugador: `{player_filter}`",
                parse_mode=ParseMode.MARKDOWN
            )
            return

    # ── 3. Agrupar props únicos por partido y jugador ──
    grouped_unique: Dict[str, Dict[str, List[Tuple[str, float, str]]]] = {}
    seen_lines: set = set()
    for p in filtered:
        if p.side != "over":
            continue
        key = (p.game_slug, p.player, p.tipo, p.line)
        if key in seen_lines:
            continue
        seen_lines.add(key)
        slug = p.game_slug or "unknown"
        grouped_unique.setdefault(slug, {})
        grouped_unique[slug].setdefault(p.player, [])
        grouped_unique[slug][p.player].append((p.tipo, p.line, p.source))

    total_jugadores = sum(len(pls) for pls in grouped_unique.values())
    total_lineas    = len(seen_lines)

    await msg_loading.edit_text(
        f"⚡ *Calculando scores...*\n"
        f"_{total_jugadores} jugadores · {total_lineas} líneas_\n"
        f"_(los resultados aparecen partido a partido)_",
        parse_mode=ParseMode.MARKDOWN
    )

    # ── 4. Header global ──
    today_str = date.today().strftime("%d/%m/%Y")
    fuentes   = ", ".join(sorted(set(p.source for p in filtered)))
    header = (
        f"📋 *NBA Props — {today_str}*\n"
        f"🔌 {fuentes}  ·  {total_lineas} líneas\n"
        f"{'─'*30}\n"
        f"_🔥≥75 · ✅≥60 · 🟡≥45 · 🟠≥30 · ❄️<30_"
    )
    await update.message.reply_text(header, parse_mode=ParseMode.MARKDOWN)

    # ── 5. Semáforo: máximo 4 threads simultáneos a NBA API ──
    sem = asyncio.Semaphore(4)

    async def compute_player_safe(player_name: str, lineas: List[Tuple[str, float, str]]) -> Tuple[str, List[dict]]:
        async with sem:
            results = []
            for (tipo, line, source) in lineas:
                try:
                    entry = await asyncio.wait_for(
                        asyncio.to_thread(_compute_pre_for_player, player_name, tipo, line, source),
                        timeout=30.0
                    )
                except asyncio.TimeoutError:
                    log.warning(f"Timeout calculando {player_name} {tipo}")
                    entry = {"tipo": tipo, "line": line, "source": source,
                             "pre_over": 0, "pre_under": 0, "meta_over": {}, "pid": None}
                except Exception as e:
                    log.warning(f"Error calculando {player_name}: {e}")
                    entry = {"tipo": tipo, "line": line, "source": source,
                             "pre_over": 0, "pre_under": 0, "meta_over": {}, "pid": None}
                results.append(entry)
            return player_name, results

    # ── 6. Procesar partido a partido ──
    for slug in sorted(grouped_unique.keys()):
        matchup = _slug_to_matchup(slug)
        players_in_game = grouped_unique[slug]

        try:
            await msg_loading.edit_text(
                f"⚡ *Calculando...*\n🏀 _{matchup}_ ({len(players_in_game)} jugadores)",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception:
            pass

        # Lanzar todos los jugadores del partido con semáforo (máx 4 a la vez)
        tasks = [compute_player_safe(pl, lineas) for pl, lineas in players_in_game.items()]
        try:
            player_results_raw = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=120.0
            )
        except asyncio.TimeoutError:
            log.warning(f"Timeout global en partido {slug}")
            await update.message.reply_text(
                f"⚠️ *{matchup}* — timeout calculando scores, mostrando sin PRE.",
                parse_mode=ParseMode.MARKDOWN
            )
            continue

        # Reconstruir dict player → entries (ignorar excepciones individuales)
        players_data: Dict[str, List[dict]] = {}
        for result in player_results_raw:
            if isinstance(result, Exception):
                log.warning(f"Excepción en gather: {result}")
                continue
            pl_name, entries = result
            players_data[pl_name] = sorted(entries, key=lambda e: tipo_order.get(e["tipo"], 9))

        if not players_data:
            continue

        # Construir mensaje y partir si es necesario
        msg_game = _build_game_message(slug, players_data)
        await _send_long_message(update, msg_game)

    # ── 7. Borrar loading ──
    try:
        await msg_loading.delete()
    except Exception:
        pass


async def _send_long_message(update, text: str, max_len: int = 3800):
    """Envía un mensaje partiéndolo en bloques si supera max_len."""
    if len(text) <= max_len:
        try:
            await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            log.warning(f"Markdown error, enviando sin formato: {e}")
            await update.message.reply_text(text.replace("*","").replace("_","").replace("`",""))
        return

    # Partir en bloques respetando saltos de jugador
    parts = []
    remaining = text
    while len(remaining) > max_len:
        # Buscar el último \n👤 antes del límite
        cut = remaining[:max_len].rfind("\n👤")
        if cut < 200:
            # No hay salto limpio, cortar en último \n
            cut = remaining[:max_len].rfind("\n")
        if cut < 0:
            cut = max_len
        parts.append(remaining[:cut])
        remaining = remaining[cut:]

    if remaining:
        parts.append(remaining)

    for i, part in enumerate(parts):
        prefix = f"_(continuación {i+1}/{len(parts)})_\n" if i > 0 else ""
        try:
            await update.message.reply_text(prefix + part, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            log.warning(f"Markdown error parte {i}: {e}")
            await update.message.reply_text((prefix + part).replace("*","").replace("_","").replace("`",""))
        await asyncio.sleep(0.3)  # pequeña pausa entre partes

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
# INJURY REPORT + LINEUPS
# =========================

INJURY_CACHE: Dict[str, dict] = {}   # team_id -> {ts, players}
INJURY_TTL = 15 * 60                 # 15 min

# Posiciones estelares (para evaluar impacto de bajas)
STAR_ROLES = {"G", "F", "C", "G-F", "F-G", "F-C", "C-F"}

# Status de injury report NBA
INJURY_STATUS_LABELS = {
    "Out": "🔴 BAJA",
    "Doubtful": "🟠 DUDA",
    "Questionable": "🟡 DUDA POSIBLE",
    "Probable": "🟢 PROBABLE",
    "Available": "✅ DISPONIBLE",
    "Active": "✅ ACTIVO",
}

def get_team_id_by_tricode(tricode: str) -> Optional[int]:
    """Obtiene el team_id de NBA por tricode (ej: 'BOS' → 1610612738)."""
    all_teams = nba_teams_static.get_teams()
    for t in all_teams:
        if t.get("abbreviation", "").upper() == tricode.upper():
            return int(t["id"])
    return None

def fetch_team_roster_and_injuries(team_id: int) -> dict:
    """
    Obtiene el roster completo + injury status via commonteamroster.
    Retorna dict con 'players': lista de dicts con name, position, status, injury_desc.
    """
    cache_key = str(team_id)
    now = now_ts()
    if cache_key in INJURY_CACHE and (now - INJURY_CACHE[cache_key].get("ts", 0)) < INJURY_TTL:
        return INJURY_CACHE[cache_key]

    time.sleep(0.4 + random.random() * 0.2)
    try:
        roster = commonteamroster.CommonTeamRoster(
            team_id=team_id,
            season=SEASON,
        )
        df = roster.get_data_frames()
        # df[0] = roster, df[1] = coaches
        roster_df = df[0] if df else None
        if roster_df is None or roster_df.empty:
            return {"ts": now, "players": []}

        player_list = []
        for _, row in roster_df.iterrows():
            status_raw = str(row.get("HOW_ACQUIRED") or "")
            # El injury status real viene del live scoreboard, no del roster endpoint
            # Aquí guardamos la info base del jugador
            player_list.append({
                "name": str(row.get("PLAYER") or ""),
                "position": str(row.get("POSITION") or ""),
                "number": str(row.get("NUM") or ""),
                "player_id": int(row.get("PLAYER_ID") or 0),
                "status": "Active",       # se sobreescribe con live data
                "injury_desc": "",
            })

        result = {"ts": now, "players": player_list}
        INJURY_CACHE[cache_key] = result
        return result

    except Exception as e:
        log.warning(f"fetch_team_roster_and_injuries team_id={team_id}: {e}")
        return {"ts": now, "players": []}

def fetch_injury_report_from_scoreboard(games: list) -> Dict[str, List[dict]]:
    """
    Extrae el injury report embebido en el scoreboard de NBA.
    Retorna dict: team_tricode → lista de {name, status, description, position}
    """
    injuries: Dict[str, List[dict]] = {}
    for g in games:
        for team_key in ["homeTeam", "awayTeam"]:
            team = g.get(team_key, {}) or {}
            tricode = team.get("teamTricode", "")
            if not tricode:
                continue
            injuries.setdefault(tricode, [])
            # El scoreboard incluye 'players' con gameStatus para live
            # Pero para injury report pre-game usamos el campo 'injuries' si existe
            game_injuries = g.get("gameLeaders", {})  # no es la fuente correcta

            # Intentar desde el campo injuries del juego (disponible en algunos endpoints)
            inj_list = g.get("injuries", []) or []
            for inj in inj_list:
                if (inj.get("teamTricode") or "").upper() == tricode.upper():
                    injuries[tricode].append({
                        "name": inj.get("playerName", ""),
                        "status": inj.get("status", ""),
                        "description": inj.get("injuryDescription", ""),
                        "position": inj.get("position", ""),
                    })
    return injuries

def fetch_boxscore_injury_data(game_id: str) -> Dict[str, List[dict]]:
    """
    Obtiene datos de jugadores del boxscore pre-game / live.
    Incluye status (Active, Inactive, etc.).
    Retorna dict: team_tricode → lista de jugadores con status
    """
    result: Dict[str, List[dict]] = {}
    try:
        time.sleep(0.3)
        box = boxscore.BoxScore(game_id).get_dict().get("game", {})
        for team_key in ["homeTeam", "awayTeam"]:
            team = box.get(team_key, {}) or {}
            tricode = team.get("teamTricode", "")
            if not tricode:
                continue
            result[tricode] = []
            for pl in team.get("players", []):
                status = pl.get("status", "Active")
                name = f"{pl.get('firstName', '')} {pl.get('familyName', '')}".strip()
                pos = pl.get("position", "")
                starter = pl.get("starter", "0")
                not_playing = pl.get("notPlayingReason", "") or pl.get("inactiveReason", "") or ""
                result[tricode].append({
                    "name": name,
                    "status": status,
                    "position": pos,
                    "starter": starter == "1",
                    "not_playing_reason": not_playing,
                    "player_id": pl.get("personId", 0),
                })
    except Exception as e:
        log.warning(f"fetch_boxscore_injury_data game_id={game_id}: {e}")
    return result

def _is_star_player(name: str, pid: int) -> bool:
    """Heurística simple: si tiene historial de 20+ ppg es estrella."""
    try:
        _, rows = get_gamelog_table(pid)
        if not rows or len(rows) < 3:
            return False
        pts_vals = []
        for r in rows[:10]:
            # PTS suele estar en columna 26 aprox, pero usamos last_n_values
            pass
        v = last_n_values(pid, "puntos", 10)
        avg = sum(v) / len(v) if v else 0
        return avg >= 18.0
    except Exception:
        return False

def analyze_lineup_impact(
    home_tri: str,
    away_tri: str,
    home_players: List[dict],
    away_players: List[dict],
) -> List[str]:
    """
    Analiza el impacto de ausencias/dudas en el partido.
    Retorna lista de strings con análisis.
    """
    alerts = []

    for tri, pl_list, label in [(home_tri, home_players, "🏠"), (away_tri, away_players, "✈️")]:
        inactives = [p for p in pl_list if p.get("status", "").lower() in ("inactive", "out")]
        starters_missing = [p for p in inactives if p.get("starter")]

        out_by_reason: Dict[str, List[str]] = {}
        for p in inactives:
            reason = p.get("not_playing_reason") or "Baja"
            reason_short = reason[:40]
            out_by_reason.setdefault(reason_short, []).append(p["name"])

        # Estrellas fuera
        for p in inactives:
            pid = p.get("player_id", 0)
            if pid and _is_star_player(p["name"], pid):
                alerts.append(
                    f"⚠️ *ESTRELLA BAJA* {label}{tri}: *{p['name']}* ({p.get('not_playing_reason','Out')})\n"
                    f"   → Impacto ALTO: buscar beneficiados en props"
                )

        # Muchos titulares fuera
        if len(starters_missing) >= 2:
            names = ", ".join(p["name"] for p in starters_missing)
            alerts.append(
                f"🚨 *{len(starters_missing)} TITULARES FUERA* {label}{tri}: {names}\n"
                f"   → Ritmo/rotación afectado, líneas pueden estar desajustadas"
            )
        elif len(starters_missing) == 1:
            alerts.append(
                f"⚡ *Titular fuera* {label}{tri}: *{starters_missing[0]['name']}*\n"
                f"   → Posible aumento de minutos para suplentes"
            )

        # Muchos inactivos en general
        if len(inactives) >= 4:
            alerts.append(
                f"🏥 *{tri}* tiene {len(inactives)} jugadores inactivos hoy — rotación muy corta"
            )

    # Análisis cruzado: si ambos equipos tienen bajas → más puntos totales (suplentes que corren más)
    home_inact = sum(1 for p in home_players if p.get("status", "").lower() in ("inactive", "out"))
    away_inact = sum(1 for p in away_players if p.get("status", "").lower() in ("inactive", "out"))
    if home_inact >= 2 and away_inact >= 2:
        alerts.append(
            f"📈 *Ambos equipos con bajas* ({home_tri}: {home_inact}, {away_tri}: {away_inact})\n"
            f"   → Partido más abierto, líneas de puntos individuales pueden ser más alcanzables"
        )

    if not alerts:
        alerts.append("✅ Sin bajas destacadas — alineaciones completas esperadas")

    return alerts

def format_team_lineup(tricode: str, players_data: List[dict]) -> str:
    """Formatea la alineación de un equipo de forma visual."""
    starters = [p for p in players_data if p.get("starter") and p.get("status", "").lower() not in ("inactive", "out")]
    bench = [p for p in players_data if not p.get("starter") and p.get("status", "").lower() not in ("inactive", "out")]
    inactives = [p for p in players_data if p.get("status", "").lower() in ("inactive", "out")]

    lines = [f"*{tricode}*"]

    if starters:
        lines.append("  5️⃣ *Titulares:*")
        for p in starters[:5]:
            pos = f"[{p['position']}]" if p.get("position") else ""
            lines.append(f"    • {p['name']} {pos}")

    if bench:
        lines.append(f"  🪑 *Banco* ({len(bench)} jug.):")
        for p in bench[:6]:
            lines.append(f"    • {p['name']}")
        if len(bench) > 6:
            lines.append(f"    ... +{len(bench)-6} más")

    if inactives:
        lines.append(f"  🔴 *Inactivos* ({len(inactives)}):")
        for p in inactives:
            reason = p.get("not_playing_reason", "")
            reason_str = f" — _{reason[:30]}_" if reason else ""
            lines.append(f"    • {p['name']}{reason_str}")

    return "\n".join(lines)

async def cmd_lineup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra alineaciones, injury report y análisis de impacto para los partidos de hoy."""
    msg_wait = await update.message.reply_text(
        "⏳ Obteniendo alineaciones e injury report...",
        parse_mode=ParseMode.MARKDOWN
    )

    try:
        board = scoreboard.ScoreBoard().get_dict()["scoreboard"]
        games = board.get("games", [])
    except Exception as e:
        await msg_wait.edit_text(f"⚠️ Error leyendo scoreboard: {e}")
        return

    if not games:
        await msg_wait.edit_text("No hay partidos NBA hoy.")
        return

    # Filtrar por partido si se pasó argumento
    args = context.args or []
    filter_tri = " ".join(args).strip().upper() if args else None

    today_str = date.today().strftime("%d/%m/%Y")
    await msg_wait.edit_text(
        f"🔄 Cargando datos de {len(games)} partidos...",
        parse_mode=ParseMode.MARKDOWN
    )

    sent_any = False
    for g in games:
        away_team = g.get("awayTeam", {}) or {}
        home_team = g.get("homeTeam", {}) or {}
        away_tri = away_team.get("teamTricode", "")
        home_tri = home_team.get("teamTricode", "")
        game_id = g.get("gameId", "")
        status_txt = g.get("gameStatusText", "")
        game_status = g.get("gameStatus", 1)  # 1=pre, 2=live, 3=final

        # Filtro opcional
        if filter_tri and filter_tri not in (away_tri, home_tri):
            continue

        # Obtener datos de jugadores desde boxscore
        box_data = fetch_boxscore_injury_data(game_id) if game_id else {}
        away_players = box_data.get(away_tri, [])
        home_players = box_data.get(home_tri, [])

        # Si el boxscore no tiene datos (partido muy early), intentar con roster
        if not away_players:
            away_id = get_team_id_by_tricode(away_tri)
            if away_id:
                roster_data = fetch_team_roster_and_injuries(away_id)
                away_players = roster_data.get("players", [])

        if not home_players:
            home_id = get_team_id_by_tricode(home_tri)
            if home_id:
                roster_data = fetch_team_roster_and_injuries(home_id)
                home_players = roster_data.get("players", [])

        # Análisis de impacto
        impact_alerts = analyze_lineup_impact(home_tri, away_tri, home_players, away_players)

        # ── Construir mensaje ──
        game_label = f"✈️ *{away_tri}* @ 🏠 *{home_tri}*"
        status_icon = "🟢 EN VIVO" if game_status == 2 else ("⏰ PREVIO" if game_status == 1 else "🏁 FINAL")

        header = (
            f"{'─'*32}\n"
            f"{game_label}\n"
            f"{status_icon} | {status_txt}\n"
            f"{'─'*32}"
        )

        # Alineaciones
        away_fmt = format_team_lineup(away_tri, away_players) if away_players else f"*{away_tri}*\n  _(sin datos aún)_"
        home_fmt = format_team_lineup(home_tri, home_players) if home_players else f"*{home_tri}*\n  _(sin datos aún)_"

        lineup_block = f"🏀 *ALINEACIONES*\n\n{away_fmt}\n\n{home_fmt}"

        # Análisis
        impact_block = "🧠 *ANÁLISIS DE IMPACTO*\n\n" + "\n\n".join(impact_alerts)

        full_msg = f"{header}\n\n{lineup_block}\n\n{impact_block}"

        if len(full_msg) > 3900:
            full_msg = full_msg[:3900] + "\n…(recortado)"

        try:
            await update.message.reply_text(full_msg, parse_mode=ParseMode.MARKDOWN)
            sent_any = True
        except Exception as e:
            log.warning(f"Error enviando lineup msg: {e}")
            # Intentar sin markdown
            await update.message.reply_text(
                full_msg.replace("*", "").replace("_", "").replace("`", "")
            )
            sent_any = True

        time.sleep(0.5)  # respetar rate limits

    await msg_wait.delete()

    if not sent_any:
        await update.message.reply_text(
            f"No encontré datos para `{filter_tri}`.\n"
            f"Usa `/lineup` sin argumentos para ver todos los partidos.",
            parse_mode=ParseMode.MARKDOWN
        )

# ================================================================
# BLOQUE 1 — ANÁLISIS ESTADÍSTICO AVANZADO
# ================================================================

def get_full_gamelog(pid: int) -> Tuple[List[str], List[list]]:
    """Devuelve headers y rows completos del gamelog (usa cache existente)."""
    return get_gamelog_table(pid)

def gamelog_col(headers: List[str], rows: List[list], col: str) -> List[float]:
    """Extrae una columna del gamelog como lista de floats (juego más reciente primero)."""
    if not headers or not rows:
        return []
    try:
        idx = headers.index(col)
    except ValueError:
        return []
    vals = []
    for r in rows:
        try:
            vals.append(float(r[idx]))
        except Exception:
            pass
    return vals

def gamelog_col_str(headers: List[str], rows: List[list], col: str) -> List[str]:
    """Extrae una columna del gamelog como lista de strings."""
    if not headers or not rows:
        return []
    try:
        idx = headers.index(col)
    except ValueError:
        return []
    return [str(r[idx]) for r in rows if idx < len(r)]

def trend_arrow(values: List[float]) -> str:
    """Flecha de tendencia basada en los últimos 5 vs anteriores 5."""
    if len(values) < 6:
        return "→"
    rec  = sum(values[:5])  / 5
    prev = sum(values[5:10]) / min(5, len(values[5:10]))
    diff = rec - prev
    if diff >  1.5: return "📈"
    if diff < -1.5: return "📉"
    return "→"

def streak_info(values: List[float], line: float, side: str) -> str:
    """Calcula racha actual (ej: '4 en racha ✅' o '2 sin cumplir ❌')."""
    if not values:
        return "sin datos"
    count = 0
    if side == "over":
        hit_fn = lambda v: v > line
    else:
        hit_fn = lambda v: v < line
    first_hit = hit_fn(values[0])
    for v in values:
        if hit_fn(v) == first_hit:
            count += 1
        else:
            break
    emoji = "✅" if first_hit else "❌"
    label = "en racha" if first_hit else "sin cumplir"
    return f"{count} {label} {emoji}"

def matchup_stats(pid: int, opp_tricode: str, tipo: str) -> Optional[dict]:
    """
    Stats del jugador contra ese rival específico esta temporada.
    Busca en el gamelog partidos donde MATCHUP contiene el tricode.
    """
    headers, rows = get_full_gamelog(pid)
    if not headers or not rows:
        return None
    col = STAT_COL.get(tipo)
    if not col:
        return None
    try:
        stat_idx     = headers.index(col)
        matchup_idx  = headers.index("MATCHUP")
        min_idx      = headers.index("MIN") if "MIN" in headers else None
    except ValueError:
        return None

    opp_upper = opp_tricode.upper()
    vals = []
    for r in rows:
        matchup_str = str(r[matchup_idx]).upper() if matchup_idx < len(r) else ""
        if opp_upper in matchup_str:
            try:
                vals.append(float(r[stat_idx]))
            except Exception:
                pass

    if not vals:
        return None
    avg = sum(vals) / len(vals)
    return {
        "games":  len(vals),
        "avg":    round(avg, 1),
        "max":    max(vals),
        "min":    min(vals),
        "values": vals,
    }

def home_away_splits(pid: int, tipo: str) -> dict:
    """Promedio home vs away del jugador esta temporada."""
    headers, rows = get_full_gamelog(pid)
    if not headers or not rows:
        return {}
    col = STAT_COL.get(tipo)
    if not col:
        return {}
    try:
        stat_idx    = headers.index(col)
        matchup_idx = headers.index("MATCHUP")
    except ValueError:
        return {}

    home_vals, away_vals = [], []
    for r in rows:
        matchup_str = str(r[matchup_idx]) if matchup_idx < len(r) else ""
        try:
            val = float(r[stat_idx])
        except Exception:
            continue
        if " vs. " in matchup_str:
            home_vals.append(val)
        elif " @ " in matchup_str:
            away_vals.append(val)

    result = {}
    if home_vals:
        result["home_avg"] = round(sum(home_vals) / len(home_vals), 1)
        result["home_n"]   = len(home_vals)
    if away_vals:
        result["away_avg"] = round(sum(away_vals) / len(away_vals), 1)
        result["away_n"]   = len(away_vals)
    return result

def is_back_to_back(pid: int) -> bool:
    """¿Jugó ayer? Compara la fecha más reciente del gamelog con hoy."""
    headers, rows = get_full_gamelog(pid)
    if not headers or not rows:
        return False
    try:
        date_idx = headers.index("GAME_DATE")
    except ValueError:
        return False
    try:
        last_date_str = str(rows[0][date_idx])   # ej: "FEB 24, 2026"
        from datetime import datetime, timedelta
        last_date = datetime.strptime(last_date_str, "%b %d, %Y").date()
        yesterday = date.today() - timedelta(days=1)
        return last_date == yesterday
    except Exception:
        return False

def build_advanced_analysis(pid: int, player_name: str, tipo: str, line: float,
                            side: str, opp_tricode: str, is_home: bool) -> str:
    """
    Construye el bloque de análisis avanzado para un jugador/prop.
    Incluye: tendencia, racha, splits H/A, matchup histórico, back-to-back.
    """
    v20 = last_n_values(pid, tipo, 20)
    v10 = last_n_values(pid, tipo, 10)
    v5  = last_n_values(pid, tipo,  5)

    if not v10:
        return "_Sin suficientes datos estadísticos_"

    lines_out = []

    # — Promedios y tendencia —
    avg5  = round(sum(v5)  / len(v5),  1) if v5  else "—"
    avg10 = round(sum(v10) / len(v10), 1) if v10 else "—"
    avg20 = round(sum(v20) / len(v20), 1) if v20 else "—"
    arrow = trend_arrow(v10)

    lines_out.append(
        f"📊 *Promedios:* últ.5 `{avg5}` | últ.10 `{avg10}` | últ.20 `{avg20}` {arrow}"
    )

    # — Racha actual —
    racha = streak_info(v10, line, side)
    lines_out.append(f"🔁 *Racha actual:* {racha}  (línea `{line}` {side.upper()})")

    # — Últimos 5 juegos detalle —
    vals_str = "  ".join(f"`{v:.0f}`" for v in v5[:5])
    lines_out.append(f"🕐 *Últ. 5 juegos:* {vals_str}")

    # — Home/Away splits —
    splits = home_away_splits(pid, tipo)
    if splits:
        loc     = "home" if is_home else "away"
        opp_loc = "away" if is_home else "home"
        loc_avg = splits.get(f"{loc}_avg")
        opp_avg = splits.get(f"{opp_loc}_avg")
        loc_n   = splits.get(f"{loc}_n", 0)
        loc_icon = "🏠" if is_home else "✈️"
        if loc_avg is not None:
            diff_ha = round(loc_avg - (opp_avg or loc_avg), 1)
            sign    = "+" if diff_ha >= 0 else ""
            lines_out.append(
                f"{loc_icon} *H/A split:* prom {loc_loc_str(is_home)} `{loc_avg}` "
                f"({loc_n}G)  vs prom opuesto `{opp_avg or '—'}` → dif `{sign}{diff_ha}`"
            )

    # — Matchup histórico vs rival —
    mu = matchup_stats(pid, opp_tricode, tipo)
    if mu and mu["games"] >= 1:
        lines_out.append(
            f"🆚 *vs {opp_tricode}:* `{mu['avg']}` prom en {mu['games']}G "
            f"(max `{mu['max']:.0f}` / min `{mu['min']:.0f}`)"
        )
    else:
        lines_out.append(f"🆚 *vs {opp_tricode}:* sin historial esta temporada")

    # — Back-to-back —
    if is_back_to_b2b := is_back_to_back(pid):
        lines_out.append("⚠️ *BACK-TO-BACK:* jugó ayer → posible reducción de minutos/rendimiento")

    # — Veredicto automático —
    verdict = _auto_verdict(v10, line, side, avg10 if isinstance(avg10, float) else 0,
                             mu, splits, is_home, is_back_to_b2b)
    lines_out.append(f"\n🧠 *Veredicto:* {verdict}")

    return "\n".join(lines_out)

def loc_loc_str(is_home: bool) -> str:
    return "local" if is_home else "visitante"

def _auto_verdict(v10, line, side, avg10, mu, splits, is_home, is_b2b) -> str:
    """Genera un veredicto textual automático basado en todos los factores."""
    signals = []
    warnings = []

    # Señal 1: promedio vs línea
    if side == "over":
        gap = avg10 - line
        if gap >  2: signals.append(f"promedio {avg10:.1f} supera la línea por {gap:.1f}")
        elif gap < -2: warnings.append(f"promedio {avg10:.1f} está {abs(gap):.1f} por debajo")
    else:
        gap = line - avg10
        if gap >  2: signals.append(f"promedio {avg10:.1f} está {gap:.1f} bajo la línea")
        elif gap < -2: warnings.append(f"promedio {avg10:.1f} supera la línea por {abs(gap):.1f}")

    # Señal 2: racha
    h5, _ = hit_counts(v10[:5], line, side)
    if h5 >= 4: signals.append(f"viene de cumplir {h5}/5 últimos")
    elif h5 <= 1: warnings.append(f"solo {h5}/5 últimos cumplieron")

    # Señal 3: matchup
    if mu:
        mu_gap = mu["avg"] - line if side == "over" else line - mu["avg"]
        if mu_gap > 1.5: signals.append(f"historial favorable vs rival ({mu['avg']:.1f} prom)")
        elif mu_gap < -1.5: warnings.append(f"historial desfavorable vs rival ({mu['avg']:.1f} prom)")

    # Señal 4: split H/A
    loc = "home" if is_home else "away"
    loc_avg = splits.get(f"{loc}_avg") if splits else None
    if loc_avg:
        split_gap = loc_avg - line if side == "over" else line - loc_avg
        if split_gap > 1.5: signals.append(f"mejor de {loc_loc_str(is_home)} ({loc_avg:.1f} prom)")
        elif split_gap < -1.5: warnings.append(f"peor de {loc_loc_str(is_home)} ({loc_avg:.1f} prom)")

    # Señal 5: back-to-back
    if is_b2b:
        warnings.append("back-to-back reduce rendimiento esperado")

    if len(signals) >= 2 and len(warnings) == 0:
        return f"✅ *FAVORABLE* — {'; '.join(signals)}"
    elif len(signals) >= 1 and len(warnings) == 0:
        return f"🟡 *LIGERA VENTAJA* — {signals[0]}"
    elif len(warnings) >= 2 and len(signals) == 0:
        return f"🔴 *EN CONTRA* — {'; '.join(warnings)}"
    elif len(warnings) >= 1 and len(signals) == 0:
        return f"🟠 *PRECAUCIÓN* — {warnings[0]}"
    elif signals and warnings:
        return f"⚖️ *MIXTO* — a favor: {signals[0]} | en contra: {warnings[0]}"
    else:
        return "⚪ Sin señal clara — datos insuficientes"

async def cmd_analisis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /analisis Jugador | tipo | side | linea
    Ej: /analisis Nikola Jokic | puntos | over | 27.5
    """
    body = re.sub(r"^/analisis(@\w+)?\s*", "", (update.message.text or "")).strip()
    if "|" not in body:
        await update.message.reply_text(
            "Formato: `/analisis Nombre | tipo | side | linea`\n"
            "Ej: `/analisis Nikola Jokic | puntos | over | 27.5`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    parts = [x.strip() for x in body.split("|")]
    if len(parts) != 4:
        await update.message.reply_text("Necesito exactamente 4 campos separados por `|`", parse_mode=ParseMode.MARKDOWN)
        return

    player_name, tipo, side, line_s = parts
    tipo = tipo.lower()
    side = side.lower()
    if tipo not in ("puntos", "rebotes", "asistencias"):
        await update.message.reply_text("tipo debe ser: puntos / rebotes / asistencias")
        return
    if side not in ("over", "under"):
        await update.message.reply_text("side debe ser: over / under")
        return
    try:
        line = float(line_s)
    except Exception:
        await update.message.reply_text("La línea debe ser un número (ej: 27.5)")
        return

    msg_wait = await update.message.reply_text(
        f"🔍 Analizando *{player_name}* — {tipo} {side} {line}...",
        parse_mode=ParseMode.MARKDOWN
    )

    # Ejecutar en thread para no bloquear
    def _run():
        pid = get_pid_for_name(player_name)
        if not pid:
            return None, None, None
        po, pu, meta = pre_score_cached(pid, tipo, line)
        # Intentar detectar rival desde props del día
        opp_tricode = "???"
        is_home = True
        props_hoy = PM_CACHE.get("props", [])
        for p in props_hoy:
            if p.player.lower() == player_name.lower() and p.game_slug:
                parts_slug = (p.game_slug or "").replace("nba-","").split("-")
                if len(parts_slug) >= 2:
                    opp_tricode = parts_slug[1].upper() if is_home else parts_slug[0].upper()
                break
        return pid, po if side == "over" else pu, meta

    pid, pre, meta = await asyncio.to_thread(_run)

    if not pid:
        await msg_wait.edit_text(f"⚠️ No encontré al jugador: *{player_name}*", parse_mode=ParseMode.MARKDOWN)
        return

    # Detectar rival e is_home desde props cacheados
    opp_tricode = "???"
    is_home = True
    for p in PM_CACHE.get("props", []):
        if p.player.lower() == player_name.lower() and p.game_slug:
            slug_parts = (p.game_slug or "").replace("nba-","").split("-")
            if len(slug_parts) >= 2:
                # Heurística: buscamos en rosters de cada equipo del slug
                opp_tricode = slug_parts[1].upper()
                is_home = False  # visitante por defecto
            break

    analysis_text = await asyncio.to_thread(
        build_advanced_analysis, pid, player_name, tipo, line, side, opp_tricode, is_home
    )

    pre_label = _pre_label(pre or 0)
    pre_emoji = _pre_rating_emoji(pre or 0)
    pre_bar   = _pre_bar(pre or 0)

    header = (
        f"🔬 *ANÁLISIS AVANZADO*\n"
        f"👤 *{player_name}*\n"
        f"📌 {tipo.upper()} {side.upper()} `{line}`\n"
        f"{pre_emoji} PRE Score: `{pre}/100` {pre_bar} _{pre_label}_\n"
        f"{'─'*30}"
    )

    full = f"{header}\n\n{analysis_text}"
    if len(full) > 3900:
        full = full[:3900] + "\n…"
    await msg_wait.edit_text(full, parse_mode=ParseMode.MARKDOWN)


# ================================================================
# BLOQUE 2 — ALERTAS PRE-PARTIDO INTELIGENTES
# ================================================================

SMART_ALERTS_FILE  = "smart_alerts_state.json"
SMART_ALERT_THRESH = 68   # PRE score mínimo para alerta pre-partido
SMART_ALERT_HOUR_START = 10  # hora local a partir de la cual enviar alertas pre-partido
SMART_ALERTS_SENT_TTL  = 20 * 60 * 60  # no reenviar la misma alerta en 20h

def load_smart_alerts_state() -> dict:
    return load_json(SMART_ALERTS_FILE, {})

def save_smart_alerts_state(st: dict):
    save_json(SMART_ALERTS_FILE, st)

def _smart_alert_key(player: str, tipo: str, side: str, line: float) -> str:
    d = date.today().isoformat()
    return f"{d}|{player.lower()}|{tipo}|{side}|{line}"

def _build_pre_game_alert(player: str, tipo: str, side: str, line: float,
                           pre: int, meta: dict, extra_flags: List[str]) -> str:
    """Formatea el mensaje de alerta pre-partido."""
    tipo_icon = {"puntos": "🏀", "rebotes": "💪", "asistencias": "🎯"}.get(tipo, "•")
    pre_emoji = _pre_rating_emoji(pre)
    pre_bar   = _pre_bar(pre)
    pre_lbl   = _pre_label(pre)

    h5  = meta.get("hits5",  "?")
    n5  = meta.get("n5",     "?")
    h10 = meta.get("hits10", "?")
    n10 = meta.get("n10",    "?")
    avg = meta.get("avg10",  None)

    flags_str = "\n".join(f"  ⚡ {f}" for f in extra_flags) if extra_flags else ""

    msg = (
        f"🔔 *ALERTA PRE-PARTIDO*\n"
        f"{'─'*28}\n"
        f"👤 *{player}*\n"
        f"{tipo_icon} {tipo.upper()} *{side.upper()}* `{line}`\n\n"
        f"{pre_emoji} PRE Score: *{pre}/100* {pre_bar}\n"
        f"_{pre_lbl}_ — `{h5}/{n5}` últ.5 | `{h10}/{n10}` últ.10"
        + (f" | prom `{avg:.1f}`" if avg else "") + "\n"
    )
    if flags_str:
        msg += f"\n{flags_str}\n"
    return msg

async def background_smart_alerts(context: ContextTypes.DEFAULT_TYPE):
    """
    Job periódico que analiza props pre-partido y envía alertas cuando
    el PRE score supera el umbral, con señales de contexto adicionales.
    """
    chat_id = context.job.chat_id
    state   = load_smart_alerts_state()
    now     = now_ts()

    props_pm = await asyncio.to_thread(polymarket_props_today_from_scoreboard)
    if not props_pm:
        return

    # Solo props que aún no han empezado (gameStatus == 1)
    try:
        games = await asyncio.to_thread(
            lambda: scoreboard.ScoreBoard().get_dict()["scoreboard"]["games"]
        )
    except Exception:
        return

    pregame_slugs = set()
    for g in games:
        if g.get("gameStatus", 1) == 1:   # 1 = pre-partido
            pregame_slugs.add(_slug_from_scoreboard_game(g))

    # Filtrar solo props de partidos que aún no empiezan
    pregame_props = [p for p in props_pm if (p.game_slug or "") in pregame_slugs and p.side == "over"]

    for p in pregame_props:
        alert_key = _smart_alert_key(p.player, p.tipo, p.side, p.line)
        last_sent = int(state.get(alert_key, 0))
        if now - last_sent < SMART_ALERTS_SENT_TTL:
            continue  # ya enviada hoy

        def _calc(player=p.player, tipo=p.tipo, line=p.line):
            pid = get_pid_for_name(player)
            if not pid:
                return None, 0, {}
            po, _, meta = pre_score_cached(pid, tipo, line)
            return pid, po, meta

        pid, pre_over, meta = await asyncio.to_thread(_calc)
        if not pid or pre_over < SMART_ALERT_THRESH:
            continue

        # Señales adicionales de contexto
        extra_flags: List[str] = []

        def _extra(pid=pid, player=p.player, tipo=p.tipo, line=p.line):
            flags = []
            v10 = last_n_values(pid, tipo, 10)
            if not v10:
                return flags
            # Racha actual
            h3 = sum(1 for v in v10[:3] if v > line)
            if h3 == 3:
                flags.append("En racha: cumplió los últimos 3 partidos ✅")
            # Tendencia alcista
            if len(v10) >= 6 and (sum(v10[:5])/5) > (sum(v10[5:])/len(v10[5:])) + 1.5:
                flags.append("Tendencia ALCISTA en últimos 5 🔺")
            # Back-to-back
            if is_back_to_back(pid):
                flags.append("⚠️ Back-to-back — puede afectar minutos")
            return flags

        extra_flags = await asyncio.to_thread(_extra)

        alert_msg = _build_pre_game_alert(
            p.player, p.tipo, "over", p.line, pre_over, meta, extra_flags
        )

        try:
            await context.bot.send_message(chat_id=chat_id, text=alert_msg, parse_mode=ParseMode.MARKDOWN)
            state[alert_key] = now
            log.info(f"Smart alert enviada: {p.player} {p.tipo} over {p.line} PRE={pre_over}")
        except Exception as e:
            log.warning(f"Error enviando smart alert: {e}")

    save_smart_alerts_state(state)

async def cmd_alertas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /alertas — muestra todas las props pre-partido con PRE ≥ 60, ordenadas.
    """
    msg_wait = await update.message.reply_text(
        "🔍 *Buscando mejores props pre-partido...*",
        parse_mode=ParseMode.MARKDOWN
    )

    props_pm = await asyncio.to_thread(polymarket_props_today_from_scoreboard)
    if not props_pm:
        await msg_wait.edit_text("❌ Sin props disponibles.")
        return

    # Solo OVER (el under se analiza independiente)
    props_over = [p for p in props_pm if p.side == "over"]

    # Calcular PRE con semáforo (máx 4 simultáneos)
    sem = asyncio.Semaphore(4)

    async def _calc_one(p: Prop):
        async with sem:
            def _inner():
                pid = get_pid_for_name(p.player)
                if not pid:
                    return None, 0, {}
                po, pu, meta = pre_score_cached(pid, p.tipo, p.line)
                return pid, po, meta
            try:
                return p, await asyncio.wait_for(asyncio.to_thread(_inner), timeout=25.0)
            except Exception:
                return p, (None, 0, {})

    results = await asyncio.gather(*[_calc_one(p) for p in props_over])

    # Filtrar y ordenar por PRE score
    scored = []
    for prop, (pid, pre, meta) in results:
        if pid and pre >= 55:
            scored.append((pre, prop, meta))
    scored.sort(key=lambda x: x[0], reverse=True)

    if not scored:
        await msg_wait.edit_text(
            "😔 No hay props con PRE ≥ 55 hoy.\n"
            "Usa `/odds` para ver todas con sus scores.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    today_str = date.today().strftime("%d/%m/%Y")
    lines = [
        f"🏆 *MEJORES PROPS HOY — {today_str}*",
        f"_{len(scored)} props con PRE ≥ 55, ordenadas por score_\n",
    ]

    tipo_icon = {"puntos": "🏀", "rebotes": "💪", "asistencias": "🎯"}
    for i, (pre, prop, meta) in enumerate(scored[:20], 1):
        icon     = tipo_icon.get(prop.tipo, "•")
        pre_e    = _pre_rating_emoji(pre)
        bar      = _pre_bar(pre, 6)
        avg10    = meta.get("avg10")
        h5, n5   = meta.get("hits5","?"), meta.get("n5","?")
        h10, n10 = meta.get("hits10","?"), meta.get("n10","?")
        matchup  = _slug_to_matchup(prop.game_slug or "")
        avg_str  = f"prom `{avg10:.1f}`" if avg10 else ""

        lines.append(
            f"*{i}.* {pre_e} `{pre}/100` — *{prop.player}*\n"
            f"   {icon} {prop.tipo.upper()} OVER `{prop.line}`  _{matchup}_\n"
            f"   {bar}  {avg_str}  `{h5}/{n5}` últ5 | `{h10}/{n10}` últ10"
        )

    msg = "\n".join(lines)
    if len(msg) > 3900:
        msg = msg[:3900] + "\n…(recortado)"
    await msg_wait.edit_text(msg, parse_mode=ParseMode.MARKDOWN)


# ================================================================
# BLOQUE 3 — HISTORIAL Y TRACKING DE APUESTAS
# ================================================================

BETS_FILE = "bets.json"

@dataclass
class Bet:
    id: str                    # uuid corto
    user_id: int
    player: str
    tipo: str
    side: str
    line: float
    amount: float              # unidades o $
    pre_score: int
    game_slug: str
    placed_at: int             # timestamp
    result: Optional[str] = None   # "win" | "loss" | "push" | None (pendiente)
    actual_stat: Optional[float] = None
    resolved_at: Optional[int] = None
    notes: str = ""

def load_bets() -> List[Bet]:
    raw = load_json(BETS_FILE, {"bets": []})
    out = []
    for b in raw.get("bets", []):
        try:
            out.append(Bet(**b))
        except Exception:
            pass
    return out

def save_bets(bets: List[Bet]):
    save_json(BETS_FILE, {"bets": [asdict(b) for b in bets]})

def _new_bet_id() -> str:
    import uuid
    return str(uuid.uuid4())[:8].upper()

def _parse_bet_command(text: str) -> Optional[dict]:
    """
    Parsea: /bet Jugador | tipo | side | linea | monto
    Ej:     /bet Nikola Jokic | puntos | over | 27.5 | 50
    """
    body = re.sub(r"^/bet(@\w+)?\s*", "", text).strip()
    parts = [x.strip() for x in body.split("|")]
    if len(parts) < 4:
        return None
    player, tipo, side, line_s = parts[0], parts[1], parts[2], parts[3]
    amount_s = parts[4] if len(parts) >= 5 else "1"
    tipo = tipo.lower(); side = side.lower()
    if tipo not in ("puntos","rebotes","asistencias"): return None
    if side not in ("over","under"): return None
    try:
        line   = float(line_s)
        amount = float(amount_s)
    except Exception:
        return None
    return {"player": player, "tipo": tipo, "side": side, "line": line, "amount": amount}

async def cmd_bet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /bet Jugador | tipo | side | linea | monto
    Registra una apuesta y calcula el PRE score.
    """
    parsed = _parse_bet_command(update.message.text or "")
    if not parsed:
        await update.message.reply_text(
            "Formato: `/bet Jugador | tipo | side | linea | monto`\n"
            "Ej: `/bet Nikola Jokic | puntos | over | 27.5 | 50`\n"
            "_monto es opcional (default: 1 unidad)_",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    msg_wait = await update.message.reply_text("⏳ Registrando apuesta...", parse_mode=ParseMode.MARKDOWN)
    user_id = update.effective_user.id if update.effective_user else 0

    def _calc():
        pid = get_pid_for_name(parsed["player"])
        if not pid:
            return None, 0, {}, ""
        po, pu, meta = pre_score_cached(pid, parsed["tipo"], parsed["line"])
        pre = po if parsed["side"] == "over" else pu
        # Buscar game_slug
        slug = ""
        for p in PM_CACHE.get("props", []):
            if p.player.lower() == parsed["player"].lower():
                slug = p.game_slug or ""
                break
        return pid, pre, meta, slug

    pid, pre, meta, slug = await asyncio.to_thread(_calc)

    if not pid:
        await msg_wait.edit_text(f"⚠️ Jugador no encontrado: *{parsed['player']}*", parse_mode=ParseMode.MARKDOWN)
        return

    bet = Bet(
        id          = _new_bet_id(),
        user_id     = user_id,
        player      = parsed["player"],
        tipo        = parsed["tipo"],
        side        = parsed["side"],
        line        = parsed["line"],
        amount      = parsed["amount"],
        pre_score   = pre,
        game_slug   = slug,
        placed_at   = now_ts(),
    )

    bets = load_bets()
    bets.append(bet)
    save_bets(bets)

    pre_e   = _pre_rating_emoji(pre)
    pre_bar = _pre_bar(pre)
    tipo_icon = {"puntos": "🏀", "rebotes": "💪", "asistencias": "🎯"}.get(parsed["tipo"], "•")

    confirm = (
        f"✅ *Apuesta registrada* `#{bet.id}`\n"
        f"{'─'*28}\n"
        f"👤 *{bet.player}*\n"
        f"{tipo_icon} {bet.tipo.upper()} *{bet.side.upper()}* `{bet.line}`\n"
        f"💰 Monto: `{bet.amount}` unidades\n"
        f"{pre_e} PRE Score: `{pre}/100` {pre_bar}\n"
        f"📅 {date.today().strftime('%d/%m/%Y')}\n\n"
        f"_Usa `/resultado {bet.id} WIN 28` cuando termine el partido_"
    )
    await msg_wait.edit_text(confirm, parse_mode=ParseMode.MARKDOWN)

async def cmd_resultado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /resultado ID WIN|LOSS|PUSH stat_real
    Ej: /resultado A3F2B1C0 WIN 29.0
    """
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Formato: `/resultado ID WIN|LOSS|PUSH stat_real`\n"
            "Ej: `/resultado A3F2B1C0 WIN 29`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    bet_id   = args[0].upper()
    result   = args[1].upper()
    actual   = float(args[2]) if len(args) >= 3 else None

    if result not in ("WIN","LOSS","PUSH"):
        await update.message.reply_text("Resultado debe ser WIN, LOSS o PUSH")
        return

    bets = load_bets()
    found = None
    for b in bets:
        if b.id == bet_id:
            found = b
            b.result       = result.lower()
            b.actual_stat  = actual
            b.resolved_at  = now_ts()
            break

    if not found:
        await update.message.reply_text(f"No encontré la apuesta `{bet_id}`", parse_mode=ParseMode.MARKDOWN)
        return

    save_bets(bets)

    emoji = {"win": "✅", "loss": "❌", "push": "🔁"}.get(result.lower(), "❓")
    actual_str = f" | stat real: `{actual}`" if actual else ""
    await update.message.reply_text(
        f"{emoji} Apuesta `#{bet_id}` → *{result}*{actual_str}\n"
        f"👤 {found.player} — {found.tipo.upper()} {found.side.upper()} `{found.line}`",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_historial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /historial — muestra estadísticas completas de tus apuestas.
    /historial 30 — últimos 30 días (default: 30)
    """
    args = context.args or []
    days = int(args[0]) if args and args[0].isdigit() else 30
    user_id = update.effective_user.id if update.effective_user else 0
    cutoff  = now_ts() - days * 86400

    bets = load_bets()
    # Filtrar por usuario y periodo
    mine = [b for b in bets if b.user_id == user_id and b.placed_at >= cutoff]

    if not mine:
        await update.message.reply_text(
            f"No tienes apuestas registradas en los últimos {days} días.\n"
            "Usa `/bet` para registrar una.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    resolved   = [b for b in mine if b.result in ("win","loss","push")]
    pending    = [b for b in mine if not b.result]
    wins       = [b for b in resolved if b.result == "win"]
    losses     = [b for b in resolved if b.result == "loss"]
    pushes     = [b for b in resolved if b.result == "push"]

    total_res  = len(wins) + len(losses)
    win_rate   = round(len(wins) / total_res * 100, 1) if total_res else 0
    net_units  = sum(b.amount for b in wins) - sum(b.amount for b in losses)
    roi        = round(net_units / sum(b.amount for b in resolved) * 100, 1) if resolved else 0

    # Stats por tipo
    def type_stats(bets_list, tipo):
        sub = [b for b in bets_list if b.tipo == tipo and b.result in ("win","loss")]
        w   = sum(1 for b in sub if b.result == "win")
        return f"`{w}/{len(sub)}`" if sub else "—"

    # Stats por side
    def side_stats(bets_list, side):
        sub = [b for b in bets_list if b.side == side and b.result in ("win","loss")]
        w   = sum(1 for b in sub if b.result == "win")
        return f"`{w}/{len(sub)}`" if sub else "—"

    # Mejor y peor racha
    def calc_streaks(bets_sorted):
        best = cur_best = 0
        worst = cur_worst = 0
        for b in bets_sorted:
            if b.result == "win":
                cur_best += 1; cur_worst = 0
            elif b.result == "loss":
                cur_worst += 1; cur_best = 0
            best  = max(best,  cur_best)
            worst = max(worst, cur_worst)
        return best, worst

    res_sorted = sorted(resolved, key=lambda b: b.placed_at)
    best_streak, worst_streak = calc_streaks(res_sorted)

    # Props más rentables
    player_stats: Dict[str, dict] = {}
    for b in resolved:
        ps = player_stats.setdefault(b.player, {"w":0,"l":0,"net":0})
        if b.result == "win":   ps["w"]+=1; ps["net"]+=b.amount
        elif b.result == "loss": ps["l"]+=1; ps["net"]-=b.amount

    top_players = sorted(player_stats.items(), key=lambda x: x[1]["net"], reverse=True)[:3]

    net_sign = "+" if net_units >= 0 else ""
    roi_sign = "+" if roi >= 0 else ""
    roi_emoji = "🟢" if roi > 0 else ("🔴" if roi < 0 else "⚪")

    msg = (
        f"📊 *MI HISTORIAL — últimos {days} días*\n"
        f"{'─'*30}\n"
        f"📝 Total apuestas: `{len(mine)}`  "
        f"(resueltas: `{len(resolved)}` | pendientes: `{len(pending)}`)\n\n"
        f"✅ Wins:  `{len(wins)}`    ❌ Losses: `{len(losses)}`    🔁 Push: `{len(pushes)}`\n"
        f"🎯 Win rate: *{win_rate}%*\n"
        f"{roi_emoji} ROI: *{roi_sign}{roi}%*  |  Neto: `{net_sign}{net_units:.1f}` unidades\n\n"
        f"{'─'*30}\n"
        f"*Por tipo:*\n"
        f"  🏀 Puntos:     {type_stats(resolved,'puntos')}\n"
        f"  💪 Rebotes:    {type_stats(resolved,'rebotes')}\n"
        f"  🎯 Asistencias:{type_stats(resolved,'asistencias')}\n\n"
        f"*Por lado:*\n"
        f"  📈 Over:  {side_stats(resolved,'over')}\n"
        f"  📉 Under: {side_stats(resolved,'under')}\n\n"
        f"*Rachas:* mejor `{best_streak}W` | peor `{worst_streak}L`\n\n"
    )

    if top_players:
        msg += f"*🏆 Top jugadores (por ganancia):*\n"
        for pl, st in top_players:
            sign = "+" if st["net"] >= 0 else ""
            msg += f"  • {pl}: `{st['w']}W/{st['l']}L`  {sign}{st['net']:.1f}u\n"

    if pending:
        msg += f"\n*⏳ Pendientes ({len(pending)}):*\n"
        for b in pending[-5:]:
            msg += f"  `#{b.id}` {b.player} {b.tipo.upper()} {b.side.upper()} `{b.line}` — `{b.amount}`u\n"
        if len(pending) > 5:
            msg += f"  _...y {len(pending)-5} más_\n"

    msg += f"\n_Usa `/resultado ID WIN|LOSS stat` para resolver_"

    if len(msg) > 3900:
        msg = msg[:3900] + "\n…"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def cmd_misapuestas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /misapuestas — lista las apuestas pendientes del usuario.
    """
    user_id = update.effective_user.id if update.effective_user else 0
    bets    = load_bets()
    pending = [b for b in bets if b.user_id == user_id and not b.result]

    if not pending:
        await update.message.reply_text(
            "No tienes apuestas pendientes.\nUsa `/bet` para registrar una.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    tipo_icon = {"puntos": "🏀", "rebotes": "💪", "asistencias": "🎯"}
    lines = [f"⏳ *Apuestas pendientes* ({len(pending)})\n"]
    for b in sorted(pending, key=lambda x: x.placed_at, reverse=True):
        icon     = tipo_icon.get(b.tipo, "•")
        pre_e    = _pre_rating_emoji(b.pre_score)
        matchup  = _slug_to_matchup(b.game_slug) if b.game_slug else "—"
        ts_str   = time.strftime("%d/%m %H:%M", time.localtime(b.placed_at))
        lines.append(
            f"`#{b.id}` {icon} *{b.player}*\n"
            f"  {b.tipo.upper()} {b.side.upper()} `{b.line}` — `{b.amount}`u\n"
            f"  {pre_e} PRE `{b.pre_score}/100` | {matchup} | {ts_str}"
        )

    msg = "\n\n".join(lines)
    if len(msg) > 3900:
        msg = msg[:3900] + "\n…"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


# =========================
# Main
# =========================
async def on_startup(app: Application):
    log.info("Bot arrancado.")

async def register_job(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    # Job: scan en vivo
    if not context.job_queue.get_jobs_by_name(f"scan:{chat_id}"):
        context.job_queue.run_repeating(
            background_scan,
            interval=POLL_SECONDS,
            first=5,
            chat_id=chat_id,
            name=f"scan:{chat_id}",
        )
        await update.message.reply_text(f"✅ Scan en vivo activado (cada {POLL_SECONDS}s).")

    # Job: alertas pre-partido (cada 30 min)
    if not context.job_queue.get_jobs_by_name(f"smart:{chat_id}"):
        context.job_queue.run_repeating(
            background_smart_alerts,
            interval=30 * 60,
            first=15,
            chat_id=chat_id,
            name=f"smart:{chat_id}",
        )
        await update.message.reply_text("✅ Alertas pre-partido activadas (cada 30min).")

    await cmd_help(update, context)

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Comandos principales
    app.add_handler(CommandHandler("start",       register_job))
    app.add_handler(CommandHandler("help",        cmd_help))
    app.add_handler(CommandHandler("games",       cmd_games))
    app.add_handler(CommandHandler("today",       cmd_games))
    app.add_handler(CommandHandler("odds",        cmd_odds))
    app.add_handler(CommandHandler("live",        cmd_live))
    app.add_handler(CommandHandler("lineup",      cmd_lineup))
    app.add_handler(CommandHandler("debug",       cmd_debug))

    # Análisis avanzado
    app.add_handler(CommandHandler("analisis",    cmd_analisis))
    app.add_handler(CommandHandler("alertas",     cmd_alertas))

    # Tracking de apuestas
    app.add_handler(CommandHandler("bet",         cmd_bet))
    app.add_handler(CommandHandler("resultado",   cmd_resultado))
    app.add_handler(CommandHandler("historial",   cmd_historial))
    app.add_handler(CommandHandler("misapuestas", cmd_misapuestas))

    # Manual
    app.add_handler(CommandHandler("add",         cmd_add))

    app.post_init = on_startup
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
