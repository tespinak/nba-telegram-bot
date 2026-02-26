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
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
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
    "Origin": "https://polymarket.com",
    "Referer": "https://polymarket.com/",
}

def build_session(headers: dict) -> requests.Session:
    s = requests.Session()
    retry = Retry(total=5, connect=5, read=5, backoff_factor=1.5, status_forcelist=(403, 408, 429, 500, 502, 503, 504), allowed_methods=frozenset(["GET", "POST"]))
    s.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20))
    s.headers.update(headers)
    return s

SESSION_NBA = build_session(NBA_HEADERS)
SESSION_PM = build_session(PM_HEADERS)

# =========================
# Helpers
# =========================
def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f: return json.load(f)
    except: return default

def save_json(path: str, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def now_ts() -> int: return int(time.time())
def clamp(x, lo=0, hi=100): return max(lo, min(hi, x))

# =========================
# Data Models
# =========================
@dataclass
class Prop:
    player: str; tipo: str; line: float; side: str
    source: str = "manual"; game_slug: Optional[str] = None; market_id: Optional[str] = None
    added_by: Optional[int] = None; added_at: Optional[int] = None

@dataclass
class Bet:
    id: str; user_id: int; player: str; tipo: str; side: str; line: float; amount: float
    pre_score: int; game_slug: str; placed_at: int
    result: Optional[str] = None; actual_stat: Optional[float] = None; resolved_at: Optional[int] = None; notes: str = ""

def load_props() -> List[Prop]: return [Prop(**p) for p in load_json(PROPS_FILE, {"props": []}).get("props", [])]
def save_props(props: List[Prop]): save_json(PROPS_FILE, {"props": [asdict(p) for p in props]})
def load_bets() -> List[Bet]: return [Bet(**b) for b in load_json(BETS_FILE, {"bets": []}).get("bets", [])]
def save_bets(bets: List[Bet]): save_json(BETS_FILE, {"bets": [asdict(b) for b in bets]})
def _new_bet_id() -> str: import uuid; return str(uuid.uuid4())[:8].upper()

# =========================
# API Caches
# =========================
def obtener_id_jugador(nombre: str) -> Optional[int]:
    time.sleep(0.1 + random.random() * 0.1)
    res = players.find_players_by_full_name(nombre)
    if not res: return None
    exact = [p for p in res if (p.get("full_name") or "").lower() == nombre.lower()]
    return int((exact[0] if exact else res[0]).get("id"))

def get_pid_for_name(name: str) -> Optional[int]:
    c = load_json(IDS_CACHE_FILE, {})
    if name in c: return int(c[name])
    pid = obtener_id_jugador(name)
    if pid: c[name] = int(pid); save_json(IDS_CACHE_FILE, c)
    return pid

GLOG_TTL_SECONDS = 6 * 60 * 60
def get_gamelog_table(pid: int) -> Tuple[List[str], List[list]]:
    c = load_json(GLOG_CACHE_FILE, {})
    k = str(pid); now = now_ts()
    if k in c and (now - int(c[k].get("ts", 0))) < GLOG_TTL_SECONDS: return c[k].get("headers", []), c[k].get("rows", [])
    
    time.sleep(0.3 + random.random() * 0.2)
    url = "https://stats.nba.com/stats/playergamelog"
    params = {"DateFrom": "", "DateTo": "", "LeagueID": "00", "PlayerID": str(pid), "Season": SEASON, "SeasonType": "Regular Season"}
    try:
        resp = SESSION_NBA.get(url, params=params, timeout=(12, 30))
        if resp.status_code != 200: return c.get(k, {}).get("headers", []), c.get(k, {}).get("rows", [])
        rs = resp.json().get("resultSets", [])
        if not rs: rs = [resp.json().get("resultSet")] if resp.json().get("resultSet") else []
        hdrs, rows = rs[0].get("headers", []) if rs else [], rs[0].get("rowSet", []) if rs else []
        c[k] = {"ts": now, "headers": hdrs, "rows": rows}; save_json(GLOG_CACHE_FILE, c)
        return hdrs, rows
    except: return c.get(k, {}).get("headers", []), c.get(k, {}).get("rows", [])

# =========================
# Contexto y Stats
# =========================
CONTEXT_CACHE = {}; CONTEXT_TTL = 4 * 60 * 60
_TRICODE_TO_TEAM_ID_CACHE = {}

def get_team_id_cached(tricode: str) -> Optional[int]:
    if tricode in _TRICODE_TO_TEAM_ID_CACHE: return _TRICODE_TO_TEAM_ID_CACHE[tricode]
    for t in nba_teams_static.get_teams():
        if t.get("abbreviation", "").upper() == tricode.upper():
            _TRICODE_TO_TEAM_ID_CACHE[tricode] = int(t["id"]); return int(t["id"])
    return None

def fetch_league_team_stats(measure="Advanced") -> Dict[int, dict]:
    k = f"team_stats_{measure}"; now = now_ts()
    if k in CONTEXT_CACHE and (now - CONTEXT_CACHE[k].get("ts", 0)) < CONTEXT_TTL: return CONTEXT_CACHE[k]["data"]
    url = "https://stats.nba.com/stats/leaguedashteamstats"
    params = {"MeasureType": measure, "PerMode": "PerGame", "Season": SEASON, "SeasonType": "Regular Season", "LeagueID": "00"}
    try:
        resp = SESSION_NBA.get(url, params=params, timeout=30)
        if resp.status_code != 200: return {}
        rs = resp.json().get("resultSets", [{}])[0]
        hdrs, rows = rs.get("headers", []), rs.get("rowSet", [])
        res = {int(dict(zip(hdrs, r)).get("TEAM_ID", 0)): dict(zip(hdrs, r)) for r in rows}
        CONTEXT_CACHE[k] = {"ts": now, "data": res}; return res
    except: return {}

def get_defensive_context(opp_tricode: str, tipo: str) -> dict:
    res = {"def_rating": None, "pace": None, "opp_stat": None, "def_rank": None, "pace_rank": None, "opp_stat_rank": None, "verdict": ""}
    ts_adv = fetch_league_team_stats("Advanced")
    ts_opp = fetch_league_team_stats("Opponent")
    tid = get_team_id_cached(opp_tricode)
    if not tid or tid not in ts_adv: return res

    t_adv = ts_adv[tid]
    res["def_rating"], res["pace"] = t_adv.get("DEF_RATING"), t_adv.get("PACE")
    all_def = sorted(ts_adv.values(), key=lambda x: x.get("DEF_RATING", 999))
    res["def_rank"] = next((i+1 for i, t in enumerate(all_def) if t.get("TEAM_NAME") == t_adv.get("TEAM_NAME")), None)
    all_pace = sorted(ts_adv.values(), key=lambda x: x.get("PACE", 0), reverse=True)
    res["pace_rank"] = next((i+1 for i, t in enumerate(all_pace) if t.get("TEAM_NAME") == t_adv.get("TEAM_NAME")), None)

    if tid in ts_opp:
        sk = {"puntos": "OPP_PTS", "rebotes": "OPP_REB", "asistencias": "OPP_AST"}.get(tipo)
        if sk:
            res["opp_stat"] = ts_opp[tid].get(sk)
            all_opp = sorted(ts_opp.values(), key=lambda x: x.get(sk, 0), reverse=True)
            res["opp_stat_rank"] = next((i+1 for i, s in enumerate(all_opp) if s.get(sk) == ts_opp[tid].get(sk)), None)

    vd = []
    if res["def_rank"]: vd.append("defensa débil" if res["def_rank"] >= 25 else ("defensa élite" if res["def_rank"] <= 5 else ""))
    if res["pace_rank"]: vd.append("ritmo alto" if res["pace_rank"] <= 5 else ("ritmo lento" if res["pace_rank"] >= 25 else ""))
    res["verdict"] = " · ".join(filter(bool, vd)) or "contexto neutro"
    return res

# =========================
# PRE SCORE
# =========================
def hit_counts(values: List[float], line: float, side: str) -> Tuple[int, int]:
    if not values: return 0, 0
    return sum(1 for v in values if (v > line if side == "over" else v < line)), len(values)

def pre_score(pid: int, tipo: str, line: float, side: str) -> Tuple[int, dict]:
    hdrs, rows = get_gamelog_table(pid)
    col = STAT_COL.get(tipo)
    idx = hdrs.index(col) if col in hdrs else -1
    if idx == -1 or not rows: return 0, {}
    
    vals = []
    for r in rows[:10]:
        try: vals.append(float(r[idx]))
        except: pass

    v5, v10 = vals[:5], vals[:10]
    h5, n5 = hit_counts(v5, line, side)
    h10, n10 = hit_counts(v10, line, side)
    hit5, hit10 = (h5/n5) if n5 else 0.0, (h10/n10) if n10 else 0.0

    m5 = [(v - line if side == "over" else line - v) for v in v5]
    m10 = [(v - line if side == "over" else line - v) for v in v10]
    w_margin = max(0.0, (0.65 * (sum(m10)/len(m10) if m10 else 0.0)) + (0.35 * (sum(m5)/len(m5) if m5 else 0.0)))

    HitScore = 100.0 * (0.65 * hit10 + 0.35 * hit5)
    MarginScore = clamp((w_margin / MARGIN_CAP.get(tipo, 3.0)) * 100.0, 0, 100)
    std10 = math.sqrt(sum((x - sum(v10)/len(v10))**2 for x in v10)/(len(v10)-1)) if len(v10)>1 else 0
    ConsistencyScore = 100.0 - clamp((std10 / STD_CAP.get(tipo, 4.0)) * 60.0, 0, 60)

    PRE = int(clamp(0.55 * HitScore + 0.25 * MarginScore + 0.20 * ConsistencyScore, 0, 100))
    return PRE, {
        "hit5": round(hit5, 2), "hit10": round(hit10, 2), "hits5": h5, "n5": n5, "hits10": h10, "n10": n10,
        "avg5": round(sum(v5)/len(v5), 2) if v5 else 0.0, "avg10": round(sum(v10)/len(v10), 2) if v10 else 0.0,
        "std10": round(std10, 2), "w_margin": round(w_margin, 2), "vals": v10
    }

def home_away_splits(pid: int, tipo: str) -> dict:
    hdrs, rows = get_gamelog_table(pid)
    try: idx = hdrs.index(STAT_COL.get(tipo)); m_idx = hdrs.index("MATCHUP")
    except: return {}
    hv, av = [], []
    for r in rows:
        ms = str(r[m_idx])
        try:
            v = float(r[idx])
            if " vs. " in ms: hv.append(v)
            elif " @ " in ms: av.append(v)
        except: pass
    res = {}
    if hv: res["home_avg"], res["home_n"] = round(sum(hv)/len(hv),1), len(hv)
    if av: res["away_avg"], res["away_n"] = round(sum(av)/len(av),1), len(av)
    return res

def pre_score_v2(pid: int, tipo: str, line: float, side: str, opp_tricode: str = "", is_home: bool = True, rest_days: int = 1) -> Tuple[int, dict]:
    base, meta = pre_score(pid, tipo, line, side)
    adjs, adj_tot = [], 0.0

    if opp_tricode:
        ctx = get_defensive_context(opp_tricode, tipo)
        dr, pr, osr = ctx.get("def_rank"), ctx.get("pace_rank"), ctx.get("opp_stat_rank")
        if dr:
            a = (+8 if dr>=25 else +4 if dr>=20 else -8 if dr<=5 else -4 if dr<=10 else 0) if side=="over" else (+8 if dr<=5 else +4 if dr<=10 else -8 if dr>=25 else -4 if dr>=20 else 0)
            if a: adj_tot += a; adjs.append(f"defensa {a:+d}")
        if pr:
            a = (+5 if pr<=5 else -5 if pr>=25 else 0) if side=="over" else (+5 if pr>=25 else -5 if pr<=5 else 0)
            if a: adj_tot += a; adjs.append(f"ritmo {a:+d}")
        if osr:
            a = (+6 if osr<=8 else -6 if osr>=23 else 0) if side=="over" else (+6 if osr>=23 else -6 if osr<=8 else 0)
            if a: adj_tot += a; adjs.append(f"permite_{tipo[:3]} {a:+d}")
        meta.update({"ctx_def_rank": dr, "ctx_pace_rank": pr, "ctx_osr": osr, "ctx_opp_tri": opp_tricode})

    loc = "home" if is_home else "away"
    loc_avg = home_away_splits(pid, tipo).get(f"{loc}_avg")
    if loc_avg is not None:
        gap = loc_avg - line if side == "over" else line - loc_avg
        a = +5 if gap > 2.0 else -5 if gap < -2.0 else 0
        if a: adj_tot += a; adjs.append(f"split_{loc} {a:+d}")

    if rest_days == 0: a = -6 if side=="over" else +6; adj_tot += a; adjs.append(f"b2b {a:+d}")
    elif rest_days >= 3: a = +4 if side=="over" else -4; adj_tot += a; adjs.append(f"descansado {a:+d}")

    meta.update({"v2_base": base, "v2_adj": round(adj_tot, 1), "v2_adjustments": adjs})
    return int(clamp(base + adj_tot, 0, 100)), meta

# =========================
# Polymarket Fetcher (Robusto)
# =========================
PM_CACHE = {"ts": 0, "date": None, "props": []}
PM_TTL_SECONDS = 8 * 60

_TRICODE_TO_SLUG_NAMES = {
    "ATL": ["atlanta", "hawks", "atl"], "BOS": ["boston", "celtics", "bos"],
    "BKN": ["brooklyn", "nets", "bkn", "bk"], "CHA": ["charlotte", "hornets", "cha"],
    "CHI": ["chicago", "bulls", "chi"], "CLE": ["cleveland", "cavaliers", "cavs", "cle"],
    "DAL": ["dallas", "mavericks", "mavs", "dal"], "DEN": ["denver", "nuggets", "den"],
    "DET": ["detroit", "pistons", "det"], "GSW": ["golden-state", "warriors", "gsw", "gs"],
    "HOU": ["houston", "rockets", "hou"], "IND": ["indiana", "pacers", "ind"],
    "LAC": ["la-clippers", "clippers", "lac"], "LAL": ["la-lakers", "lakers", "lal", "la"],
    "MEM": ["memphis", "grizzlies", "mem"], "MIA": ["miami", "heat", "mia"],
    "MIL": ["milwaukee", "bucks", "mil"], "MIN": ["minnesota", "timberwolves", "wolves", "min"],
    "NOP": ["new-orleans", "pelicans", "nop", "no"], "NYK": ["new-york", "knicks", "nyk", "ny"],
    "OKC": ["oklahoma", "thunder", "okc"], "ORL": ["orlando", "magic", "orl"],
    "PHI": ["philadelphia", "76ers", "sixers", "phi"], "PHX": ["phoenix", "suns", "phx"],
    "POR": ["portland", "trail-blazers", "blazers", "por"], "SAC": ["sacramento", "kings", "sac"],
    "SAS": ["san-antonio", "spurs", "sas", "sa"], "TOR": ["toronto", "raptors", "tor"],
    "UTA": ["utah", "jazz", "uta"], "WAS": ["washington", "wizards", "was"],
}

def polymarket_props_today_from_scoreboard() -> List[Prop]:
    today = date.today().isoformat()
    now = now_ts()
    if PM_CACHE["date"] == today and (now - PM_CACHE["ts"]) < PM_TTL_SECONDS:
        return PM_CACHE["props"]

    try: games = scoreboard.ScoreBoard().get_dict()["scoreboard"]["games"]
    except Exception: games = []

    props_all: List[Prop] = []
    
    # 1. Obtener TODOS los eventos NBA activos de una vez
    all_events = []
    try:
        r = SESSION_PM.get(f"{GAMMA}/events", params={"tag_slug": "nba", "closed": "false", "limit": 100}, timeout=15)
        if r.status_code == 200:
            data = r.json()
            all_events = data if isinstance(data, list) else data.get("events", [])
    except: pass

    # 2. Emparejar cada juego con los eventos
    for g in games:
        away_tri = (g.get("awayTeam", {}) or {}).get("teamTricode", "").upper()
        home_tri = (g.get("homeTeam", {}) or {}).get("teamTricode", "").upper()
        if not away_tri or not home_tri: continue
        
        local_slug = f"nba-{away_tri.lower()}-{home_tri.lower()}-{today}"
        
        ev_match = None
        for ev in all_events:
            ev_slug = (ev.get("slug") or "").lower()
            ev_title = (ev.get("title") or "").lower()
            combined = ev_slug + " " + ev_title
            
            away_names = _TRICODE_TO_SLUG_NAMES.get(away_tri, [away_tri.lower()])
            home_names = _TRICODE_TO_SLUG_NAMES.get(home_tri, [home_tri.lower()])
            
            if any(n in combined for n in away_names) and any(n in combined for n in home_names):
                ev_match = ev
                break
        
        if not ev_match:
            try:
                r = SESSION_PM.get(f"{GAMMA}/events/slug/{local_slug}", timeout=10)
                if r.status_code == 200: ev_match = r.json()
            except: pass

        if ev_match:
            try:
                markets = ev_match.get("markets", [])
                if not markets and ev_match.get("id"):
                    mr = SESSION_PM.get(f"{GAMMA}/markets", params={"event_id": ev_match["id"], "limit": 200}, timeout=15)
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
                        mx = re.search(r"^(.*?)(?::\s*|\s+)(?:Points|Rebounds|Assists)", q, re.IGNORECASE)
                        if mx: player = mx.group(1).strip()
                    
                    try: line_val = float(m.get("line", 0))
                    except:
                        mx = re.search(r"O\/U\s*(\d+(?:\.\d+)?)", q, re.IGNORECASE)
                        line_val = float(mx.group(1)) if mx else None

                    if player and line_val:
                        tipo = {"points":"puntos","rebounds":"rebotes","assists":"asistencias"}[smt]
                        props_all.extend([Prop(player, tipo, line_val, "over", "polymarket", local_slug, str(m.get("id"))),
                                          Prop(player, tipo, line_val, "under", "polymarket", local_slug, str(m.get("id")))])
            except: pass

    # 3. Deduplicar
    seen = set()
    uniq = []
    for p in props_all:
        k = (p.game_slug, p.player.lower(), p.tipo, p.line, p.side)
        if k not in seen:
            seen.add(k)
            uniq.append(p)

    if not uniq:
        log.warning("Polymarket falló o no hay líneas. Usando Fallback.")
        fs = "nba-okc-det-" + today
        uniq = [Prop("Shai Gilgeous-Alexander", "puntos", 32.5, s, "fallback", fs) for s in ("over","under")] + \
               [Prop("Cade Cunningham", "puntos", 28.5, s, "fallback", fs) for s in ("over","under")]

    PM_CACHE["date"] = today; PM_CACHE["ts"] = now; PM_CACHE["props"] = uniq
    return uniq

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
# Formato & UI
# =========================
def _e(s: int) -> str: return "🔥" if s>=75 else "✅" if s>=60 else "🟡" if s>=45 else "🟠" if s>=30 else "❄️"
def _bar(s: int) -> str: f = round(s/100*8); return "█"*f + "░"*(8-f)
def _mt(s: str) -> str: p = s.replace("nba-","").split("-"); return f"{p[0].upper()} @ {p[1].upper()}" if len(p)>=2 else s

async def _send_long(u: Update, t: str):
    if len(t) <= 3800:
        try: await u.message.reply_text(t, parse_mode=ParseMode.MARKDOWN)
        except: await u.message.reply_text(t.replace("*","").replace("_","").replace("`",""))
        return
    rem = t
    while len(rem) > 3800:
        c = rem[:3800].rfind("\n👤")
        if c < 0: c = 3800
        try: await u.message.reply_text(rem[:c], parse_mode=ParseMode.MARKDOWN)
        except: await u.message.reply_text(rem[:c].replace("*","").replace("_",""))
        rem = rem[c:]
        await asyncio.sleep(0.3)
    if rem:
        try: await u.message.reply_text(rem, parse_mode=ParseMode.MARKDOWN)
        except: await u.message.reply_text(rem.replace("*","").replace("_",""))

# =========================
# COMANDOS PRINCIPALES
# =========================
async def cmd_help(u: Update, c: ContextTypes.DEFAULT_TYPE):
    t = ("🧠 *NBA Props Bot v3*\n\n"
         "*📋 Programación*\n• `/odds` → menú props y score v2\n• `/games` → cartelera\n• `/live` → en vivo\n• `/lineup` → alineaciones y bajas\n\n"
         "*📊 Análisis*\n• `/analisis Jugador | tipo | side | linea` → deep dive\n• `/alertas` → top props del día\n• `/contexto AWAY HOME` → defensas\n\n"
         "*💰 Apuestas*\n• `/bet Jugador | tipo | side | linea | monto`\n• `/misapuestas`, `/resultado ID WIN`, `/historial`")
    await u.message.reply_text(t, parse_mode=ParseMode.MARKDOWN)

async def cmd_games(u: Update, c: ContextTypes.DEFAULT_TYPE):
    try: games = await asyncio.wait_for(asyncio.to_thread(lambda: scoreboard.ScoreBoard().get_dict()["scoreboard"]["games"]), timeout=20.0)
    except: return await u.message.reply_text("⚠️ Error de API NBA.")
    if not games: return await u.message.reply_text("No hay juegos hoy.")
    ls = ["📅 *NBA hoy*"] + [f"• {g.get('awayTeam',{}).get('teamTricode','?')} @ {g.get('homeTeam',{}).get('teamTricode','?')} — {g.get('gameStatusText','')}" for g in games]
    await u.message.reply_text("\n".join(ls), parse_mode=ParseMode.MARKDOWN)

# --- ODDS & MENU ---
async def cmd_odds(u: Update, c: ContextTypes.DEFAULT_TYPE):
    msg = await u.message.reply_text("⏳ *Buscando partidos en Polymarket...*", parse_mode=ParseMode.MARKDOWN)
    props = await asyncio.to_thread(polymarket_props_today_from_scoreboard)
    if not props: return await msg.edit_text("❌ No hay props disponibles ahora mismo.")
    
    gd = {}
    for p in props: gd.setdefault(p.game_slug or "unknown", []).append(p)
    
    ls = [f"📋 *NBA Props — {date.today().strftime('%d/%m/%Y')}*\n🎮 *Elige un partido:*\n{'─'*30}"]
    for i, (sl, prs) in enumerate(gd.items(), 1):
        ls.append(f"{i}. *{_mt(sl)}* (👤 {len(set(p.player for p in prs))} jug | 📊 {len(prs)//2} líneas)")
    
    c.user_data['games_menu'] = list(gd.keys())
    await msg.edit_text("\n".join(ls) + f"\n{'─'*30}\n_Responde con el número (1,2,3...)_", parse_mode=ParseMode.MARKDOWN)

async def handle_game_selection(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if 'games_menu' not in c.user_data: return
    txt = u.message.text.strip()
    if not txt.isdigit(): return
    idx = int(txt) - 1
    gm = c.user_data['games_menu']
    if 0 <= idx < len(gm):
        slug = gm[idx]
        props = await asyncio.to_thread(polymarket_props_today_from_scoreboard)
        gp = [p for p in props if (p.game_slug or "") == slug]
        del c.user_data['games_menu']
        if gp: await show_game_props_advanced(u, c, slug, gp)
        else: await u.message.reply_text("❌ Error cargando partido.")

async def show_game_props_advanced(u: Update, c: ContextTypes.DEFAULT_TYPE, slug: str, props: List[Prop]):
    mt = _slug_to_matchup(slug)
    msg = await u.message.reply_text(f"⚡ *Calculando scores avanzados para {mt}...*", parse_mode=ParseMode.MARKDOWN)
    p_slug = slug.replace("nba-","").split("-")
    aw, hm = (p_slug[0].upper() if len(p_slug)>=2 else "???"), (p_slug[1].upper() if len(p_slug)>=2 else "???")

    ul = {}
    for p in props:
        if p.side == "over":
            if (p.tipo, p.line) not in ul.setdefault(p.player, []): ul[p.player].append((p.tipo, p.line))

    def _calc(pl: str, lns: List[Tuple[str, float]]):
        pid = get_pid_for_name(pl)
        if not pid: return pl, []
        op, is_h = hm, False
        try:
            r = commonteamroster.CommonTeamRoster(team_id=get_team_id_cached(hm), season=SEASON).get_data_frames()[0]
            if pid in r['PLAYER_ID'].values: op, is_h = aw, True
        except: pass
        res = []
        for (tp, ln) in lns:
            po, mo = pre_score_v2(pid, tp, ln, "over", op, is_h, 1)
            pu, _ = pre_score_v2(pid, tp, ln, "under", op, is_h, 1)
            res.append({"t": tp, "l": ln, "po": po, "pu": pu, "m": mo})
        return pl, res

    sem = asyncio.Semaphore(3)
    async def _sf(pl, lns):
        async with sem: return await asyncio.wait_for(asyncio.to_thread(_calc, pl, lns), timeout=25.0)

    try: 
        results = await asyncio.gather(*[_sf(pl, lns) for pl, lns in ul.items()], return_exceptions=True)
    except Exception as e: 
        return await msg.edit_text(f"❌ Error de servidor: {e}")

    pd = {}
    for item in results:
        if isinstance(item, Exception): continue
        pl, res = item
        if res: pd[pl] = res

    if not pd: 
        return await msg.edit_text("❌ La API de la NBA está tardando demasiado. Intenta de nuevo en unos minutos.")

    ps = sorted(pd.keys(), key=lambda pl: max((max(e["po"], e["pu"]) for e in pd[pl]), default=0), reverse=True)
    ti = {"puntos": "🏀", "rebotes": "💪", "asistencias": "🎯"}
    
    ls = [f"🟣 *{mt}*\n`{slug}`\n{'─'*28}"]
    for pl in ps:
        ls.append(f"\n👤 *{pl}*")
        for e in sorted(pd[pl], key=lambda x: {"puntos":0,"rebotes":1,"asistencias":2}.get(x["t"],9)):
            po, pu, m = e["po"], e["pu"], e["m"]
            adjs = f"  _(adj: {', '.join(m.get('v2_adjustments', []))[:30]})_\n" if m.get("v2_adjustments") else ""
            ctx = f"  🛡️ Def#{m.get('ctx_def_rank','?')} · Pace#{m.get('ctx_pace_rank','?')}\n" if m.get("ctx_def_rank") else ""
            ls.append(f"{ti.get(e['t'],'•')} *{e['t'].upper()}* — `{e['l']}`\n  OVER  {_e(po)} `{po:>3}/100` {_bar(po)}\n  UNDER {_e(pu)} `{pu:>3}/100` {_bar(pu)}\n{adjs}{ctx}  📊 `{m.get('hits5','?')}/{m.get('n5','?')}` últ5 | `{m.get('hits10','?')}/{m.get('n10','?')}` últ10 | prom `{m.get('avg10',0):.1f}`")

    await msg.delete()
    await _send_long(u, "\n".join(ls))

# --- LIVE ---
async def cmd_live(u: Update, c: ContextTypes.DEFAULT_TYPE):
    msg = await u.message.reply_text("⏳ *Cargando datos en vivo...*", parse_mode=ParseMode.MARKDOWN)
    
    try:
        try:
            games = await asyncio.wait_for(asyncio.to_thread(lambda: scoreboard.ScoreBoard().get_dict()["scoreboard"]["games"]), timeout=20.0)
        except Exception: 
            return await msg.edit_text("⚠️ Error leyendo scoreboard de la NBA.")
        
        live_games = [g for g in games if g.get("gameStatus") == 2]
        if not live_games: 
            return await msg.edit_text("⏸️ No hay partidos en vivo ahora mismo.")
        
        await msg.edit_text(f"🔄 *{len(live_games)} partido(s) en vivo* — calculando probabilidades...", parse_mode=ParseMode.MARKDOWN)
        
        props_pm = PM_CACHE.get("props", [])
        if not props_pm: 
            props_pm = await asyncio.to_thread(polymarket_props_today_from_scoreboard)
        
        props_manual = await asyncio.to_thread(load_props)
        all_props = (props_manual or []) + (props_pm or [])

        pbn = {}
        for p in all_props: 
            pbn.setdefault(p.player.lower(), []).append(p)

        async def _f(gid):
            try: return gid, await asyncio.wait_for(asyncio.to_thread(lambda: boxscore.BoxScore(gid).get_dict()["game"]), timeout=15.0)
            except: return gid, None

        bxr = await asyncio.gather(*[_f(g["gameId"]) for g in live_games])
        sr = []
        
        for g, (gid, box) in zip(live_games, bxr):
            if not box: continue
            
            try:
                sc_home = g.get("homeTeam", {}).get("score")
                sc_away = g.get("awayTeam", {}).get("score")
                diff = abs(int(sc_home if sc_home else 0) - int(sc_away if sc_away else 0))
            except Exception:
                diff = 0
                
            q = int(g.get("period", 0))
            clk = g.get("gameClock", "")
            clock_sec = clock_to_seconds(clk)
            elapsed_min = game_elapsed_minutes(q, clock_sec)

            for tk in ["homeTeam", "awayTeam"]:
                for pl in box.get(tk, {}).get("players", []):
                    fn = f"{pl.get('firstName', '')} {pl.get('familyName', '')}".strip().lower()
                    m = pbn.get(fn, [])
                    if not m and pl.get("familyName"):
                        for k, lst in pbn.items():
                            if pl.get("familyName").lower() in k: 
                                m = lst
                                break
                    if not m: continue

                    s = pl.get("statistics", {})
                    pts = float(s.get("points") or 0)
                    reb = float(s.get("reboundsTotal") or 0)
                    ast = float(s.get("assists") or 0)
                    pf = float(s.get("foulsPersonal") or 0)
                    mins = parse_minutes(s.get("minutes", ""))
                    pid = pl.get("personId")

                    for pr in m:
                        try:
                            act = pts if pr.tipo == "puntos" else (reb if pr.tipo == "rebotes" else ast)
                            pv, meta = await asyncio.to_thread(pre_score, pid, pr.tipo, pr.line, pr.side)
                            
                            if pr.side == "over":
                                delta = float(pr.line) - act
                                if not should_gate_by_minutes("over", pr.tipo, delta, mins, elapsed_min, diff>=20) and 0.5 <= delta <= 4.0:
                                    live_sc = compute_over_score(pr.tipo, delta, mins, pf, q, clock_sec, diff, diff<=8, diff>=20)
                                    f = int(clamp(0.55 * live_sc + 0.45 * pv, 0, 100))
                                    sr.append((f, pr, act, delta, q, clk, diff, meta, mins))
                            else:
                                delta = float(pr.line) - act
                                if not should_gate_by_minutes("under", pr.tipo, delta, mins, elapsed_min, diff>=20) and delta >= 2.0:
                                    live_sc = compute_under_score(pr.tipo, delta, mins, pf, q, clock_sec, diff, diff<=8, diff>=20)
                                    f = int(clamp(0.65 * live_sc + 0.35 * pv, 0, 100))
                                    sr.append((f, pr, act, delta, q, clk, diff, meta, mins))
                        except Exception as sub_e:
                            log.warning(f"Error procesando prop de {pr.player}: {sub_e}")

        await msg.delete()
        if not sr: 
            return await u.message.reply_text("📭 *Sin señal en vivo ahora*\nNinguna prop está lo suficientemente cerca de su línea.", parse_mode=ParseMode.MARKDOWN)
        
        sr.sort(key=lambda x: x[0], reverse=True)
        ls = [f"🔥 *LIVE — {len(live_games)} partido(s)*\n{'─'*28}"]
        
        for (f, pr, act, d, q, clk, df, m, mns) in sr[:15]:
            ic = {"puntos":"🏀","rebotes":"💪","asistencias":"🎯"}.get(pr.tipo,"•")
            lbl = "faltan" if pr.side == "over" else "colchón"
            ls.append(f"\n{_e(f)} `{f}/100` — *{pr.player}*\n"
                      f"{ic} {pr.tipo.upper()} {pr.side.upper()} `{pr.line}` | actual `{act:.0f}` ({lbl} `{d:.1f}`)\n"
                      f"⏱️ Q{q} {clk} | MIN `{mns:.0f}` Dif `{df}` | H5: `{m.get('hits5','?')}`")
                      
        await _send_long(u, "\n".join(ls))

    except Exception as e:
        log.error(f"Error fatal en cmd_live: {e}")
        await msg.edit_text(f"❌ Ocurrió un error inesperado al calcular los puntajes en vivo.\n`{e}`", parse_mode=ParseMode.MARKDOWN)

# --- ANALISIS PROFUNDO ---
async def cmd_analisis(u: Update, c: ContextTypes.DEFAULT_TYPE):
    b = re.sub(r"^/analisis(@\w+)?\s*", "", (u.message.text or "")).strip()
    if "|" not in b: return await u.message.reply_text("Uso: `/analisis Nombre | tipo | side | linea`", parse_mode=ParseMode.MARKDOWN)
    p = [x.strip() for x in b.split("|")]
    if len(p) != 4: return await u.message.reply_text("Deben ser 4 campos.")
    pl, tp, sd, ln = p[0], p[1].lower(), p[2].lower(), float(p[3])
    
    msg = await u.message.reply_text(f"🔍 Analizando *{pl}*...", parse_mode=ParseMode.MARKDOWN)
    
    def _run():
        pid = get_pid_for_name(pl)
        if not pid: return None, 0, {}
        pv, m = pre_score_v2(pid, tp, ln, sd, "", True, 1)
        return pid, pv, m

    pid, pre, meta = await asyncio.to_thread(_run)
    if not pid: return await msg.edit_text("⚠️ Jugador no encontrado.")
    
    v10 = meta.get("vals", [])
    racha = "✅" * sum(1 for v in v10[:3] if (v>ln if sd=="over" else v<ln))
    
    t = (f"🔬 *ANÁLISIS AVANZADO*\n👤 *{pl}*\n📌 {tp.upper()} {sd.upper()} `{ln}`\n"
         f"{_e(pre)} PRE Score: `{pre}/100` {_bar(pre)}\n{'─'*30}\n"
         f"📊 Promedios: Últ5 `{meta.get('avg5')}` | Últ10 `{meta.get('avg10')}`\n"
         f"🔁 Racha últ 3: {racha or '❌'}\n"
         f"🕒 Valores últ 10: {', '.join(str(v) for v in v10)}")
    await msg.edit_text(t, parse_mode=ParseMode.MARKDOWN)

# --- ALERTAS TOP ---
async def cmd_alertas(u: Update, c: ContextTypes.DEFAULT_TYPE):
    msg = await u.message.reply_text("🔍 Buscando top props pre-partido...", parse_mode=ParseMode.MARKDOWN)
    props = await asyncio.to_thread(polymarket_props_today_from_scoreboard)
    if not props: return await msg.edit_text("❌ Sin props disponibles.")
    
    sem = asyncio.Semaphore(4)
    async def _co(p: Prop):
        async with sem:
            def _i():
                pid = get_pid_for_name(p.player)
                if not pid: return p, 0
                po, _ = pre_score_v2(pid, p.tipo, p.line, "over")
                return p, po
            return await asyncio.to_thread(_i)
            
    res = await asyncio.gather(*[_co(p) for p in props if p.side == "over"])
    sc = sorted([(p, po) for p, po in res if po >= 60], key=lambda x: x[1], reverse=True)[:15]
    
    if not sc: return await msg.edit_text("😔 No hay props con PRE ≥ 60 hoy.")
    ls = [f"🏆 *MEJORES PROPS HOY*\n"]
    for i, (p, po) in enumerate(sc, 1):
        ls.append(f"*{i}.* {_e(po)} `{po}/100` — *{p.player}*\n    {p.tipo.upper()} OVER `{p.line}` | {_mt(p.game_slug or '')}")
    await msg.edit_text("\n".join(ls), parse_mode=ParseMode.MARKDOWN)

# --- CONTEXTO DEFENSIVO ---
async def cmd_contexto(u: Update, c: ContextTypes.DEFAULT_TYPE):
    a = c.args or []
    if len(a) < 2: return await u.message.reply_text("Uso: `/contexto AWAY HOME`", parse_mode=ParseMode.MARKDOWN)
    aw, hm = a[0].upper(), a[1].upper()
    msg = await u.message.reply_text(f"⏳ Cargando contexto {aw} @ {hm}...", parse_mode=ParseMode.MARKDOWN)

    def _f():
        return get_defensive_context(hm, "puntos"), get_defensive_context(aw, "puntos")
    aw_ctx, hm_ctx = await asyncio.to_thread(_f)

    def _fmt(tri, cx, lbl):
        return f"*{lbl} — {tri}*\n🛡️ Def Rating: `{cx.get('def_rating')} #{cx.get('def_rank')}`\n🏃 Pace: `{cx.get('pace')} #{cx.get('pace_rank')}`\n💬 _{cx.get('verdict')}_"

    t = f"🛡️ *CONTEXTO: {aw} @ {hm}*\n{'─'*30}\n" + _fmt(hm, aw_ctx, "Defensa vs Visitante") + f"\n\n{'─'*30}\n" + _fmt(aw, hm_ctx, "Defensa vs Local")
    await msg.edit_text(t, parse_mode=ParseMode.MARKDOWN)

# --- LINEUP ---
async def cmd_lineup(u: Update, c: ContextTypes.DEFAULT_TYPE):
    msg = await u.message.reply_text("⏳ Obteniendo alineaciones...", parse_mode=ParseMode.MARKDOWN)
    try: games = await asyncio.wait_for(asyncio.to_thread(lambda: scoreboard.ScoreBoard().get_dict()["scoreboard"]["games"]), timeout=15.0)
    except: return await msg.edit_text("⚠️ Error leyendo scoreboard")

    ft = " ".join(c.args or []).strip().upper() if c.args else None
    await msg.edit_text(f"🔄 Cargando datos...", parse_mode=ParseMode.MARKDOWN)

    def _get_bx(gid):
        try:
            r = {}
            bx = boxscore.BoxScore(gid).get_dict().get("game", {})
            for t_k in ["homeTeam", "awayTeam"]:
                tri = bx.get(t_k, {}).get("teamTricode", "")
                if tri:
                    r[tri] = [{"name": f"{p.get('firstName','')} {p.get('familyName','')}".strip(), "status": p.get("status","Active"), "starter": p.get("starter","0")=="1", "not_playing_reason": p.get("notPlayingReason","") or p.get("inactiveReason","")} for p in bx.get(t_k, {}).get("players", [])]
            return r
        except: return {}

    snt = False
    for g in games:
        aw, hm = g.get("awayTeam", {}).get("teamTricode", ""), g.get("homeTeam", {}).get("teamTricode", "")
        if ft and ft not in (aw, hm): continue
        bd = await asyncio.to_thread(_get_bx, g.get("gameId", "")) if g.get("gameId") else {}
        
        ap, hp = bd.get(aw, []), bd.get(hm, [])
        hdr = f"{'─'*32}\n✈️ *{aw}* @ 🏠 *{hm}*\n{'─'*32}"
        
        def _f(tr, pl):
            st = [p for p in pl if p.get("starter") and p.get("status","").lower() not in ("inactive","out")]
            out = [p for p in pl if p.get("status","").lower() in ("inactive","out")]
            r = [f"*{tr}*"]
            if st: r.append("  5️⃣ Titulares: " + ", ".join(p["name"] for p in st[:5]))
            if out: r.append("  🔴 Bajas: " + ", ".join(f"{p['name']} ({p['not_playing_reason'][:15]})" for p in out))
            return "\n".join(r) if pl else f"*{tr}*\n  _(sin datos)_"

        await u.message.reply_text(f"{hdr}\n\n{_f(aw, ap)}\n\n{_f(hm, hp)}", parse_mode=ParseMode.MARKDOWN)
        snt = True
        await asyncio.sleep(0.5)

    await msg.delete()
    if not snt: await u.message.reply_text("No encontré datos para ese equipo.")

# --- APUESTAS ---
def _parse_bet_command(text: str) -> Optional[dict]:
    body = re.sub(r"^/bet(@\w+)?\s*", "", text).strip()
    parts = [x.strip() for x in body.split("|")]
    if len(parts) < 4: return None
    player, tipo, side, line_s = parts[0], parts[1].lower(), parts[2].lower(), parts[3]
    amount_s = parts[4] if len(parts) >= 5 else "1"
    if tipo not in ("puntos","rebotes","asistencias") or side not in ("over","under"): return None
    try: return {"player": player, "tipo": tipo, "side": side, "line": float(line_s), "amount": float(amount_s)}
    except: return None

async def cmd_bet(u: Update, c: ContextTypes.DEFAULT_TYPE):
    parsed = _parse_bet_command(u.message.text or "")
    if not parsed:
        await u.message.reply_text("Uso: `/bet Jugador | tipo | side | linea | monto`", parse_mode=ParseMode.MARKDOWN)
        return
    msg = await u.message.reply_text("⏳ Registrando...", parse_mode=ParseMode.MARKDOWN)
    
    def _c():
        pid = get_pid_for_name(parsed["player"])
        if not pid: return None, 0
        po, pu, _ = pre_score(pid, parsed["tipo"], parsed["line"], parsed["side"])
        return pid, po if parsed["side"]=="over" else pu
    
    pid, pre = await asyncio.to_thread(_c)
    if not pid:
        await msg.edit_text("⚠️ Jugador no encontrado.")
        return

    b = Bet(_new_bet_id(), u.effective_user.id, parsed["player"], parsed["tipo"], parsed["side"], parsed["line"], parsed["amount"], pre, "", now_ts())
    bets = load_bets(); bets.append(b); save_bets(bets)
    await msg.edit_text(f"✅ *Apuesta Registrada #{b.id}*\n👤 {parsed['player']} | {parsed['tipo'].upper()} {parsed['side'].upper()} `{parsed['line']}` | 💰 `{parsed['amount']}u`\n{_e(pre)} PRE: `{pre}/100`", parse_mode=ParseMode.MARKDOWN)

async def cmd_misapuestas(u: Update, c: ContextTypes.DEFAULT_TYPE):
    pnd = [b for b in load_bets() if b.user_id == u.effective_user.id and not b.result]
    if not pnd: return await u.message.reply_text("Sin apuestas pendientes.")
    ls = [f"⏳ *Pendientes* ({len(pnd)})\n"] + [f"`#{b.id}` {b.player} {b.tipo} {b.side} `{b.line}` ({b.amount}u)" for b in pnd]
    await u.message.reply_text("\n".join(ls), parse_mode=ParseMode.MARKDOWN)

async def cmd_resultado(u: Update, c: ContextTypes.DEFAULT_TYPE):
    a = c.args or []
    if len(a) < 2: return await u.message.reply_text("Uso: `/resultado ID WIN|LOSS|PUSH`", parse_mode=ParseMode.MARKDOWN)
    bid, res = a[0].upper(), a[1].upper()
    bets = load_bets()
    f = next((b for b in bets if b.id == bid), None)
    if not f: return await u.message.reply_text("No encontrada.")
    f.result, f.resolved_at = res.lower(), now_ts()
    save_bets(bets)
    await u.message.reply_text(f"✅ #{bid} actualizada a *{res}*", parse_mode=ParseMode.MARKDOWN)

async def cmd_historial(u: Update, c: ContextTypes.DEFAULT_TYPE):
    bts = [b for b in load_bets() if b.user_id == u.effective_user.id and b.result in ("win","loss","push")]
    w, l = sum(1 for b in bts if b.result=="win"), sum(1 for b in bts if b.result=="loss")
    tot = w + l
    net = sum(b.amount for b in bts if b.result=="win") - sum(b.amount for b in bts if b.result=="loss")
    wr = round(w/tot*100,1) if tot else 0
    await u.message.reply_text(f"📊 *Historial*\nTotal: {tot} | ✅ {w}W ❌ {l}L\n🎯 WinRate: *{wr}%*\n💰 Neto: *{net:+}u*", parse_mode=ParseMode.MARKDOWN)

# =========================
# TAREAS EN SEGUNDO PLANO
# =========================
async def background_autoresolve_bets(context: ContextTypes.DEFAULT_TYPE):
    pnd = [b for b in load_bets() if not b.result]
    if not pnd: return
    try: games = await asyncio.to_thread(lambda: scoreboard.ScoreBoard().get_dict()["scoreboard"]["games"])
    except: return

async def background_smart_alerts(context: ContextTypes.DEFAULT_TYPE):
    pass

async def send_morning_digest(context: ContextTypes.DEFAULT_TYPE):
    td = date.today().isoformat()
    st = await asyncio.to_thread(load_json, MORNING_DIGEST_FILE, {})
    if st.get("last_date") == td: return
    st["last_date"] = td
    await asyncio.to_thread(save_json, MORNING_DIGEST_FILE, st)
    try:
        games = await asyncio.to_thread(lambda: scoreboard.ScoreBoard().get_dict()["scoreboard"]["games"])
        gl = [f"• {g['awayTeam']['teamTricode']} @ {g['homeTeam']['teamTricode']}" for g in games]
        await context.bot.send_message(context.job.chat_id, f"🌅 *Buenos días NBA*\n" + "\n".join(gl), parse_mode=ParseMode.MARKDOWN)
    except: pass

async def background_check_morning(context: ContextTypes.DEFAULT_TYPE):
    from datetime import datetime
    if datetime.now().hour == MORNING_DIGEST_HOUR: await send_morning_digest(context)

# =========================
# MULTI-USUARIO Y MAIN
# =========================
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

async def guard(u: Update) -> bool:
    uid = u.effective_user.id
    if is_allowed(uid): return True
    await u.message.reply_text(f"🔒 *Acceso restringido*\nTu ID: `{uid}`", parse_mode=ParseMode.MARKDOWN)
    return False

async def register_job(u: Update, c: ContextTypes.DEFAULT_TYPE):
    uid, uname = u.effective_user.id, u.effective_user.first_name
    us = load_users()
    if not us["allowed"] and not us["admins"]:
        add_user(uid, uname, admin=True)
        await u.message.reply_text(f"👑 *Primer usuario (Admin)*\nID: `{uid}`", parse_mode=ParseMode.MARKDOWN)
    elif not is_allowed(uid):
        return await u.message.reply_text(f"🔒 *Acceso denegado*\nID: `{uid}`", parse_mode=ParseMode.MARKDOWN)
    else: add_user(uid, uname)

    cid = u.effective_chat.id
    if not c.job_queue.get_jobs_by_name(f"morning:{cid}"):
        c.job_queue.run_repeating(background_check_morning, interval=3600, first=10, chat_id=cid, name=f"morning:{cid}")

    await u.message.reply_text("✅ *Bot activado y trabajos iniciados*\nUsa `/odds` o `/help`.", parse_mode=ParseMode.MARKDOWN)

BOT_COMMANDS = [
    BotCommand("start", "Activar bot"), BotCommand("odds", "Props por partido (v2)"),
    BotCommand("games", "Cartelera hoy"), BotCommand("live", "Props en vivo"),
    BotCommand("lineup", "Alineaciones"), BotCommand("analisis", "Análisis profundo"),
    BotCommand("alertas", "Top props del día"), BotCommand("contexto", "Defensas vs Posición"),
    BotCommand("bet", "Apostar"), BotCommand("misapuestas", "Pendientes"),
    BotCommand("historial", "Resultados"), BotCommand("resultado", "Resolver"),
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
    app.add_handler(CommandHandler("analisis", guarded(cmd_analisis)))
    app.add_handler(CommandHandler("alertas", guarded(cmd_alertas)))
    app.add_handler(CommandHandler("contexto", guarded(cmd_contexto)))
    app.add_handler(CommandHandler("bet", guarded(cmd_bet)))
    app.add_handler(CommandHandler("resultado", guarded(cmd_resultado)))
    app.add_handler(CommandHandler("historial", guarded(cmd_historial)))
    app.add_handler(CommandHandler("misapuestas", guarded(cmd_misapuestas)))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_game_selection))

    app.post_init = on_startup
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

