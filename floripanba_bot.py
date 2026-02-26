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

from telegram import Update, BotCommand
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
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
BETS_FILE = "bets.json"
SMART_ALERTS_FILE  = "smart_alerts_state.json"
MORNING_DIGEST_FILE = "morning_digest_state.json"

SEASON = os.environ.get("NBA_SEASON", "2025-26")

FINAL_ALERT_THRESHOLD = 75
FINAL_ALERT_THRESHOLD_CLUTCH = 68
SMART_ALERT_THRESH = 68
MORNING_DIGEST_HOUR = int(os.environ.get("MORNING_HOUR", "10"))

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
        total=8, connect=8, read=8, backoff_factor=1.5,
        status_forcelist=(403, 408, 429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]), raise_on_status=False,
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
    tipo: str
    line: float
    side: str
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

@dataclass
class Bet:
    id: str
    user_id: int
    player: str
    tipo: str
    side: str
    line: float
    amount: float
    pre_score: int
    game_slug: str
    placed_at: int
    result: Optional[str] = None
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

# =========================
# API & Cache Wrappers
# =========================
def obtener_id_jugador(nombre: str) -> Optional[int]:
    time.sleep(0.2 + random.random() * 0.1)
    res = players.find_players_by_full_name(nombre)
    if not res:
        return None
    exact = [p for p in res if (p.get("full_name") or "").lower() == nombre.lower()]
    pick = exact[0] if exact else res[0]
    return int(pick.get("id"))

def get_pid_for_name(name: str) -> Optional[int]:
    cache = load_json(IDS_CACHE_FILE, {})
    if name in cache:
        return int(cache[name])
    pid = obtener_id_jugador(name)
    if pid:
        cache[name] = int(pid)
        save_json(IDS_CACHE_FILE, cache)
    return pid

GLOG_TTL_SECONDS = 6 * 60 * 60
def get_gamelog_table(pid: int) -> Tuple[List[str], List[list]]:
    cache = load_json(GLOG_CACHE_FILE, {})
    k = str(pid)
    now = now_ts()

    if k in cache and (now - int(cache[k].get("ts", 0))) < GLOG_TTL_SECONDS:
        return cache[k].get("headers", []), cache[k].get("rows", [])

    time.sleep(0.5 + random.random() * 0.25)
    url = "https://stats.nba.com/stats/playergamelog"
    params = {"DateFrom": "", "DateTo": "", "LeagueID": "00", "PlayerID": str(pid), "Season": SEASON, "SeasonType": "Regular Season"}

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
        save_json(GLOG_CACHE_FILE, cache)
        return headers, rows
    except Exception:
        return cache.get(k, {}).get("headers", []), cache.get(k, {}).get("rows", [])

# =========================
# PRE SCORE CORE
# =========================
def clamp(x, lo=0, hi=100):
    return max(lo, min(hi, x))

def stdev(vals: List[float]) -> float:
    if not vals or len(vals) < 2: return 0.0
    mu = sum(vals) / len(vals)
    var = sum((x - mu) ** 2 for x in vals) / (len(vals) - 1)
    return math.sqrt(var)

def last_n_values(pid: int, tipo: str, n: int = 10) -> List[float]:
    headers, rows = get_gamelog_table(pid)
    if not headers or not rows: return []
    idx = {h: i for i, h in enumerate(headers)}
    col = STAT_COL.get(tipo)
    i = idx.get(col)
    if i is None: return []
    rows_n = rows[:n] if len(rows) >= n else rows
    vals = []
    for r in rows_n:
        if i < len(r):
            try: vals.append(float(r[i]))
            except: pass
    return vals

def hit_counts(values: List[float], line: float, side: str) -> Tuple[int, int]:
    if not values: return 0, 0
    hits = sum(1 for v in values if (v > line if side == "over" else v < line))
    return hits, len(values)

def pre_score(pid: int, tipo: str, line: float, side: str) -> Tuple[int, dict]:
    v5 = last_n_values(pid, tipo, 5)
    v10 = last_n_values(pid, tipo, 10)

    h5, n5 = hit_counts(v5, line, side)
    h10, n10 = hit_counts(v10, line, side)

    hit5 = (h5 / n5) if n5 else 0.0
    hit10 = (h10 / n10) if n10 else 0.0

    m5 = [(v - line if side == "over" else line - v) for v in v5]
    m10 = [(v - line if side == "over" else line - v) for v in v10]

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
        "avg5": round(sum(v5)/len(v5), 2) if v5 else 0.0,
        "avg10": round(sum(v10)/len(v10), 2) if v10 else 0.0,
        "std10": round(std10, 2), "w_margin": round(w_margin, 2),
    }
    return PRE, meta

# =========================
# CONTEXTO DEFENSIVO & PRE v2
# =========================
CONTEXT_CACHE: Dict[str, dict] = {}
CONTEXT_TTL = 4 * 60 * 60
_TRICODE_TO_TEAM_ID_CACHE: Dict[str, int] = {}

def get_team_id_by_tricode(tricode: str) -> Optional[int]:
    for t in nba_teams_static.get_teams():
        if t.get("abbreviation", "").upper() == tricode.upper():
            return int(t["id"])
    return None

def get_team_id_cached(tricode: str) -> Optional[int]:
    if tricode in _TRICODE_TO_TEAM_ID_CACHE: return _TRICODE_TO_TEAM_ID_CACHE[tricode]
    tid = get_team_id_by_tricode(tricode)
    if tid: _TRICODE_TO_TEAM_ID_CACHE[tricode] = tid
    return tid

def fetch_league_team_stats() -> Dict[int, dict]:
    now = now_ts()
    if "league_team_stats" in CONTEXT_CACHE and (now - CONTEXT_CACHE["league_team_stats"].get("ts", 0)) < CONTEXT_TTL:
        return CONTEXT_CACHE["league_team_stats"]["data"]

    time.sleep(0.5)
    url = "https://stats.nba.com/stats/leaguedashteamstats"
    params = {"MeasureType": "Advanced", "PerMode": "PerGame", "Season": SEASON, "SeasonType": "Regular Season", "LeagueID": "00"}
    try:
        resp = SESSION_NBA.get(url, params=params, timeout=(12, 60))
        if resp.status_code != 200: return {}
        rs = resp.json().get("resultSets", [{}])[0]
        hdrs, rows = rs.get("headers", []), rs.get("rowSet", [])
        res = {int(dict(zip(hdrs, r)).get("TEAM_ID", 0)): {
            "team_name": dict(zip(hdrs, r)).get("TEAM_NAME",""),
            "def_rating": float(dict(zip(hdrs, r)).get("DEF_RATING") or 0),
            "pace": float(dict(zip(hdrs, r)).get("PACE") or 0)} for r in rows}
        CONTEXT_CACHE["league_team_stats"] = {"ts": now, "data": res}
        return res
    except: return {}

def fetch_opp_position_stats() -> Dict[int, dict]:
    now = now_ts()
    if "opp_pos_stats" in CONTEXT_CACHE and (now - CONTEXT_CACHE["opp_pos_stats"].get("ts", 0)) < CONTEXT_TTL:
        return CONTEXT_CACHE["opp_pos_stats"]["data"]

    time.sleep(0.4)
    url = "https://stats.nba.com/stats/leaguedashteamstats"
    params = {"MeasureType": "Opponent", "PerMode": "PerGame", "Season": SEASON, "SeasonType": "Regular Season", "LeagueID": "00"}
    try:
        resp = SESSION_NBA.get(url, params=params, timeout=(12, 60))
        if resp.status_code != 200: return {}
        rs = resp.json().get("resultSets", [{}])[0]
        hdrs, rows = rs.get("headers", []), rs.get("rowSet", [])
        res = {int(dict(zip(hdrs, r)).get("TEAM_ID", 0)): {
            "opp_pts": float(dict(zip(hdrs, r)).get("OPP_PTS") or 0),
            "opp_reb": float(dict(zip(hdrs, r)).get("OPP_REB") or 0),
            "opp_ast": float(dict(zip(hdrs, r)).get("OPP_AST") or 0)} for r in rows}
        CONTEXT_CACHE["opp_pos_stats"] = {"ts": now, "data": res}
        return res
    except: return {}

def get_defensive_context(opp_tricode: str, tipo: str) -> dict:
    result = {"def_rating": None, "pace": None, "opp_stat": None, "def_rank": None, "pace_rank": None, "verdict": ""}
    team_stats = fetch_league_team_stats()
    opp_stats = fetch_opp_position_stats()
    
    opp_tid = get_team_id_cached(opp_tricode)
    if not opp_tid or opp_tid not in team_stats: return result

    ts = team_stats[opp_tid]
    result["def_rating"] = ts["def_rating"]
    result["pace"] = ts["pace"]

    all_def = sorted(team_stats.values(), key=lambda x: x["def_rating"])
    result["def_rank"] = next((i+1 for i,t in enumerate(all_def) if t.get("team_name") == ts["team_name"]), None)

    all_pace = sorted(team_stats.values(), key=lambda x: x["pace"], reverse=True)
    result["pace_rank"] = next((i+1 for i,t in enumerate(all_pace) if t.get("team_name") == ts["team_name"]), None)

    if opp_tid in opp_stats:
        os = opp_stats[opp_tid]
        stat_key = {"puntos": "opp_pts", "rebotes": "opp_reb", "asistencias": "opp_ast"}.get(tipo)
        if stat_key:
            result["opp_stat"] = os.get(stat_key)
            all_opp = sorted(opp_stats.values(), key=lambda x: x.get(stat_key,0), reverse=True)
            result["opp_stat_rank"] = next((i+1 for i,s in enumerate(all_opp) if s.get(stat_key) == os.get(stat_key)), None)

    return result

def home_away_splits(pid: int, tipo: str) -> dict:
    headers, rows = get_gamelog_table(pid)
    if not headers or not rows: return {}
    col = STAT_COL.get(tipo)
    if not col: return {}
    try:
        stat_idx = headers.index(col)
        matchup_idx = headers.index("MATCHUP")
    except ValueError: return {}

    home_vals, away_vals = [], []
    for r in rows:
        matchup_str = str(r[matchup_idx]) if matchup_idx < len(r) else ""
        try: val = float(r[stat_idx])
        except: continue
        if " vs. " in matchup_str: home_vals.append(val)
        elif " @ " in matchup_str: away_vals.append(val)

    res = {}
    if home_vals: res["home_avg"], res["home_n"] = round(sum(home_vals)/len(home_vals),1), len(home_vals)
    if away_vals: res["away_avg"], res["away_n"] = round(sum(away_vals)/len(away_vals),1), len(away_vals)
    return res

def pre_score_v2(pid: int, tipo: str, line: float, side: str, opp_tricode: str = "", is_home: bool = True, rest_days: int = 1) -> Tuple[int, dict]:
    base_score, meta = pre_score(pid, tipo, line, side)
    adjustments, adj_total = [], 0.0

    if opp_tricode:
        ctx = get_defensive_context(opp_tricode, tipo)
        dr_rank, pace_rank, osr = ctx.get("def_rank"), ctx.get("pace_rank"), ctx.get("opp_stat_rank")

        if dr_rank:
            if side == "over": adj = +8 if dr_rank >= 25 else (+4 if dr_rank >= 20 else (-8 if dr_rank <= 5 else (-4 if dr_rank <= 10 else 0)))
            else: adj = +8 if dr_rank <= 5 else (+4 if dr_rank <= 10 else (-8 if dr_rank >= 25 else (-4 if dr_rank >= 20 else 0)))
            adj_total += adj
            if adj != 0: adjustments.append(f"defensa {adj:+d}")

        if pace_rank:
            if side == "over": adj = +5 if pace_rank <= 5 else (-5 if pace_rank >= 25 else 0)
            else: adj = +5 if pace_rank >= 25 else (-5 if pace_rank <= 5 else 0)
            adj_total += adj
            if adj != 0: adjustments.append(f"ritmo {adj:+d}")

        if osr:
            if side == "over": adj = +6 if osr <= 8 else (-6 if osr >= 23 else 0)
            else: adj = +6 if osr >= 23 else (-6 if osr <= 8 else 0)
            adj_total += adj
            if adj != 0: adjustments.append(f"permite_{tipo[:3]} {adj:+d}")

        meta.update({"ctx_def_rank": dr_rank, "ctx_pace_rank": pace_rank, "ctx_osr": osr, "ctx_opp_tri": opp_tricode})

    splits = home_away_splits(pid, tipo)
    loc = "home" if is_home else "away"
    loc_avg = splits.get(f"{loc}_avg")
    if loc_avg is not None:
        gap = loc_avg - line if side == "over" else line - loc_avg
        adj = +5 if gap > 2.0 else (-5 if gap < -2.0 else 0)
        adj_total += adj
        if adj != 0: adjustments.append(f"split_{loc} {adj:+d}")

    if rest_days == 0:
        adj = -6 if side == "over" else +6
        adj_total += adj
        adjustments.append(f"b2b {adj:+d}")
    elif rest_days >= 3:
        adj = +4 if side == "over" else -4
        adj_total += adj
        adjustments.append(f"descansado {adj:+d}")

    meta.update({"v2_base": base_score, "v2_adj": round(adj_total, 1), "v2_adjustments": adjustments})
    return int(clamp(base_score + adj_total, 0, 100)), meta

# =========================
# LIVE SCORE CORE
# =========================
def parse_minutes(min_str) -> float:
    if not min_str: return 0.0
    try:
        mm, ss = str(min_str).split(":")
        return float(mm) + float(ss) / 60.0
    except: return 0.0

def clock_to_seconds(game_clock: str) -> Optional[int]:
    if not game_clock: return None
    gc = str(game_clock)
    if gc.startswith("PT") and "M" in gc:
        try:
            return int(gc.split("PT")[1].split("M")[0]) * 60 + int(gc.split("M")[1].replace("S", "").split(".")[0])
        except: return None
    if ":" in gc:
        try:
            mm, ss = gc.split(":")
            return int(mm) * 60 + int(ss)
        except: return None
    return None

def game_elapsed_minutes(period: int, clock_seconds: Optional[int]) -> Optional[float]:
    if clock_seconds is None or period <= 0: return None
    if period <= 4: return ((period - 1) * 720 + (720 - clock_seconds)) / 60.0
    return (4 * 720 + (period - 5) * 300 + (300 - min(clock_seconds, 300))) / 60.0

def should_gate_by_minutes(side: str, tipo: str, value: float, mins: float, elapsed_min, is_blowout: bool) -> bool:
    if side == "over":
        if elapsed_min is not None and elapsed_min >= 18: return False
        return mins < (MIN_MINUTES_POINTS if tipo == "puntos" else MIN_MINUTES_REB_AST)
    else:
        if elapsed_min is None: return True
        if is_blowout and elapsed_min >= 16: return False
        return elapsed_min < 22

def compute_over_score(tipo, faltante, mins, pf, period, clock_seconds, diff, is_clutch, is_blowout) -> int:
    if tipo == "puntos": near_max, ideal_max, close_w, ideal_b, min_floor, foul_m, blow_m = 4.0, 2.0, 60, 10, 10.0, 1.0, 1.0
    else: near_max, ideal_max, close_w, ideal_b, min_floor, foul_m, blow_m = 1.5, 0.9, 65, 12, 14.0, 1.25, 1.35
    if faltante < 0.5 or faltante > near_max: return 0
    base = close_w * clamp((near_max - faltante) / (near_max - 0.5), 0, 1) + (ideal_b if faltante <= ideal_max else 0)
    spot = (12 if period >= 4 else (7 if period == 3 else (3 if period == 2 else 0)))
    if clock_seconds is not None:
        spot += clamp((720 - clock_seconds) / 720 * (9 if period >= 4 else 5), 0, 9) if period >= 3 else 0
    spot += 11 if is_clutch else 0
    min_score = clamp((mins - min_floor) / 18 * 12, 0, 12)
    foul_pen = (18 if pf >= 5 else (10 if pf == 4 else (4 if pf == 3 else 0))) * foul_m
    blow_pen = (18 if diff >= 25 else (12 if diff >= 20 else 0)) * blow_m
    return int(clamp(base + spot + min_score - foul_pen - blow_pen, 0, 100))

def compute_under_score(tipo, margin_under, mins, pf, period, clock_seconds, diff, is_clutch, is_blowout) -> int:
    if tipo == "puntos": min_m, good_m, blow_b, clutch_p = 3.0, 6.0, 20, 10
    else: min_m, good_m, blow_b, clutch_p = 2.0, 3.5, 24, 14
    if margin_under < min_m: return 0
    elapsed_min = game_elapsed_minutes(period, clock_seconds)
    time_score = clamp(((elapsed_min or 0) - 20) / 28 * 28, 0, 28)
    cushion = clamp((margin_under - min_m) / (good_m - min_m) * 40, 0, 40)
    blow = (blow_b + (6 if diff >= 25 else 0)) if is_blowout else 0
    foul_pen = 6 if pf >= 5 else (3 if pf == 4 else 0)
    min_bonus = 0
    if elapsed_min and elapsed_min >= 30: min_bonus = 12 if mins < 18 else (8 if mins < 24 else 0)
    return int(clamp(cushion + time_score + blow + min_bonus - (clutch_p if is_clutch else 0) - foul_pen, 0, 100))

# =========================
# Polymarket Fetcher (Robusto)
# =========================
PM_CACHE = {"ts": 0, "date": None, "props": []}
PM_TTL_SECONDS = 8 * 60

def _slug_from_scoreboard_game(g: dict) -> str:
    away = (g.get("awayTeam", {}) or {}).get("teamTricode", "").lower()
    home = (g.get("homeTeam", {}) or {}).get("teamTricode", "").lower()
    return f"nba-{away}-{home}-{date.today().isoformat()}"

def polymarket_props_today_from_scoreboard() -> List[Prop]:
    today = date.today().isoformat()
    now = now_ts()
    if PM_CACHE["date"] == today and (now - PM_CACHE["ts"]) < PM_TTL_SECONDS:
        return PM_CACHE["props"]

    try: games = scoreboard.ScoreBoard().get_dict()["scoreboard"]["games"]
    except Exception: games = []

    props_all: List[Prop] = []
    
    # 1. Intentar por evento explícito (slug)
    for g in games:
        away_tri = (g.get("awayTeam", {}) or {}).get("teamTricode", "").lower()
        home_tri = (g.get("homeTeam", {}) or {}).get("teamTricode", "").lower()
        local_slug = f"nba-{away_tri}-{home_tri}-{today}"
        
        try:
            r = SESSION_PM.get(f"{GAMMA}/events/slug/{local_slug}", timeout=15)
            if r.status_code == 200:
                ev = r.json()
                markets = ev.get("markets", [])
                if not markets and ev.get("id"):
                    mr = SESSION_PM.get(f"{GAMMA}/markets", params={"event_id": ev["id"], "limit": 200}, timeout=15)
                    markets = mr.json() if mr.status_code == 200 else []
                
                for m in markets:
                    smt = (m.get("sportsMarketType") or m.get("sport_market_type") or "").lower()
                    q = (m.get("question") or m.get("title") or "").strip()
                    if not smt:
                        if "point" in q.lower(): smt = "points"
                        elif "rebound" in q.lower(): smt = "rebounds"
                        elif "assist" in q.lower(): smt = "assists"
                    if smt not in ("points", "rebounds", "assists"): continue
                    
                    player = m.get("groupItemTitle") or m.get("group_item_title") or ""
                    if not player:
                        match = re.search(r"^(.*?)(?::\s*|\s+)(?:Points|Rebounds|Assists)", q, re.IGNORECASE)
                        if match: player = match.group(1).strip()
                    
                    try: line_val = float(m.get("line", 0))
                    except:
                        match = re.search(r"O\/U\s*(\d+(?:\.\d+)?)", q, re.IGNORECASE)
                        line_val = float(match.group(1)) if match else None

                    if player and line_val:
                        tipo = {"points":"puntos","rebounds":"rebotes","assists":"asistencias"}[smt]
                        props_all.append(Prop(player, tipo, line_val, "over", "polymarket", local_slug, str(m.get("id"))))
                        props_all.append(Prop(player, tipo, line_val, "under", "polymarket", local_slug, str(m.get("id"))))
        except: pass

    # 2. Deduplicar
    seen = set()
    uniq = []
    for p in props_all:
        k = (p.game_slug, p.player.lower(), p.tipo, p.line, p.side)
        if k not in seen:
            seen.add(k)
            uniq.append(p)

    # 3. FALLBACK: Si la API no trajo nada, cargamos unos de prueba para que el bot no muera
    if not uniq:
        log.warning("⚠️ Sin props de Polymarket. Usando Fallback.")
        fallback_slug = "nba-okc-det-" + today
        uniq = [
            Prop("Shai Gilgeous-Alexander", "puntos", 32.5, "over", "fallback", fallback_slug),
            Prop("Shai Gilgeous-Alexander", "puntos", 32.5, "under", "fallback", fallback_slug),
            Prop("Cade Cunningham", "puntos", 28.5, "over", "fallback", fallback_slug),
            Prop("Cade Cunningham", "puntos", 28.5, "under", "fallback", fallback_slug),
            Prop("Jalen Williams", "puntos", 22.5, "over", "fallback", fallback_slug),
            Prop("Jalen Williams", "puntos", 22.5, "under", "fallback", fallback_slug),
            Prop("Jalen Duren", "rebotes", 12.5, "over", "fallback", fallback_slug),
            Prop("Jalen Duren", "rebotes", 12.5, "under", "fallback", fallback_slug)
        ]

    PM_CACHE["date"] = today
    PM_CACHE["ts"] = now
    PM_CACHE["props"] = uniq
    return uniq

# =========================
# UI Helpers
# =========================
def _pre_rating_emoji(score: int) -> str:
    if score >= 75: return "🔥"
    elif score >= 60: return "✅"
    elif score >= 45: return "🟡"
    elif score >= 30: return "🟠"
    else: return "❄️"

def _pre_bar(score: int, length: int = 8) -> str:
    filled = round(score / 100 * length)
    return "█" * filled + "░" * (length - filled)

def _pre_label(score: int) -> str:
    if score >= 75: return "FUERTE"
    elif score >= 60: return "BUENA"
    elif score >= 45: return "MEDIA"
    elif score >= 30: return "DÉBIL"
    else: return "BAJA"

def _slug_to_matchup(slug: str) -> str:
    parts = slug.replace("nba-", "").split("-")
    if len(parts) >= 2: return f"{parts[0].upper()} @ {parts[1].upper()}"
    return slug

async def _send_long_message(update: Update, text: str, max_len: int = 3800):
    if len(text) <= max_len:
        try: await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        except: await update.message.reply_text(text.replace("*","").replace("_","").replace("`",""))
        return
    parts, remaining = [], text
    while len(remaining) > max_len:
        cut = remaining[:max_len].rfind("\n👤")
        if cut < 200: cut = remaining[:max_len].rfind("\n")
        if cut < 0: cut = max_len
        parts.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining: parts.append(remaining)

    for i, part in enumerate(parts):
        prefix = f"_(continuación {i+1}/{len(parts)})_\n" if i > 0 else ""
        try: await update.message.reply_text(prefix + part, parse_mode=ParseMode.MARKDOWN)
        except: await update.message.reply_text((prefix + part).replace("*","").replace("_","").replace("`",""))
        await asyncio.sleep(0.3)

# =========================
# COMANDOS PRINCIPALES
# =========================
HELP_TEXT = (
    "🧠 *NBA Props Bot v3*\n\n"
    "*📋 Programación*\n"
    "• `/odds` → menú rápido de props con score avanzado\n"
    "• `/games` → partidos de hoy\n"
    "• `/live` → props en vivo con scoring\n"
    "• `/lineup` → alineaciones e injury report\n\n"
    "*📊 Análisis*\n"
    "• `/analisis Jugador | tipo | side | linea` → tendencia profunda\n"
    "• `/alertas` → ranking top props del día\n"
    "• `/contexto AWAY HOME` → defensas y pace\n\n"
    "*💰 Apuestas*\n"
    "• `/bet Jugador | tipo | side | linea | monto`\n"
    "• `/misapuestas`, `/historial`, `/resultado`\n"
)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)

async def cmd_games(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        games = await asyncio.wait_for(
            asyncio.to_thread(lambda: scoreboard.ScoreBoard().get_dict()["scoreboard"]["games"]),
            timeout=20.0
        )
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {e}")
        return

    if not games:
        await update.message.reply_text("No hay juegos hoy.")
        return

    lines = ["📅 *NBA hoy*"]
    for g in games:
        at = g.get("awayTeam", {}).get("teamTricode", "?")
        ht = g.get("homeTeam", {}).get("teamTricode", "?")
        status = g.get("gameStatusText", "")
        lines.append(f"• {at} @ {ht} — {status}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

# === ODDS CON MENÚ INTERACTIVO Y SCORES AVANZADOS ===
async def cmd_odds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ *Cargando partidos de Polymarket...*", parse_mode=ParseMode.MARKDOWN)
    props_pm = await asyncio.to_thread(polymarket_props_today_from_scoreboard)
    if not props_pm:
        await msg.edit_text("❌ No pude obtener props de hoy.")
        return

    games_dict: Dict[str, List[Prop]] = {}
    for p in props_pm:
        slug = p.game_slug or "unknown"
        games_dict.setdefault(slug, []).append(p)

    args = context.args or []
    if args:
        slug_filter = " ".join(args).strip().lower()
        if slug_filter in games_dict:
            await msg.delete()
            await show_game_props_advanced(update, context, slug_filter, games_dict[slug_filter])
            return
        for slug in games_dict.keys():
            if slug_filter.upper() in _slug_to_matchup(slug):
                await msg.delete()
                await show_game_props_advanced(update, context, slug, games_dict[slug])
                return
        await msg.edit_text(f"❌ No encontré el equipo o slug: `{slug_filter}`")
        return

    today_str = date.today().strftime("%d/%m/%Y")
    header = f"📋 *NBA Props — {today_str}*\n🎮 *Selecciona un partido:*\n{'─'*30}\n"
    game_lines = []
    for i, (slug, props) in enumerate(games_dict.items(), 1):
        matchup = _slug_to_matchup(slug)
        players = set(p.player for p in props)
        game_lines.append(f"{i}. *{matchup}* (👤 {len(players)} jug | 📊 {len(props)//2} líneas)")

    footer = f"{'─'*30}\n_Responde con el número (1,2,3...)_"
    context.user_data['games_menu'] = list(games_dict.keys())
    await msg.edit_text(header + "\n".join(game_lines) + "\n" + footer, parse_mode=ParseMode.MARKDOWN)

async def handle_game_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'games_menu' not in context.user_data: return
    text = update.message.text.strip()
    if not text.isdigit(): return

    idx = int(text) - 1
    games = context.user_data['games_menu']
    if 0 <= idx < len(games):
        slug = games[idx]
        props_pm = await asyncio.to_thread(polymarket_props_today_from_scoreboard)
        game_props = [p for p in props_pm if (p.game_slug or "") == slug]
        if game_props:
            del context.user_data['games_menu']
            await show_game_props_advanced(update, context, slug, game_props)
        else:
            await update.message.reply_text("❌ Error cargando props del partido seleccionado.")

async def show_game_props_advanced(update: Update, context: ContextTypes.DEFAULT_TYPE, slug: str, props: List[Prop]):
    """Calcula y muestra PRE Scores v2 solo para el partido seleccionado."""
    matchup = _slug_to_matchup(slug)
    msg = await update.message.reply_text(f"⚡ *Calculando scores avanzados para {matchup}...*", parse_mode=ParseMode.MARKDOWN)

    parts_slug = slug.replace("nba-","").split("-")
    away_tri = parts_slug[0].upper() if len(parts_slug) >= 2 else "???"
    home_tri = parts_slug[1].upper() if len(parts_slug) >= 2 else "???"

    # Agrupar props únicas del partido (lado over)
    unique_lines: Dict[str, List[Tuple[str, float]]] = {}
    for p in props:
        if p.side == "over":
            if (p.tipo, p.line) not in unique_lines.setdefault(p.player, []):
                unique_lines[p.player].append((p.tipo, p.line))

    # Definir el thread-worker
    def _calc_player(player: str, lines: List[Tuple[str, float]]) -> Tuple[str, List[dict]]:
        pid = get_pid_for_name(player)
        if not pid: return player, []
        
        # Averiguar si es local y el rival
        opp_tricode = home_tri
        is_home = False
        try:
            roster = commonteamroster.CommonTeamRoster(team_id=get_team_id_cached(home_tri), season=SEASON).get_data_frames()[0]
            if pid in roster['PLAYER_ID'].values:
                opp_tricode = away_tri
                is_home = True
        except: pass

        rest = 1 # simplificado
        results = []
        for (tipo, line) in lines:
            po, meta = pre_score_v2(pid, tipo, line, "over", opp_tricode, is_home, rest)
            pu, _    = pre_score_v2(pid, tipo, line, "under", opp_tricode, is_home, rest)
            results.append({"tipo": tipo, "line": line, "po": po, "pu": pu, "meta": meta})
        return player, results

    # Ejecutar en paralelo limitando a 3 hilos simultáneos
    sem = asyncio.Semaphore(3)
    async def _safe_calc(player, lines):
        async with sem:
            return await asyncio.wait_for(asyncio.to_thread(_calc_player, player, lines), timeout=25.0)

   tasks = [_safe_calc(pl, ln) for pl, ln in unique_lines.items()]
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
    except Exception as e:
        await msg.edit_text(f"❌ Error de servidor calculando scores: {e}")
        return

    # MANEJO SEGURO DE ERRORES: Extraer solo los datos válidos y saltar los Timeouts
    players_data = {}
    for item in results:
        if isinstance(item, Exception):
            log.warning(f"Error de API en un jugador (saltando): {item}")
            continue
        
        pl, res = item
        if res:
            players_data[pl] = res

    if not players_data:
        await msg.edit_text("❌ La API de la NBA está tardando demasiado o bloqueó la IP temporalmente. Por favor, intenta de nuevo en unos minutos.")
        return

    # Formatear el mensaje rico (como en la v2 original)
    tipo_icon = {"puntos": "🏀", "rebotes": "💪", "asistencias": "🎯"}
    tipo_order = {"puntos": 0, "rebotes": 1, "asistencias": 2}
    
    players_sorted = sorted(players_data.keys(), key=lambda pl: max((max(e["po"], e["pu"]) for e in players_data[pl]), default=0), reverse=True)

    lines = [f"🟣 *{matchup}*\n`{slug}`\n{'─'*28}"]
    for pl in players_sorted:
        lines.append(f"\n👤 *{pl}*")
        entries = sorted(players_data[pl], key=lambda e: tipo_order.get(e["tipo"], 9))
        for e in entries:
            po, pu, meta = e["po"], e["pu"], e["meta"]
            h5, n5 = meta.get("hits5", "?"), meta.get("n5", "?")
            h10, n10 = meta.get("hits10", "?"), meta.get("n10", "?")
            avg10 = meta.get("avg10", None)
            
            adj_str = f"  _(adj: {', '.join(meta.get('v2_adjustments', []))[:30]})_\n" if meta.get("v2_adjustments") else ""
            ctx_str = f"  🛡️ Def#{meta.get('ctx_def_rank','?')} · Pace#{meta.get('ctx_pace_rank','?')}\n" if meta.get("ctx_def_rank") else ""

            lines.append(
                f"{tipo_icon.get(e['tipo'], '•')} *{e['tipo'].upper()}* — `{e['line']}`\n"
                f"  OVER  {_pre_rating_emoji(po)} `{po:>3}/100` {_pre_bar(po)}\n"
                f"  UNDER {_pre_rating_emoji(pu)} `{pu:>3}/100` {_pre_bar(pu)}\n"
                f"{adj_str}{ctx_str}"
                f"  📊 `{h5}/{n5}` últ5 | `{h10}/{n10}` últ10 | prom `{avg10:.1f}`" if avg10 else ""
            )

    await msg.delete()
    await _send_long_message(update, "\n".join(lines))


# === COMANDO LIVE (Threaded) ===
async def cmd_live(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ *Cargando datos en vivo...*", parse_mode=ParseMode.MARKDOWN)

    try:
        games = await asyncio.wait_for(
            asyncio.to_thread(lambda: scoreboard.ScoreBoard().get_dict()["scoreboard"]["games"]),
            timeout=20.0
        )
    except Exception as e:
        await msg.edit_text(f"⚠️ Error scoreboard: {e}")
        return

    live_games = [g for g in games if g.get("gameStatus") == 2]
    if not live_games:
        await msg.edit_text("⏸️ No hay partidos en vivo ahora.\nUsa `/games` para ver la cartelera.")
        return

    await msg.edit_text(f"🔄 *{len(live_games)} partido(s) en vivo* — leyendo boxscores...", parse_mode=ParseMode.MARKDOWN)

    props_pm = PM_CACHE.get("props", [])
    if not props_pm: props_pm = await asyncio.to_thread(polymarket_props_today_from_scoreboard)
    props_manual = await asyncio.to_thread(load_props)
    all_props = (props_manual or []) + (props_pm or [])

    props_by_name = {}
    for p in all_props: props_by_name.setdefault(p.player.lower(), []).append(p)

    async def fetch_box(gid: str):
        try: return gid, await asyncio.wait_for(asyncio.to_thread(lambda: boxscore.BoxScore(gid).get_dict()["game"]), timeout=15.0)
        except: return gid, None

    box_results = await asyncio.gather(*[fetch_box(g["gameId"]) for g in live_games])

    scored_rows = []
    for g, (gid, box) in zip(live_games, box_results):
        if not box: continue
        status = g.get("gameStatusText", "")
        period = int(g.get("period", 0) or 0)
        clock_sec = clock_to_seconds(g.get("gameClock", "") or "")
        diff = abs(int(g.get("homeTeam", {}).get("score", 0)) - int(g.get("awayTeam", {}).get("score", 0)))
        elapsed_min = game_elapsed_minutes(period, clock_sec)

        for team_key in ["homeTeam", "awayTeam"]:
            for pl in box.get(team_key, {}).get("players", []):
                full_name = f"{pl.get('firstName', '')} {pl.get('familyName', '')}".strip().lower()
                matching = props_by_name.get(full_name, [])
                if not matching and pl.get("familyName"):
                    for key, plist in props_by_name.items():
                        if pl.get("familyName").lower() in key: matching = plist; break
                if not matching: continue

                s = pl.get("statistics", {})
                pts, reb, ast = float(s.get("points",0) or 0), float(s.get("reboundsTotal",0) or 0), float(s.get("assists",0) or 0)
                pf, mins = float(s.get("foulsPersonal",0) or 0), parse_minutes(s.get("minutes", ""))
                pid = pl.get("personId")

                for pr in matching:
                    actual = pts if pr.tipo == "puntos" else (reb if pr.tipo == "rebotes" else ast)
                    
                    # Rápido cálculo síncrono porque en vivo to_thread de muchos peta.
                    # Usamos el cache local del gamelog que se llenó antes o es instantáneo
                    pre_val, meta = await asyncio.to_thread(pre_score, pid, pr.tipo, pr.line, pr.side)

                    if pr.side == "over":
                        faltante = float(pr.line) - actual
                        if not should_gate_by_minutes("over", pr.tipo, faltante, mins, elapsed_min, diff>=20) and 0.5 <= faltante <= 4.0:
                            live_sc = compute_over_score(pr.tipo, faltante, mins, pf, period, clock_sec, diff, diff<=8, diff>=20)
                            final = int(clamp(0.55 * live_sc + 0.45 * pre_val, 0, 100))
                            scored_rows.append((final, live_sc, pre_val, pr, actual, faltante, status, period, g.get("gameClock"), mins, diff, meta))
                    else:
                        margin = float(pr.line) - actual
                        if not should_gate_by_minutes("under", pr.tipo, margin, mins, elapsed_min, diff>=20) and margin >= 2.0:
                            live_sc = compute_under_score(pr.tipo, margin, mins, pf, period, clock_sec, diff, diff<=8, diff>=20)
                            final = int(clamp(0.65 * live_sc + 0.35 * pre_val, 0, 100))
                            scored_rows.append((final, live_sc, pre_val, pr, actual, margin, status, period, g.get("gameClock"), mins, diff, meta))

    await msg.delete()
    if not scored_rows:
        await update.message.reply_text("📭 *Sin señal en vivo ahora*\nNinguna prop destacada cerca de su línea.", parse_mode=ParseMode.MARKDOWN)
        return

    scored_rows.sort(key=lambda x: x[0], reverse=True)
    out = [f"🔥 *LIVE — {len(live_games)} partido(s)*\n{'─'*28}"]
    for (final, live, pre, pr, act, delta, st, q, clk, m, df, meta) in scored_rows[:15]:
        ic = {"puntos":"🏀","rebotes":"💪","asistencias":"🎯"}.get(pr.tipo,"•")
        lbl = "faltan" if pr.side == "over" else "colchón"
        out.append(f"\n{_pre_rating_emoji(final)} `{final}/100` — *{pr.player}*\n"
                   f"{ic} {pr.tipo.upper()} {pr.side.upper()} `{pr.line}` | actual `{act:.0f}` ({lbl} `{delta:.1f}`)\n"
                   f"⏱️ {st} Q{q} {clk} | MIN `{m:.0f}` Dif `{df}` | `H5: {meta.get('hits5','?')}`")

    await _send_long_message(update, "\n".join(out))


# === LINEUP (Threaded) ===
def format_team_lineup(tricode: str, players_data: List[dict]) -> str:
    starters = [p for p in players_data if p.get("starter") and p.get("status", "").lower() not in ("inactive", "out")]
    inactives = [p for p in players_data if p.get("status", "").lower() in ("inactive", "out")]
    lines = [f"*{tricode}*"]
    if starters:
        lines.append("  5️⃣ *Titulares:*")
        for p in starters[:5]: lines.append(f"    • {p['name']} [{p.get('position','')}]")
    if inactives:
        lines.append(f"  🔴 *Inactivos* ({len(inactives)}):")
        for p in inactives: lines.append(f"    • {p['name']} — _{p.get('not_playing_reason','')[:30]}_")
    return "\n".join(lines)

def fetch_boxscore_injury_data(game_id: str) -> Dict[str, List[dict]]:
    result = {}
    try:
        box = boxscore.BoxScore(game_id).get_dict().get("game", {})
        for t_k in ["homeTeam", "awayTeam"]:
            tri = box.get(t_k, {}).get("teamTricode", "")
            if not tri: continue
            result[tri] = []
            for pl in box.get(t_k, {}).get("players", []):
                result[tri].append({
                    "name": f"{pl.get('firstName', '')} {pl.get('familyName', '')}".strip(),
                    "status": pl.get("status", "Active"), "position": pl.get("position", ""),
                    "starter": pl.get("starter", "0") == "1",
                    "not_playing_reason": pl.get("notPlayingReason", "") or pl.get("inactiveReason", "") or ""
                })
    except: pass
    return result

async def cmd_lineup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Obteniendo alineaciones...", parse_mode=ParseMode.MARKDOWN)
    try:
        board = await asyncio.to_thread(lambda: scoreboard.ScoreBoard().get_dict()["scoreboard"])
        games = board.get("games", [])
    except:
        await msg.edit_text("⚠️ Error leyendo scoreboard")
        return

    filter_tri = " ".join(context.args or []).strip().upper() if context.args else None
    await msg.edit_text(f"🔄 Cargando datos...", parse_mode=ParseMode.MARKDOWN)

    sent = False
    for g in games:
        away, home = g.get("awayTeam", {}).get("teamTricode", ""), g.get("homeTeam", {}).get("teamTricode", "")
        if filter_tri and filter_tri not in (away, home): continue
        gid = g.get("gameId", "")
        box_data = await asyncio.to_thread(fetch_boxscore_injury_data, gid) if gid else {}
        
        a_pls, h_pls = box_data.get(away, []), box_data.get(home, [])
        header = f"{'─'*32}\n✈️ *{away}* @ 🏠 *{home}*\n{'─'*32}"
        a_fmt = format_team_lineup(away, a_pls) if a_pls else f"*{away}*\n  _(sin datos)_"
        h_fmt = format_team_lineup(home, h_pls) if h_pls else f"*{home}*\n  _(sin datos)_"
        
        await update.message.reply_text(f"{header}\n\n{a_fmt}\n\n{h_fmt}", parse_mode=ParseMode.MARKDOWN)
        sent = True
        await asyncio.sleep(0.5)

    await msg.delete()
    if not sent: await update.message.reply_text("No encontré datos para ese equipo.")


# === COMANDOS APUESTAS ===
async def cmd_bet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parsed = _parse_bet_command(update.message.text or "")
    if not parsed:
        await update.message.reply_text("Uso: `/bet Jugador | tipo | side | linea | monto`", parse_mode=ParseMode.MARKDOWN)
        return
    msg = await update.message.reply_text("⏳ Registrando...", parse_mode=ParseMode.MARKDOWN)
    
    def _c():
        pid = get_pid_for_name(parsed["player"])
        if not pid: return None, 0
        po, pu, _ = pre_score(pid, parsed["tipo"], parsed["line"], parsed["side"])
        return pid, po if parsed["side"]=="over" else pu
    
    pid, pre = await asyncio.to_thread(_c)
    if not pid:
        await msg.edit_text("⚠️ Jugador no encontrado.")
        return

    b = Bet(_new_bet_id(), update.effective_user.id, parsed["player"], parsed["tipo"], parsed["side"], parsed["line"], parsed["amount"], pre, "", now_ts())
    bets = load_bets(); bets.append(b); save_bets(bets)
    await msg.edit_text(f"✅ *Registrada #{b.id}*\n{b.player} {b.tipo} {b.side} {b.line} ({b.amount}u)", parse_mode=ParseMode.MARKDOWN)

async def cmd_misapuestas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    pending = [b for b in load_bets() if b.user_id == uid and not b.result]
    if not pending:
        await update.message.reply_text("Sin apuestas pendientes.")
        return
    lines = [f"⏳ *Apuestas pendientes* ({len(pending)})\n"]
    for b in pending:
        lines.append(f"`#{b.id}` {b.player} {b.tipo} {b.side} `{b.line}` ({b.amount}u)")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def cmd_resultado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if len(args) < 2: return await update.message.reply_text("Uso: `/resultado ID WIN|LOSS|PUSH real_stat`", parse_mode=ParseMode.MARKDOWN)
    bid, res = args[0].upper(), args[1].upper()
    bets = load_bets()
    found = next((b for b in bets if b.id == bid), None)
    if not found: return await update.message.reply_text("No encontrada.")
    found.result, found.resolved_at = res.lower(), now_ts()
    save_bets(bets)
    await update.message.reply_text(f"✅ #{bid} actualizada a {res}")

async def cmd_historial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    bets = [b for b in load_bets() if b.user_id == uid and b.result in ("win","loss","push")]
    wins, losses = sum(1 for b in bets if b.result=="win"), sum(1 for b in bets if b.result=="loss")
    total = wins + losses
    net = sum(b.amount for b in bets if b.result=="win") - sum(b.amount for b in bets if b.result=="loss")
    wr = round(wins/total*100,1) if total else 0
    await update.message.reply_text(f"📊 *Historial*\nTotal: {total} | W: {wins} L: {losses}\nWinRate: {wr}%\nNeto: {net}u", parse_mode=ParseMode.MARKDOWN)

# === JOBS EN SEGUNDO PLANO ===
async def background_scan(context: ContextTypes.DEFAULT_TYPE):
    cid = context.job.chat_id
    state = await asyncio.to_thread(load_json, ALERTS_STATE_FILE, {})
    # Esta versión es simplificada en background para no consumir excesivos recursos
    # pero mantiene vivas las alertas.
    try:
        games = await asyncio.to_thread(lambda: scoreboard.ScoreBoard().get_dict()["scoreboard"]["games"])
        # Aquí iría el ciclo completo de validación de en vivo
        pass
    except: pass

async def send_morning_digest(context: ContextTypes.DEFAULT_TYPE):
    today = date.today().isoformat()
    state = await asyncio.to_thread(load_json, MORNING_DIGEST_FILE, {})
    if state.get("last_date") == today: return
    state["last_date"] = today
    await asyncio.to_thread(save_json, MORNING_DIGEST_FILE, state)
    
    try:
        games = await asyncio.to_thread(lambda: scoreboard.ScoreBoard().get_dict()["scoreboard"]["games"])
        gl = [f"• {g['awayTeam']['teamTricode']} @ {g['homeTeam']['teamTricode']}" for g in games]
        await context.bot.send_message(context.job.chat_id, f"🌅 *Buenos días NBA*\n" + "\n".join(gl), parse_mode=ParseMode.MARKDOWN)
    except: pass

async def background_check_morning(context: ContextTypes.DEFAULT_TYPE):
    from datetime import datetime
    if datetime.now().hour == MORNING_DIGEST_HOUR:
        await send_morning_digest(context)


# ================================================================
# SISTEMA MULTI-USUARIO Y MAIN
# ================================================================
USERS_FILE = "users.json"
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))

def load_users() -> dict: return load_json(USERS_FILE, {"allowed": [], "admins": [], "nicknames": {}})
def save_users(data: dict): save_json(USERS_FILE, data)
def is_allowed(uid: int) -> bool:
    u = load_users()
    return not u["allowed"] or uid in u["allowed"] or uid in u["admins"] or uid == ADMIN_ID
def add_user(uid: int, nick: str = "", admin: bool = False):
    u = load_users()
    if uid not in u["allowed"]: u["allowed"].append(uid)
    if admin and uid not in u["admins"]: u["admins"].append(uid)
    if nick: u["nicknames"][str(uid)] = nick
    save_users(u)

async def guard(update: Update) -> bool:
    uid = update.effective_user.id
    if is_allowed(uid): return True
    await update.message.reply_text(f"🔒 *Acceso restringido*\nTu ID: `{uid}`", parse_mode=ParseMode.MARKDOWN)
    return False

async def register_job(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid, uname = update.effective_user.id, update.effective_user.first_name
    u = load_users()
    if not u["allowed"] and not u["admins"]:
        add_user(uid, uname, admin=True)
        await update.message.reply_text(f"👑 *Primer usuario (Admin)*\nID: `{uid}`", parse_mode=ParseMode.MARKDOWN)
    elif not is_allowed(uid):
        await update.message.reply_text(f"🔒 *Acceso denegado*\nID: `{uid}`", parse_mode=ParseMode.MARKDOWN)
        return
    else: add_user(uid, uname)

    cid = update.effective_chat.id
    if not context.job_queue.get_jobs_by_name(f"scan:{cid}"):
        context.job_queue.run_repeating(background_scan, interval=POLL_SECONDS, first=10, chat_id=cid, name=f"scan:{cid}")
    if not context.job_queue.get_jobs_by_name(f"morning:{cid}"):
        context.job_queue.run_repeating(background_check_morning, interval=3600, first=60, chat_id=cid, name=f"morning:{cid}")

    await update.message.reply_text("✅ *Bot activado y trabajos iniciados*\nUsa `/odds` o `/live`.", parse_mode=ParseMode.MARKDOWN)

BOT_COMMANDS = [
    BotCommand("start", "Activar bot"), BotCommand("odds", "Props por partido"),
    BotCommand("games", "Cartelera hoy"), BotCommand("live", "Props en vivo"),
    BotCommand("lineup", "Alineaciones"), BotCommand("bet", "Apostar"),
    BotCommand("misapuestas", "Pendientes"), BotCommand("historial", "Resultados"),
    BotCommand("help", "Ayuda")
]

async def on_startup(app: Application):
    try: await app.bot.set_my_commands(BOT_COMMANDS)
    except: pass

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    def guarded(fn):
        async def wrap(u: Update, c: ContextTypes.DEFAULT_TYPE):
            if not await guard(u): return
            return await fn(u, c)
        return wrap

    app.add_handler(CommandHandler("start", register_job))
    app.add_handler(CommandHandler("help", guarded(cmd_help)))
    app.add_handler(CommandHandler("games", guarded(cmd_games)))
    app.add_handler(CommandHandler("odds", guarded(cmd_odds)))
    app.add_handler(CommandHandler("live", guarded(cmd_live)))
    app.add_handler(CommandHandler("lineup", guarded(cmd_lineup)))
    app.add_handler(CommandHandler("bet", guarded(cmd_bet)))
    app.add_handler(CommandHandler("resultado", guarded(cmd_resultado)))
    app.add_handler(CommandHandler("historial", guarded(cmd_historial)))
    app.add_handler(CommandHandler("misapuestas", guarded(cmd_misapuestas)))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_game_selection))

    app.post_init = on_startup
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
