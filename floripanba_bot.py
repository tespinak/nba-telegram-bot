import os
import re
import json
import time
import math
import random
import asyncio
import logging
import uuid
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Tuple, Any
from datetime import date, datetime, timedelta

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
# CONFIGURACIÓN
# =========================
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("nba-bot")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
if not TELEGRAM_TOKEN:
    raise RuntimeError("Missing TELEGRAM_TOKEN env var")

POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "120"))
SEASON = os.environ.get("NBA_SEASON", "2025-26")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
MORNING_HOUR = int(os.environ.get("MORNING_HOUR", "10"))

# Archivos de datos
PROPS_FILE = "props.json"
ALERTS_STATE_FILE = "alerts_state.json"
IDS_CACHE_FILE = "player_ids_cache.json"
GLOG_CACHE_FILE = "gamelog_cache.json"
BETS_FILE = "bets.json"
SMART_ALERTS_FILE = "smart_alerts_state.json"
MORNING_DIGEST_FILE = "morning_digest_state.json"
USERS_FILE = "users.json"

# Constantes de alertas
FINAL_ALERT_THRESHOLD = 75
FINAL_ALERT_THRESHOLD_CLUTCH = 68
SMART_ALERT_THRESH = 68
COOLDOWN_SECONDS = 8 * 60
BLOWOUT_IS = 20
BLOWOUT_STRONG = 22

# Thresholds para alerts en vivo
THRESH_POINTS_OVER = (0.5, 4.0)
THRESH_REB_AST_OVER = (0.5, 1.5)

# Mínimos de minutos
MIN_MINUTES_POINTS = 10.0
MIN_MINUTES_REB_AST = 14.0

# Mapeo de estadísticas
STAT_COL = {"puntos": "PTS", "rebotes": "REB", "asistencias": "AST"}
STD_CAP = {"puntos": 8.0, "rebotes": 4.0, "asistencias": 3.0}
MARGIN_CAP = {"puntos": 8.0, "rebotes": 3.0, "asistencias": 3.0}
TIPO_ICON = {"puntos": "🏀", "rebotes": "💪", "asistencias": "🎯"}

GAMMA = "https://gamma-api.polymarket.com"

# =========================
# UTILIDADES DE PERSISTENCIA
# =========================
def load_json(path: str, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}

def save_json(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def now_ts() -> int:
    return int(time.time())

# =========================
# SESIONES HTTP CON RETRY
# =========================
NBA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Referer": "https://www.nba.com/",
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
    retry = Retry(
        total=8,
        connect=8,
        read=8,
        backoff_factor=1.5,
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
# MODELOS DE DATOS
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

# =========================
# FUNCIONES DE CARGA/GUARDADO
# =========================
def load_props() -> List[Prop]:
    raw = load_json(PROPS_FILE, {"props": []})
    out = []
    for p in raw.get("props", []):
        try:
            out.append(Prop(**p))
        except Exception:
            continue
    return out

def save_props(props: List[Prop]) -> None:
    save_json(PROPS_FILE, {"props": [asdict(p) for p in props]})

def load_bets() -> List[Bet]:
    raw = load_json(BETS_FILE, {"bets": []})
    out = []
    for b in raw.get("bets", []):
        try:
            out.append(Bet(**b))
        except Exception:
            pass
    return out

def save_bets(bets: List[Bet]) -> None:
    save_json(BETS_FILE, {"bets": [asdict(b) for b in bets]})

def load_alert_state() -> dict:
    return load_json(ALERTS_STATE_FILE, {})

def save_alert_state(state: dict) -> None:
    save_json(ALERTS_STATE_FILE, state)

def load_smart_alerts_state() -> dict:
    return load_json(SMART_ALERTS_FILE, {})

def save_smart_alerts_state(state: dict) -> None:
    save_json(SMART_ALERTS_FILE, state)

def load_morning_state() -> dict:
    return load_json(MORNING_DIGEST_FILE, {})

def save_morning_state(state: dict) -> None:
    save_json(MORNING_DIGEST_FILE, state)

def load_users() -> dict:
    return load_json(USERS_FILE, {"allowed": [], "admins": [], "nicknames": {}})

def save_users(data: dict) -> None:
    save_json(USERS_FILE, data)

# =========================
# SISTEMA DE USUARIOS
# =========================
def is_allowed(user_id: int) -> bool:
    users = load_users()
    if not users["allowed"] and not users["admins"]:
        return True
    return user_id in users["allowed"] or user_id in users["admins"] or user_id == ADMIN_ID

def is_admin(user_id: int) -> bool:
    users = load_users()
    return user_id in users["admins"] or user_id == ADMIN_ID

def add_user(user_id: int, nickname: str = "", admin: bool = False) -> bool:
    users = load_users()
    if user_id not in users["allowed"]:
        users["allowed"].append(user_id)
    if admin and user_id not in users["admins"]:
        users["admins"].append(user_id)
    if nickname:
        users["nicknames"][str(user_id)] = nickname
    save_users(users)
    return True

def remove_user(user_id: int) -> bool:
    users = load_users()
    if user_id in users["allowed"]:
        users["allowed"].remove(user_id)
    if user_id in users["admins"]:
        users["admins"].remove(user_id)
    users["nicknames"].pop(str(user_id), None)
    save_users(users)
    return True

def user_display(user_id: int) -> str:
    users = load_users()
    return users["nicknames"].get(str(user_id), f"#{user_id}")

async def guard(update: Update) -> bool:
    user = update.effective_user
    if not user:
        return False
    
    if is_allowed(user.id):
        return True
    
    await update.message.reply_text(
        f"🔒 *Acceso restringido*\n\nTu ID es: `{user.id}`\n"
        f"Pide al admin que ejecute: `/adduser {user.id} {user.first_name}`",
        parse_mode=ParseMode.MARKDOWN
    )
    log.info(f"Acceso denegado a {user.first_name} (id={user.id})")
    return False

# =========================
# CACHE DE JUGADORES
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
        cache[name] = pid
        save_json(IDS_CACHE_FILE, cache)
    return pid

# =========================
# GAMELOG CACHE
# =========================
GLOG_TTL_SECONDS = 6 * 60 * 60

def get_gamelog_table(pid: int) -> Tuple[List[str], List[list]]:
    cache = load_json(GLOG_CACHE_FILE, {})
    k = str(pid)
    now = now_ts()

    if k in cache and (now - int(cache[k].get("ts", 0))) < GLOG_TTL_SECONDS:
        return cache[k].get("headers", []), cache[k].get("rows", [])

    time.sleep(0.5 + random.random() * 0.25)
    
    url = "https://stats.nba.com/stats/playergamelog"
    params = {
        "DateFrom": "", "DateTo": "", "LeagueID": "00",
        "PlayerID": str(pid), "Season": SEASON,
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
        save_json(GLOG_CACHE_FILE, cache)
        return headers, rows

    except Exception as e:
        log.warning(f"Error en get_gamelog_table: {e}")
        return cache.get(k, {}).get("headers", []), cache.get(k, {}).get("rows", [])

# =========================
# FUNCIONES ESTADÍSTICAS BASE
# =========================
def clamp(x: float, lo: float = 0, hi: float = 100) -> float:
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

# =========================
# PRE SCORE (VERSIÓN BASE)
# =========================
def pre_score(pid: int, tipo: str, line: float, side: str) -> Tuple[int, dict]:
    v5 = last_n_values(pid, tipo, 5)
    v10 = last_n_values(pid, tipo, 10)

    h5, n5 = hit_counts(v5, line, side)
    h10, n10 = hit_counts(v10, line, side)

    hit5 = (h5 / n5) if n5 else 0.0
    hit10 = (h10 / n10) if n10 else 0.0

    m5 = [v - line if side == "over" else line - v for v in v5]
    m10 = [v - line if side == "over" else line - v for v in v10]

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
# CACHE DE PRE SCORES
# =========================
PRE_SCORE_CACHE: Dict[str, Tuple[int, int, dict]] = {}

def _pre_cache_key(pid: int, tipo: str, line: float) -> str:
    return f"{pid}:{tipo}:{line}"

def pre_score_cached(pid: int, tipo: str, line: float) -> Tuple[int, int, dict]:
    key = _pre_cache_key(pid, tipo, line)
    if key in PRE_SCORE_CACHE:
        return PRE_SCORE_CACHE[key]
    
    po, meta_o = pre_score(pid, tipo, line, "over")
    pu, _ = pre_score(pid, tipo, line, "under")
    PRE_SCORE_CACHE[key] = (po, pu, meta_o)
    return po, pu, meta_o

# =========================
# CONTEXTO DEFENSIVO
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
    if tricode in _TRICODE_TO_TEAM_ID_CACHE:
        return _TRICODE_TO_TEAM_ID_CACHE[tricode]
    tid = get_team_id_by_tricode(tricode)
    if tid:
        _TRICODE_TO_TEAM_ID_CACHE[tricode] = tid
    return tid

def fetch_league_team_stats() -> Dict[int, dict]:
    now = now_ts()
    cache_key = "league_team_stats"
    
    if cache_key in CONTEXT_CACHE and (now - CONTEXT_CACHE[cache_key].get("ts", 0)) < CONTEXT_TTL:
        return CONTEXT_CACHE[cache_key]["data"]

    time.sleep(0.5)
    url = "https://stats.nba.com/stats/leaguedashteamstats"
    params = {
        "MeasureType": "Advanced", "PerMode": "PerGame",
        "Season": SEASON, "SeasonType": "Regular Season",
        "LeagueID": "00",
    }
    
    try:
        resp = SESSION_NBA.get(url, params=params, timeout=(12, 60))
        if resp.status_code != 200:
            return {}
        
        data = resp.json()
        rs = (data.get("resultSets") or [{}])[0]
        hdrs = rs.get("headers", [])
        rows = rs.get("rowSet", [])
        
        result = {}
        for row in rows:
            rd = dict(zip(hdrs, row))
            tid = int(rd.get("TEAM_ID", 0))
            result[tid] = {
                "team_name": rd.get("TEAM_NAME", ""),
                "def_rating": float(rd.get("DEF_RATING") or 0),
                "pace": float(rd.get("PACE") or 0),
                "off_rating": float(rd.get("OFF_RATING") or 0),
            }
        
        CONTEXT_CACHE[cache_key] = {"ts": now, "data": result}
        return result
    except Exception as e:
        log.warning(f"fetch_league_team_stats: {e}")
        return {}

def fetch_opp_position_stats() -> Dict[int, dict]:
    now = now_ts()
    cache_key = "opp_pos_stats"
    
    if cache_key in CONTEXT_CACHE and (now - CONTEXT_CACHE[cache_key].get("ts", 0)) < CONTEXT_TTL:
        return CONTEXT_CACHE[cache_key]["data"]

    time.sleep(0.4)
    url = "https://stats.nba.com/stats/leaguedashteamstats"
    params = {
        "MeasureType": "Opponent", "PerMode": "PerGame",
        "Season": SEASON, "SeasonType": "Regular Season",
        "LeagueID": "00",
    }
    
    try:
        resp = SESSION_NBA.get(url, params=params, timeout=(12, 60))
        if resp.status_code != 200:
            return {}
        
        data = resp.json()
        rs = (data.get("resultSets") or [{}])[0]
        hdrs = rs.get("headers", [])
        rows = rs.get("rowSet", [])
        
        result = {}
        for row in rows:
            rd = dict(zip(hdrs, row))
            tid = int(rd.get("TEAM_ID", 0))
            result[tid] = {
                "opp_pts": float(rd.get("OPP_PTS") or 0),
                "opp_reb": float(rd.get("OPP_REB") or 0),
                "opp_ast": float(rd.get("OPP_AST") or 0),
            }
        
        CONTEXT_CACHE[cache_key] = {"ts": now, "data": result}
        return result
    except Exception as e:
        log.warning(f"fetch_opp_position_stats: {e}")
        return {}

def get_defensive_context(opp_tricode: str, tipo: str) -> dict:
    result = {
        "def_rating": None, "pace": None, "opp_stat": None,
        "def_rank": None, "pace_rank": None, "opp_stat_rank": None,
        "verdict": ""
    }

    team_stats = fetch_league_team_stats()
    opp_stats = fetch_opp_position_stats()
    
    opp_tid = get_team_id_cached(opp_tricode)
    if not opp_tid or opp_tid not in team_stats:
        return result

    ts = team_stats[opp_tid]
    result["def_rating"] = ts["def_rating"]
    result["pace"] = ts["pace"]

    # Ranking defensivo (menor = mejor)
    all_def = sorted(team_stats.values(), key=lambda x: x["def_rating"])
    for i, t in enumerate(all_def, 1):
        if t.get("team_name") == ts["team_name"]:
            result["def_rank"] = i
            break

    # Ranking de pace (mayor = más rápido)
    all_pace = sorted(team_stats.values(), key=lambda x: x["pace"], reverse=True)
    for i, t in enumerate(all_pace, 1):
        if t.get("team_name") == ts["team_name"]:
            result["pace_rank"] = i
            break

    # Stats permitidas
    if opp_tid in opp_stats:
        os = opp_stats[opp_tid]
        stat_key = {"puntos": "opp_pts", "rebotes": "opp_reb", "asistencias": "opp_ast"}.get(tipo)
        if stat_key and stat_key in os:
            result["opp_stat"] = os[stat_key]
            
            all_opp = sorted(opp_stats.values(), key=lambda x: x.get(stat_key, 0), reverse=True)
            for i, s in enumerate(all_opp, 1):
                if s.get(stat_key) == os[stat_key]:
                    result["opp_stat_rank"] = i
                    break

    # Veredicto automático
    verdicts = []
    if result["def_rank"]:
        if result["def_rank"] >= 25:
            verdicts.append("defensa débil ✅")
        elif result["def_rank"] <= 5:
            verdicts.append("defensa élite ⚠️")
    
    if result["pace_rank"]:
        if result["pace_rank"] <= 5:
            verdicts.append("ritmo alto ✅")
        elif result["pace_rank"] >= 25:
            verdicts.append("ritmo lento ⚠️")
    
    if result["opp_stat_rank"]:
        if result["opp_stat_rank"] <= 8:
            verdicts.append(f"top-8 en {tipo} permitidos ✅")
        elif result["opp_stat_rank"] >= 23:
            verdicts.append(f"bottom-8 en {tipo} permitidos ⚠️")

    result["verdict"] = " · ".join(verdicts) if verdicts else "contexto neutro"
    return result

# =========================
# HOME/AWAY SPLITS
# =========================
def home_away_splits(pid: int, tipo: str) -> dict:
    headers, rows = get_gamelog_table(pid)
    if not headers or not rows:
        return {}
    
    col = STAT_COL.get(tipo)
    if not col:
        return {}
    
    try:
        stat_idx = headers.index(col)
        matchup_idx = headers.index("MATCHUP")
    except ValueError:
        return {}

    home_vals, away_vals = [], []
    for r in rows:
        if matchup_idx >= len(r) or stat_idx >= len(r):
            continue
        
        matchup_str = str(r[matchup_idx])
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
        result["home_n"] = len(home_vals)
    if away_vals:
        result["away_avg"] = round(sum(away_vals) / len(away_vals), 1)
        result["away_n"] = len(away_vals)
    return result

# =========================
# BACK-TO-BACK DETECTION
# =========================
def is_back_to_back(pid: int) -> bool:
    headers, rows = get_gamelog_table(pid)
    if not headers or not rows:
        return False
    
    try:
        date_idx = headers.index("GAME_DATE")
    except ValueError:
        return False
    
    try:
        last_date_str = str(rows[0][date_idx])
        last_date = datetime.strptime(last_date_str, "%b %d, %Y").date()
        yesterday = date.today() - timedelta(days=1)
        return last_date == yesterday
    except Exception:
        return False

# =========================
# PRE SCORE V2 (CON CONTEXTO)
# =========================
def pre_score_v2(pid: int, tipo: str, line: float, side: str,
                 opp_tricode: str = "", is_home: bool = True,
                 rest_days: int = 1) -> Tuple[int, dict]:
    
    base_score, meta = pre_score(pid, tipo, line, side)
    adjustments = []
    adj_total = 0.0

    # Ajuste por contexto defensivo
    if opp_tricode:
        ctx = get_defensive_context(opp_tricode, tipo)
        dr_rank = ctx.get("def_rank")
        pace_rank = ctx.get("pace_rank")
        osr = ctx.get("opp_stat_rank")

        if dr_rank:
            if side == "over":
                if dr_rank >= 25:
                    adj = +8
                    adjustments.append(f"rival def débil +8")
                elif dr_rank >= 20:
                    adj = +4
                    adjustments.append(f"rival def floja +4")
                elif dr_rank <= 5:
                    adj = -8
                    adjustments.append(f"rival def élite -8")
                elif dr_rank <= 10:
                    adj = -4
                    adjustments.append(f"rival def buena -4")
                else:
                    adj = 0
            else:  # under
                if dr_rank <= 5:
                    adj = +8
                    adjustments.append(f"rival def élite +8")
                elif dr_rank <= 10:
                    adj = +4
                    adjustments.append(f"rival def buena +4")
                elif dr_rank >= 25:
                    adj = -8
                    adjustments.append(f"rival def débil -8")
                elif dr_rank >= 20:
                    adj = -4
                    adjustments.append(f"rival def floja -4")
                else:
                    adj = 0
            adj_total += adj

        if pace_rank:
            if side == "over":
                if pace_rank <= 5:
                    adj = +5
                    adjustments.append(f"ritmo alto +5")
                elif pace_rank >= 25:
                    adj = -5
                    adjustments.append(f"ritmo lento -5")
                else:
                    adj = 0
            else:
                if pace_rank >= 25:
                    adj = +5
                    adjustments.append(f"ritmo lento +5")
                elif pace_rank <= 5:
                    adj = -5
                    adjustments.append(f"ritmo alto -5")
                else:
                    adj = 0
            adj_total += adj

        if osr:
            if side == "over":
                if osr <= 8:
                    adj = +6
                    adjustments.append(f"rival permite muchos {tipo} +6")
                elif osr >= 23:
                    adj = -6
                    adjustments.append(f"rival limita {tipo} -6")
                else:
                    adj = 0
            else:
                if osr >= 23:
                    adj = +6
                    adjustments.append(f"rival limita {tipo} +6")
                elif osr <= 8:
                    adj = -6
                    adjustments.append(f"rival permite muchos {tipo} -6")
                else:
                    adj = 0
            adj_total += adj

        meta.update({
            "ctx_def_rank": dr_rank,
            "ctx_pace_rank": pace_rank,
            "ctx_opp_stat": ctx.get("opp_stat"),
            "ctx_osr": osr,
            "ctx_opp_tri": opp_tricode,
        })

    # Ajuste por split H/A
    splits = home_away_splits(pid, tipo)
    loc = "home" if is_home else "away"
    loc_avg = splits.get(f"{loc}_avg")
    if loc_avg is not None:
        gap = loc_avg - line if side == "over" else line - loc_avg
        if gap > 2.0:
            adj = +5
            adjustments.append(f"split {loc} favorable +5")
        elif gap < -2.0:
            adj = -5
            adjustments.append(f"split {loc} desfavorable -5")
        else:
            adj = 0
        adj_total += adj
        meta["ha_split_avg"] = loc_avg
        meta["ha_loc"] = loc

    # Ajuste por descanso
    if rest_days == 0:  # back-to-back
        adj = -6 if side == "over" else +6
        adjustments.append(f"back-to-back {adj:+d}")
        adj_total += adj
    elif rest_days >= 3:  # bien descansado
        adj = +4 if side == "over" else -4
        adjustments.append(f"bien descansado {adj:+d}")
        adj_total += adj

    final = int(clamp(base_score + adj_total, 0, 100))
    meta["v2_base"] = base_score
    meta["v2_adj"] = round(adj_total, 1)
    meta["v2_adjustments"] = adjustments
    return final, meta

# =========================
# FUNCIONES PARA TIEMPO DE JUEGO
# =========================
def parse_minutes(min_str: str) -> float:
    if not min_str:
        return 0.0
    try:
        if ":" in min_str:
            mm, ss = min_str.split(":")
            return float(mm) + float(ss) / 60.0
    except Exception:
        pass
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
# LIVE SCORE FUNCTIONS
# =========================
def should_gate_by_minutes(side: str, tipo: str, value: float, mins: float,
                           elapsed_min: Optional[float], is_blowout: bool) -> bool:
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

def compute_over_score(tipo: str, faltante: float, mins: float, pf: int,
                       period: int, clock_seconds: Optional[int],
                       diff: int, is_clutch: bool, is_blowout: bool) -> int:
    
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
    if period >= 4:
        spot += 12
    elif period == 3:
        spot += 7
    elif period == 2:
        spot += 3

    if clock_seconds is not None:
        if period >= 4:
            spot += clamp((720 - clock_seconds) / 720 * 9, 0, 9)
        elif period == 3:
            spot += clamp((720 - clock_seconds) / 720 * 5, 0, 5)

    if is_clutch:
        spot += 11

    min_score = clamp((mins - min_floor) / 18 * 12, 0, 12)

    foul_pen = 0
    if pf >= 5:
        foul_pen = 18
    elif pf == 4:
        foul_pen = 10
    elif pf == 3:
        foul_pen = 4
    foul_pen *= foul_mult

    blow_pen = 0
    if is_blowout:
        if diff >= 25:
            blow_pen = 18
        elif diff >= 20:
            blow_pen = 12
    blow_pen *= blow_mult

    score = base + spot + min_score - foul_pen - blow_pen
    return int(clamp(score, 0, 100))

def compute_under_score(tipo: str, margin_under: float, mins: float, pf: int,
                        period: int, clock_seconds: Optional[int],
                        diff: int, is_clutch: bool, is_blowout: bool) -> int:
    
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
    if pf >= 5:
        foul_pen = 6
    elif pf == 4:
        foul_pen = 3

    min_bonus = 0
    if elapsed_min is not None and elapsed_min >= 30:
        if mins < 18:
            min_bonus = 12
        elif mins < 24:
            min_bonus = 8

    score = cushion + time_score + blow + min_bonus - (clutch_pen if is_clutch else 0) - foul_pen
    return int(clamp(score, 0, 100))

# =========================
# POLYMARKET FETCHER
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
        log.info(f"Usando cache de Polymarket: {len(PM_CACHE['props'])} props")
        return PM_CACHE["props"]

    try:
        games = scoreboard.ScoreBoard().get_dict()["scoreboard"]["games"]
    except Exception as e:
        log.warning(f"Error en scoreboard: {e}")
        games = []

    props_all = []

    # Intentar obtener props para cada partido
    for g in games:
        away_tri = (g.get("awayTeam", {}) or {}).get("teamTricode", "").lower()
        home_tri = (g.get("homeTeam", {}) or {}).get("teamTricode", "").lower()
        local_slug = f"nba-{away_tri}-{home_tri}-{today}"

        try:
            # Intentar obtener evento por slug
            r = SESSION_PM.get(f"{GAMMA}/events/slug/{local_slug}", timeout=15)
            if r.status_code == 200:
                ev = r.json()
                markets = ev.get("markets", [])

                # Si no hay markets embebidos, buscarlos por event_id
                if not markets and ev.get("id"):
                    mr = SESSION_PM.get(f"{GAMMA}/markets",
                                        params={"event_id": ev["id"], "limit": 200},
                                        timeout=15)
                    if mr.status_code == 200:
                        markets = mr.json() if isinstance(mr.json(), list) else mr.json().get("markets", [])

                # Procesar cada market
                for m in markets:
                    smt = (m.get("sportsMarketType") or m.get("sport_market_type") or "").lower()
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
                        continue

                    # Extraer nombre del jugador
                    player = m.get("groupItemTitle") or m.get("group_item_title") or ""
                    if not player:
                        match = re.search(r"^(.*?)(?::\s*|\s+)(?:Points|Rebounds|Assists)", q, re.IGNORECASE)
                        if match:
                            player = match.group(1).strip()

                    # Extraer línea
                    try:
                        line_val = float(m.get("line", 0))
                    except (ValueError, TypeError):
                        match = re.search(r"O\/U\s*(\d+(?:\.\d+)?)", q, re.IGNORECASE)
                        line_val = float(match.group(1)) if match else None

                    if player and line_val:
                        tipo = {"points": "puntos", "rebounds": "rebotes", "assists": "asistencias"}[smt]
                        market_id = str(m.get("id") or "")

                        props_all.append(Prop(
                            player=player, tipo=tipo, line=line_val, side="over",
                            source="polymarket", game_slug=local_slug, market_id=market_id
                        ))
                        props_all.append(Prop(
                            player=player, tipo=tipo, line=line_val, side="under",
                            source="polymarket", game_slug=local_slug, market_id=market_id
                        ))
        except Exception as e:
            log.debug(f"Error obteniendo props de {local_slug}: {e}")
            continue

    # Deduplicar
    seen = set()
    uniq = []
    for p in props_all:
        k = (p.game_slug, p.player.lower(), p.tipo, p.side, float(p.line))
        if k not in seen:
            seen.add(k)
            uniq.append(p)

    # Fallback si no se obtuvieron props
    if not uniq:
        log.warning("⚠️ Usando fallback de props")
        fallback_slug = f"nba-okc-det-{today}"
        uniq = [
            Prop("Shai Gilgeous-Alexander", "puntos", 32.5, "over", "fallback", fallback_slug),
            Prop("Shai Gilgeous-Alexander", "puntos", 32.5, "under", "fallback", fallback_slug),
            Prop("Cade Cunningham", "puntos", 28.5, "over", "fallback", fallback_slug),
            Prop("Cade Cunningham", "puntos", 28.5, "under", "fallback", fallback_slug),
        ]

    PM_CACHE["date"] = today
    PM_CACHE["ts"] = now
    PM_CACHE["props"] = uniq
    log.info(f"Props cargados: {len(uniq)} ({len(uniq)//2} jugadores)")
    return uniq

# =========================
# UTILIDADES DE UI
# =========================
def _pre_rating_emoji(score: int) -> str:
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
    filled = round(score / 100 * length)
    return "█" * filled + "░" * (length - filled)

def _pre_label(score: int) -> str:
    if score >= 75:
        return "FUERTE"
    elif score >= 60:
        return "BUENA"
    elif score >= 45:
        return "MEDIA"
    elif score >= 30:
        return "DÉBIL"
    else:
        return "BAJA"

def _slug_to_matchup(slug: str) -> str:
    parts = slug.replace("nba-", "").split("-")
    if len(parts) >= 2:
        return f"{parts[0].upper()} @ {parts[1].upper()}"
    return slug

async def _send_long_message(update: Update, text: str, max_len: int = 3800) -> None:
    if len(text) <= max_len:
        try:
            await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            log.warning(f"Error de markdown: {e}")
            clean_text = text.replace("*", "").replace("_", "").replace("`", "")
            await update.message.reply_text(clean_text)
        return

    parts = []
    remaining = text

    while len(remaining) > max_len:
        cut = remaining[:max_len].rfind("\n👤")
        if cut < 200:
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
            log.warning(f"Error de markdown en parte {i}: {e}")
            await update.message.reply_text((prefix + part).replace("*", "").replace("_", "").replace("`", ""))
        await asyncio.sleep(0.3)

# =========================
# COMANDOS PRINCIPALES
# =========================
HELP_TEXT = (
    "🧠 *NBA Props Bot v3*\n\n"
    "*📋 Programación*\n"
    "• `/games` → partidos de hoy\n"
    "• `/lineup` → alineaciones + injury report\n"
    "   `/lineup BOS` → filtrar por equipo\n\n"
    "*📊 Props & Análisis*\n"
    "• `/odds` → menú interactivo de props con scores\n"
    "• `/analisis Jugador | tipo | side | linea` → análisis profundo\n"
    "• `/contexto AWAY HOME` → contexto defensivo\n\n"
    "*🔴 En vivo*\n"
    "• `/live` → props en vivo con scoring\n\n"
    "*💰 Apuestas*\n"
    "• `/bet Jugador | tipo | side | linea | monto`\n"
    "• `/misapuestas` → pendientes\n"
    "• `/resultado ID WIN|LOSS|PUSH` → cerrar manual\n"
    "• `/historial` → ROI y estadísticas\n\n"
    "*⚙️ Otros*\n"
    "• `/miperfil` → ver mi ID y perfil\n"
    "• `/debug` → estado técnico\n"
)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)

async def cmd_games(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        games = await asyncio.wait_for(
            asyncio.to_thread(lambda: scoreboard.ScoreBoard().get_dict()["scoreboard"]["games"]),
            timeout=20.0
        )
    except asyncio.TimeoutError:
        await update.message.reply_text("⚠️ Timeout leyendo scoreboard")
        return
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {e}")
        return

    if not games:
        await update.message.reply_text("No hay juegos hoy")
        return

    lines = ["📅 *NBA hoy*"]
    for g in games:
        away = g.get("awayTeam", {})
        home = g.get("homeTeam", {})
        at = away.get("teamTricode", "?")
        ht = home.get("teamTricode", "?")
        status = g.get("gameStatusText", "")
        slug = _slug_from_scoreboard_game(g)
        lines.append(f"• {at} @ {ht} — {status}\n  `{slug}`")

    msg = "\n".join(lines)
    if len(msg) > 3800:
        msg = msg[:3800] + "\n…"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

# =========================
# COMANDO LINEUP (AGREGADO)
# =========================
def fetch_boxscore_injury_data(game_id: str) -> Dict[str, List[dict]]:
    """Obtiene datos de jugadores del boxscore pre-game / live"""
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
    """Muestra alineaciones e injury report"""
    msg = await update.message.reply_text(
        "⏳ Obteniendo alineaciones e injury report...",
        parse_mode=ParseMode.MARKDOWN
    )

    try:
        board = await asyncio.to_thread(lambda: scoreboard.ScoreBoard().get_dict()["scoreboard"])
        games = board.get("games", [])
    except Exception as e:
        await msg.edit_text(f"⚠️ Error leyendo scoreboard: {e}")
        return

    if not games:
        await msg.edit_text("No hay partidos NBA hoy.")
        return

    # Filtrar por equipo si se pasó argumento
    args = context.args or []
    filter_tri = " ".join(args).strip().upper() if args else None

    await msg.edit_text(f"🔄 Cargando datos de {len(games)} partidos...")

    sent_any = False
    for g in games:
        away_team = g.get("awayTeam", {}) or {}
        home_team = g.get("homeTeam", {}) or {}
        away_tri = away_team.get("teamTricode", "")
        home_tri = home_team.get("teamTricode", "")
        game_id = g.get("gameId", "")
        status_txt = g.get("gameStatusText", "")
        game_status = g.get("gameStatus", 1)

        if filter_tri and filter_tri not in (away_tri, home_tri):
            continue

        # Obtener datos de jugadores desde boxscore
        box_data = await asyncio.to_thread(fetch_boxscore_injury_data, game_id) if game_id else {}
        away_players = box_data.get(away_tri, [])
        home_players = box_data.get(home_tri, [])

        # Formatear mensaje
        game_label = f"✈️ *{away_tri}* @ 🏠 *{home_tri}*"
        status_icon = "🟢 EN VIVO" if game_status == 2 else ("⏰ PREVIO" if game_status == 1 else "🏁 FINAL")

        header = (
            f"{'─'*32}\n"
            f"{game_label}\n"
            f"{status_icon} | {status_txt}\n"
            f"{'─'*32}"
        )

        away_fmt = format_team_lineup(away_tri, away_players) if away_players else f"*{away_tri}*\n  _(sin datos aún)_"
        home_fmt = format_team_lineup(home_tri, home_players) if home_players else f"*{home_tri}*\n  _(sin datos aún)_"

        full_msg = f"{header}\n\n{away_fmt}\n\n{home_fmt}"

        if len(full_msg) > 3900:
            full_msg = full_msg[:3900] + "\n…(recortado)"

        try:
            await update.message.reply_text(full_msg, parse_mode=ParseMode.MARKDOWN)
            sent_any = True
        except Exception as e:
            log.warning(f"Error enviando lineup msg: {e}")
            await update.message.reply_text(full_msg.replace("*", "").replace("_", "").replace("`", ""))
            sent_any = True

        await asyncio.sleep(0.5)

    await msg.delete()

    if not sent_any and filter_tri:
        await update.message.reply_text(f"No encontré datos para `{filter_tri}`.")

# =========================
# COMANDO ODDS INTERACTIVO
# =========================
async def cmd_odds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text(
        "⏳ *Cargando partidos de hoy...*",
        parse_mode=ParseMode.MARKDOWN
    )

    props_pm = await asyncio.to_thread(polymarket_props_today_from_scoreboard)

    if not props_pm:
        await msg.edit_text("❌ No pude obtener props. Usa `/debug`.")
        return

    # Agrupar por partido
    games_dict: Dict[str, List[Prop]] = {}
    for p in props_pm:
        slug = p.game_slug or "unknown"
        games_dict.setdefault(slug, []).append(p)

    # Si hay argumento, mostrar ese partido directamente
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

        await msg.edit_text(f"❌ No encontré el partido `{slug_filter}`")
        return

    # Mostrar menú
    today_str = date.today().strftime("%d/%m/%Y")
    header = f"📋 *NBA Props — {today_str}*\n🎮 *Selecciona un partido:*\n{'─'*30}\n"

    game_lines = []
    for i, (slug, props) in enumerate(games_dict.items(), 1):
        matchup = _slug_to_matchup(slug)
        players = set(p.player for p in props)
        game_lines.append(
            f"{i}. *{matchup}*\n"
            f"   `{slug}`\n"
            f"   👤 {len(players)} jug | 📊 {len(props)//2} líneas\n"
        )

    footer = (
        f"{'─'*30}\n"
        f"Responde con el número del partido (1,2,3...)\n"
        f"O usa `/odds BOS` para buscar por equipo"
    )

    context.user_data['games_menu'] = list(games_dict.keys())
    await msg.edit_text(header + "\n".join(game_lines) + footer, parse_mode=ParseMode.MARKDOWN)

async def handle_game_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'games_menu' not in context.user_data:
        return

    text = update.message.text.strip()
    if not text.isdigit():
        return

    idx = int(text) - 1
    games = context.user_data['games_menu']

    if 0 <= idx < len(games):
        slug = games[idx]
        props_pm = await asyncio.to_thread(polymarket_props_today_from_scoreboard)
        game_props = [p for p in props_pm if (p.game_slug or "") == slug]

        if game_props:
            await show_game_props_advanced(update, context, slug, game_props)
            del context.user_data['games_menu']
        else:
            await update.message.reply_text(f"❌ Error cargando props")
    else:
        await update.message.reply_text(f"❌ Número inválido")

async def show_game_props_advanced(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                   slug: str, props: List[Prop]) -> None:
    matchup = _slug_to_matchup(slug)
    msg = await update.message.reply_text(
        f"⚡ *Calculando scores para {matchup}...*",
        parse_mode=ParseMode.MARKDOWN
    )

    # Extraer equipos del slug
    parts_slug = slug.replace("nba-", "").split("-")
    away_tri = parts_slug[0].upper() if len(parts_slug) >= 2 else "???"
    home_tri = parts_slug[1].upper() if len(parts_slug) >= 2 else "???"

    # Agrupar props únicos (solo over para evitar duplicados)
    unique_lines: Dict[str, List[Tuple[str, float]]] = {}
    seen_lines = set()

    for p in props:
        if p.side != "over":
            continue
        key = (p.player, p.tipo, p.line)
        if key in seen_lines:
            continue
        seen_lines.add(key)
        unique_lines.setdefault(p.player, []).append((p.tipo, p.line))

    # Función para calcular en thread
    def _calc_player(player: str, lines: List[Tuple[str, float]]) -> Tuple[str, List[dict]]:
        pid = get_pid_for_name(player)
        if not pid:
            return player, []

        # Determinar si es local o visitante
        opp_tricode = home_tri
        is_home = False

        try:
            # Intentar determinar equipo del jugador
            for team_tri, team_id in [(away_tri, get_team_id_cached(away_tri)),
                                       (home_tri, get_team_id_cached(home_tri))]:
                if team_id:
                    roster = commonteamroster.CommonTeamRoster(
                        team_id=team_id, season=SEASON
                    ).get_data_frames()[0]
                    if pid in roster['PLAYER_ID'].values:
                        opp_tricode = home_tri if team_tri == away_tri else away_tri
                        is_home = (team_tri == home_tri)
                        break
        except Exception:
            pass

        # Días de descanso
        rest = 1
        try:
            if is_back_to_back(pid):
                rest = 0
        except Exception:
            pass

        results = []
        for tipo, line in lines:
            po, meta_o = pre_score_v2(pid, tipo, line, "over", opp_tricode, is_home, rest)
            pu, _ = pre_score_v2(pid, tipo, line, "under", opp_tricode, is_home, rest)
            results.append({
                "tipo": tipo,
                "line": line,
                "po": po,
                "pu": pu,
                "meta": meta_o,
            })
        return player, results

    # Ejecutar con semáforo para no saturar la API
    sem = asyncio.Semaphore(3)

    async def _safe_calc(player, lines):
        async with sem:
            return await asyncio.wait_for(
                asyncio.to_thread(_calc_player, player, lines),
                timeout=30.0
            )

    tasks = [_safe_calc(pl, ln) for pl, ln in unique_lines.items()]

    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
    except Exception as e:
        await msg.edit_text(f"❌ Error: {e}")
        return

    players_data = {}
    for item in results:
        if isinstance(item, Exception):
            log.warning(f"Error en cálculo: {item}")
            continue
        pl, res = item
        if res:
            players_data[pl] = res

    if not players_data:
        await msg.edit_text("❌ No se pudieron calcular scores. Intenta más tarde.")
        return

    # Ordenar por mejor score
    tipo_order = {"puntos": 0, "rebotes": 1, "asistencias": 2}

    def best_score(entries):
        return max(max(e["po"], e["pu"]) for e in entries)

    players_sorted = sorted(players_data.keys(),
                            key=lambda pl: best_score(players_data[pl]),
                            reverse=True)

    lines = [f"🟣 *{matchup}*\n`{slug}`\n{'─'*28}"]

    for pl in players_sorted:
        lines.append(f"\n👤 *{pl}*")
        entries = sorted(players_data[pl], key=lambda e: tipo_order.get(e["tipo"], 9))

        for e in entries:
            tipo = e["tipo"]
            po = e["po"]
            pu = e["pu"]
            meta = e["meta"]

            h5 = meta.get("hits5", "?")
            n5 = meta.get("n5", "?")
            h10 = meta.get("hits10", "?")
            n10 = meta.get("n10", "?")
            avg10 = meta.get("avg10", None)

            avg_str = f"prom10: *{avg10:.1f}*" if avg10 is not None else ""

            # Ajustes v2
            adj_list = meta.get("v2_adjustments", [])
            adj_str = f"  _(adj: {', '.join(adj_list)[:30]})_\n" if adj_list else ""

            # Contexto resumido
            ctx_parts = []
            if meta.get("ctx_def_rank"):
                ctx_parts.append(f"Def#{meta['ctx_def_rank']}")
            if meta.get("ctx_pace_rank"):
                ctx_parts.append(f"Pace#{meta['ctx_pace_rank']}")
            ctx_line = f"  🛡️ `{' · '.join(ctx_parts)}`\n" if ctx_parts else ""

            lines.append(
                f"{TIPO_ICON.get(tipo, '•')} *{tipo.upper()}* — `{e['line']}`\n"
                f"  OVER  {_pre_rating_emoji(po)} `{po:>3}/100` {_pre_bar(po)}\n"
                f"  UNDER {_pre_rating_emoji(pu)} `{pu:>3}/100` {_pre_bar(pu)}\n"
                f"{adj_str}{ctx_line}"
                f"  📊 `{h5}/{n5}` últ5 | `{h10}/{n10}` últ10  {avg_str}"
            )

    await msg.delete()
    await _send_long_message(update, "\n".join(lines))

# =========================
# COMANDO LIVE
# =========================
async def cmd_live(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text(
        "⏳ *Cargando datos en vivo...*",
        parse_mode=ParseMode.MARKDOWN
    )

    try:
        games = await asyncio.wait_for(
            asyncio.to_thread(lambda: scoreboard.ScoreBoard().get_dict()["scoreboard"]["games"]),
            timeout=20.0
        )
    except Exception as e:
        await msg.edit_text(f"⚠️ Error: {e}")
        return

    live_games = [g for g in games if g.get("gameStatus") == 2]
    if not live_games:
        await msg.edit_text("⏸️ No hay partidos en vivo ahora")
        return

    await msg.edit_text(f"🔄 *{len(live_games)} partido(s) en vivo*", parse_mode=ParseMode.MARKDOWN)

    # Props del día
    props_pm = PM_CACHE.get("props", [])
    if not props_pm:
        props_pm = await asyncio.to_thread(polymarket_props_today_from_scoreboard)

    props_manual = await asyncio.to_thread(load_props)
    all_props = props_manual + props_pm

    # Índice por nombre de jugador
    props_by_name = {}
    for p in all_props:
        props_by_name.setdefault(p.player.lower(), []).append(p)

    async def fetch_box(gid: str):
        try:
            return gid, await asyncio.wait_for(
                asyncio.to_thread(lambda: boxscore.BoxScore(gid).get_dict()["game"]),
                timeout=15.0
            )
        except Exception:
            return gid, None

    box_results = await asyncio.gather(*[fetch_box(g["gameId"]) for g in live_games])

    scored_rows = []

    for g, (gid, box) in zip(live_games, box_results):
        if not box:
            continue

        status = g.get("gameStatusText", "")
        period = int(g.get("period", 0) or 0)
        game_clock = g.get("gameClock", "") or ""
        clock_sec = clock_to_seconds(game_clock)
        home_score = int(g.get("homeTeam", {}).get("score", 0))
        away_score = int(g.get("awayTeam", {}).get("score", 0))
        diff = abs(home_score - away_score)
        is_clutch = diff <= 8
        is_blowout = diff >= BLOWOUT_IS
        elapsed_min = game_elapsed_minutes(period, clock_sec)

        for team_key in ["homeTeam", "awayTeam"]:
            for pl in box.get(team_key, {}).get("players", []):
                first = pl.get("firstName", "") or ""
                last = pl.get("familyName", "") or pl.get("lastName", "") or ""
                full_name = f"{first} {last}".strip().lower()
                pid = pl.get("personId")

                matching = props_by_name.get(full_name, [])
                if not matching and last:
                    for key, plist in props_by_name.items():
                        if last.lower() in key or key in last.lower():
                            matching = plist
                            break

                if not matching:
                    continue

                s = pl.get("statistics", {})
                pts = float(s.get("points", 0) or 0)
                reb = float(s.get("reboundsTotal", 0) or 0)
                ast = float(s.get("assists", 0) or 0)
                pf = float(s.get("foulsPersonal", 0) or 0)
                mins = parse_minutes(s.get("minutes", ""))

                for pr in matching:
                    actual = pts if pr.tipo == "puntos" else (reb if pr.tipo == "rebotes" else ast)

                    # Obtener PRE score (con cache)
                    pre_val, meta = await asyncio.to_thread(pre_score, pid, pr.tipo, pr.line, pr.side)

                    if pr.side == "over":
                        faltante = pr.line - actual
                        lo, hi = THRESH_POINTS_OVER if pr.tipo == "puntos" else THRESH_REB_AST_OVER

                        if not (lo <= faltante <= hi):
                            continue
                        if should_gate_by_minutes("over", pr.tipo, faltante, mins, elapsed_min, is_blowout):
                            continue
                        if is_blowout and diff >= BLOWOUT_STRONG:
                            if (pr.tipo == "puntos" and faltante > 1.0) or faltante > 0.8:
                                continue

                        live_sc = compute_over_score(
                            pr.tipo, faltante, mins, pf, period, clock_sec,
                            diff, is_clutch, is_blowout
                        )
                        final = int(clamp(0.55 * live_sc + 0.45 * pre_val, 0, 100))
                        scored_rows.append((
                            final, live_sc, pre_val, pr, actual, faltante,
                            status, period, game_clock, mins, pf, diff, meta
                        ))

                    else:  # under
                        margin = pr.line - actual
                        if should_gate_by_minutes("under", pr.tipo, margin, mins, elapsed_min, is_blowout):
                            continue

                        live_sc = compute_under_score(
                            pr.tipo, margin, mins, pf, period, clock_sec,
                            diff, is_clutch, is_blowout
                        )
                        final = int(clamp(0.65 * live_sc + 0.35 * pre_val, 0, 100))
                        scored_rows.append((
                            final, live_sc, pre_val, pr, actual, margin,
                            status, period, game_clock, mins, pf, diff, meta
                        ))

    await msg.delete()

    if not scored_rows:
        await update.message.reply_text(
            "📭 *Sin señal en vivo ahora*\n\n"
            "Posibles causas:\n"
            "• Ninguna prop está cerca de su línea\n"
            "• Los jugadores llevan pocos minutos\n"
            "• Usa `/odds` primero para cargar props",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    scored_rows.sort(key=lambda x: x[0], reverse=True)
    top = scored_rows[:15]

    out = [f"🔥 *LIVE — {len(live_games)} partido(s)*\n{'─'*28}"]

    for (final, live, pre, pr, act, delta, st, q, clk, m, pf, df, meta) in top:
        side_tag = "OVER" if pr.side == "over" else "UNDER"
        extra = f"faltan `{delta:.1f}`" if pr.side == "over" else f"colchón `{delta:.1f}`"
        icon = TIPO_ICON.get(pr.tipo, "•")
        pre_e = _pre_rating_emoji(final)

        out.append(
            f"\n{pre_e} `{final}/100` — *{pr.player}*\n"
            f"{icon} {pr.tipo.upper()} {side_tag} `{pr.line}` | actual `{act:.0f}` ({extra})\n"
            f"⏱️ {st} Q{q} {clk} | MIN `{m:.0f}` PF `{pf}` Dif `{df}`\n"
            f"📊 `{meta.get('hits5','?')}/{meta.get('n5','?')}` últ5 | "
            f"`{meta.get('hits10','?')}/{meta.get('n10','?')}` últ10"
        )

    await _send_long_message(update, "\n".join(out))

# =========================
# COMANDO ANÁLISIS
# =========================
async def cmd_analisis(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        await update.message.reply_text("Necesito 4 campos separados por `|`")
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
        await update.message.reply_text("La línea debe ser un número")
        return

    msg = await update.message.reply_text(
        f"🔍 Analizando *{player_name}*...",
        parse_mode=ParseMode.MARKDOWN
    )

    def _run():
        pid = get_pid_for_name(player_name)
        if not pid:
            return None, None, None

        po, pu, meta = pre_score_cached(pid, tipo, line)
        pre = po if side == "over" else pu

        # Detectar rival y localía
        opp_tricode = "???"
        is_home = True

        for p in PM_CACHE.get("props", []):
            if p.player.lower() == player_name.lower() and p.game_slug:
                slug_parts = (p.game_slug or "").replace("nba-", "").split("-")
                if len(slug_parts) >= 2:
                    opp_tricode = slug_parts[1].upper()
                    is_home = False
                break

        return pid, pre, meta, opp_tricode, is_home

    pid, pre, meta, opp_tricode, is_home = await asyncio.to_thread(_run)

    if not pid:
        await msg.edit_text(f"⚠️ No encontré al jugador: *{player_name}*")
        return

    # Construir análisis
    v10 = last_n_values(pid, tipo, 10)
    v5 = last_n_values(pid, tipo, 5)

    avg10 = round(sum(v10) / len(v10), 1) if v10 else 0
    avg5 = round(sum(v5) / len(v5), 1) if v5 else 0

    # Racha
    hits, total = hit_counts(v10, line, side)
    racha = f"{hits}/{total} cumplidos" if total else "sin datos"

    # Splits
    splits = home_away_splits(pid, tipo)
    split_str = ""
    if splits:
        loc = "local" if is_home else "visitante"
        loc_avg = splits.get(f"{'home' if is_home else 'away'}_avg")
        opp_avg = splits.get(f"{'away' if is_home else 'home'}_avg")
        if loc_avg:
            split_str = f"\n   • Promedio como {loc}: `{loc_avg}`"
        if opp_avg:
            split_str += f"\n   • Promedio como {'visitante' if is_home else 'local'}: `{opp_avg}`"

    # Contexto defensivo
    ctx = get_defensive_context(opp_tricode, tipo)
    ctx_str = ""
    if ctx.get("def_rating"):
        ctx_str = f"\n   • Def Rating rival: `{ctx['def_rating']:.1f}` (rank #{ctx.get('def_rank','?')})"
        if ctx.get("opp_stat"):
            ctx_str += f"\n   • {tipo.capitalize()} permitidos: `{ctx['opp_stat']:.1f}` (rank #{ctx.get('opp_stat_rank','?')})"

    analysis = (
        f"🔬 *ANÁLISIS DE {player_name}*\n"
        f"{'─'*30}\n"
        f"📊 *Estadísticas recientes*\n"
        f"   • Promedio últ.5: `{avg5}`\n"
        f"   • Promedio últ.10: `{avg10}`\n"
        f"   • Racha: {racha}\n"
        f"{split_str}\n\n"
        f"🛡️ *Contexto vs {opp_tricode}*\n"
        f"{ctx_str}\n\n"
        f"📈 *PRE Score*\n"
        f"   {_pre_rating_emoji(pre)} `{pre}/100` {_pre_bar(pre)} _{_pre_label(pre)}_\n"
        f"   Ajustes: {', '.join(meta.get('v2_adjustments', ['ninguno']))}"
    )

    await msg.edit_text(analysis, parse_mode=ParseMode.MARKDOWN)

# =========================
# COMANDO CONTEXTO
# =========================
async def cmd_contexto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Uso: `/contexto AWAY HOME`\nEj: `/contexto BOS DEN`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    away_tri = args[0].upper()
    home_tri = args[1].upper()

    msg = await update.message.reply_text(
        f"⏳ Cargando contexto *{away_tri} @ {home_tri}*...",
        parse_mode=ParseMode.MARKDOWN
    )

    def _fetch():
        # Forzar carga de stats
        fetch_league_team_stats()
        fetch_opp_position_stats()

        away_ctx = {
            "pts": get_defensive_context(home_tri, "puntos"),
            "reb": get_defensive_context(home_tri, "rebotes"),
            "ast": get_defensive_context(home_tri, "asistencias"),
        }
        home_ctx = {
            "pts": get_defensive_context(away_tri, "puntos"),
            "reb": get_defensive_context(away_tri, "rebotes"),
            "ast": get_defensive_context(away_tri, "asistencias"),
        }
        return away_ctx, home_ctx

    away_ctx, home_ctx = await asyncio.to_thread(_fetch)

    def _fmt_team(tri: str, ctx_dict: dict, label: str) -> str:
        lines = [f"*{label} — {tri}*"]

        for tipo, key in [("Puntos", "pts"), ("Rebotes", "reb"), ("Asistencias", "ast")]:
            ctx = ctx_dict[key]
            if ctx.get("def_rating"):
                lines.append(
                    f"  • {tipo}: {ctx.get('opp_stat', 0):.1f}/j (rank #{ctx.get('opp_stat_rank','?')})\n"
                    f"    Def Rating {ctx['def_rating']:.1f} (#{ctx.get('def_rank','?')}) · "
                    f"Pace {ctx['pace']:.1f} (#{ctx.get('pace_rank','?')})"
                )
            else:
                lines.append(f"  • {tipo}: sin datos")

        return "\n".join(lines)

    away_block = _fmt_team(home_tri, away_ctx, "Defensa del rival de los visitantes")
    home_block = _fmt_team(away_tri, home_ctx, "Defensa del rival de los locales")

    full = (
        f"🛡️ *CONTEXTO: {away_tri} @ {home_tri}*\n{'─'*30}\n\n"
        f"{away_block}\n\n{'─'*30}\n\n{home_block}\n\n"
        f"_Rank #1 = mejor defensa / permite menos_"
    )

    await msg.edit_text(full, parse_mode=ParseMode.MARKDOWN)

# =========================
# COMANDOS DE APUESTAS
# =========================
def _parse_bet_command(text: str) -> Optional[dict]:
    body = re.sub(r"^/bet(@\w+)?\s*", "", text).strip()
    parts = [x.strip() for x in body.split("|")]
    if len(parts) < 4:
        return None

    player, tipo, side, line_s = parts[0], parts[1].lower(), parts[2].lower(), parts[3]
    amount_s = parts[4] if len(parts) >= 5 else "1"

    if tipo not in ("puntos", "rebotes", "asistencias"):
        return None
    if side not in ("over", "under"):
        return None

    try:
        line = float(line_s)
        amount = float(amount_s)
    except Exception:
        return None

    return {"player": player, "tipo": tipo, "side": side, "line": line, "amount": amount}

def _new_bet_id() -> str:
    return str(uuid.uuid4())[:8].upper()

async def cmd_bet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parsed = _parse_bet_command(update.message.text or "")
    if not parsed:
        await update.message.reply_text(
            "Formato: `/bet Jugador | tipo | side | linea | monto`\n"
            "Ej: `/bet Nikola Jokic | puntos | over | 27.5 | 50`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    msg = await update.message.reply_text("⏳ Registrando apuesta...", parse_mode=ParseMode.MARKDOWN)
    user_id = update.effective_user.id

    def _calc():
        pid = get_pid_for_name(parsed["player"])
        if not pid:
            return None, 0, ""

        po, pu, _ = pre_score_cached(pid, parsed["tipo"], parsed["line"])
        pre = po if parsed["side"] == "over" else pu

        slug = ""
        for p in PM_CACHE.get("props", []):
            if p.player.lower() == parsed["player"].lower():
                slug = p.game_slug or ""
                break

        return pid, pre, slug

    pid, pre, slug = await asyncio.to_thread(_calc)

    if not pid:
        await msg.edit_text(f"⚠️ Jugador no encontrado: *{parsed['player']}*")
        return

    bet = Bet(
        id=_new_bet_id(),
        user_id=user_id,
        player=parsed["player"],
        tipo=parsed["tipo"],
        side=parsed["side"],
        line=parsed["line"],
        amount=parsed["amount"],
        pre_score=pre,
        game_slug=slug,
        placed_at=now_ts(),
    )

    bets = load_bets()
    bets.append(bet)
    save_bets(bets)

    pre_e = _pre_rating_emoji(pre)
    pre_bar = _pre_bar(pre)

    confirm = (
        f"✅ *Apuesta registrada* `#{bet.id}`\n"
        f"{'─'*24}\n"
        f"👤 *{bet.player}*\n"
        f"{TIPO_ICON.get(bet.tipo, '•')} {bet.tipo.upper()} {bet.side.upper()} `{bet.line}`\n"
        f"💰 Monto: `{bet.amount}` unidades\n"
        f"{pre_e} PRE Score: `{pre}/100` {pre_bar}\n"
        f"_Usa `/resultado {bet.id} WIN` al terminar_"
    )

    await msg.edit_text(confirm, parse_mode=ParseMode.MARKDOWN)

async def cmd_misapuestas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    bets = load_bets()
    pending = [b for b in bets if b.user_id == user_id and not b.result]

    if not pending:
        await update.message.reply_text("No tienes apuestas pendientes.")
        return

    lines = [f"⏳ *Apuestas pendientes* ({len(pending)})"]
    for b in sorted(pending, key=lambda x: x.placed_at, reverse=True):
        pre_e = _pre_rating_emoji(b.pre_score)
        lines.append(
            f"\n`#{b.id}` {TIPO_ICON.get(b.tipo, '•')} *{b.player}*\n"
            f"  {b.tipo.upper()} {b.side.upper()} `{b.line}` — `{b.amount}`u\n"
            f"  {pre_e} PRE `{b.pre_score}/100`"
        )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def cmd_resultado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Formato: `/resultado ID WIN|LOSS|PUSH`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    bet_id = args[0].upper()
    result = args[1].upper()
    actual = float(args[2]) if len(args) >= 3 else None

    if result not in ("WIN", "LOSS", "PUSH"):
        await update.message.reply_text("Resultado debe ser WIN, LOSS o PUSH")
        return

    bets = load_bets()
    found = None
    for b in bets:
        if b.id == bet_id:
            found = b
            b.result = result.lower()
            b.actual_stat = actual
            b.resolved_at = now_ts()
            break

    if not found:
        await update.message.reply_text(f"No encontré la apuesta `{bet_id}`")
        return

    save_bets(bets)

    emoji = {"win": "✅", "loss": "❌", "push": "🔁"}.get(result.lower(), "❓")
    await update.message.reply_text(
        f"{emoji} Apuesta `#{bet_id}` → *{result}*",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_historial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    days = int(args[0]) if args and args[0].isdigit() else 30
    user_id = update.effective_user.id
    cutoff = now_ts() - days * 86400

    bets = load_bets()
    mine = [b for b in bets if b.user_id == user_id and b.placed_at >= cutoff]

    if not mine:
        await update.message.reply_text(f"No tienes apuestas en los últimos {days} días")
        return

    resolved = [b for b in mine if b.result in ("win", "loss", "push")]
    wins = sum(1 for b in resolved if b.result == "win")
    losses = sum(1 for b in resolved if b.result == "loss")
    pushes = sum(1 for b in resolved if b.result == "push")

    total = wins + losses
    win_rate = round(wins / total * 100, 1) if total else 0
    net = sum(b.amount for b in resolved if b.result == "win") - sum(b.amount for b in resolved if b.result == "loss")

    msg = (
        f"📊 *Mi historial — últimos {days} días*\n"
        f"{'─'*24}\n"
        f"Total: `{len(mine)}` (resueltas: `{len(resolved)}`)\n"
        f"✅ Wins: `{wins}`  ❌ Losses: `{losses}`  🔁 Push: `{pushes}`\n"
        f"🎯 Win rate: *{win_rate}%*\n"
        f"💰 Neto: `{net:.1f}` unidades"
    )

    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

# =========================
# COMANDOS DE ADMIN/DEBUG
# =========================
async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["🔍 *DEBUG*"]

    # Partidos de hoy
    try:
        games = await asyncio.to_thread(lambda: scoreboard.ScoreBoard().get_dict()["scoreboard"]["games"])
        lines.append(f"📅 Partidos NBA hoy: {len(games)}")
        for g in games[:3]:
            slug = _slug_from_scoreboard_game(g)
            lines.append(f"  • `{slug}`")
    except Exception as e:
        lines.append(f"❌ Error scoreboard: {e}")

    # Props en cache
    props = PM_CACHE.get("props", [])
    lines.append(f"\n📦 Props en cache: {len(props)}")
    if props:
        fuentes = {}
        for p in props:
            fuentes[p.source] = fuentes.get(p.source, 0) + 1
        for src, cnt in fuentes.items():
            lines.append(f"  • {src}: {cnt}")

    # PRE score cache
    lines.append(f"\n⚡ PRE Score cache: {len(PRE_SCORE_CACHE)} entradas")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def cmd_miperfil(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = user.id
    nick = user_display(uid)
    admin = "👑 Admin" if is_admin(uid) else "👤 Usuario"

    bets = load_bets()
    mine = [b for b in bets if b.user_id == uid]
    wins = sum(1 for b in mine if b.result == "win")
    losses = sum(1 for b in mine if b.result == "loss")
    pending = sum(1 for b in mine if not b.result)

    await update.message.reply_text(
        f"👤 *Mi perfil*\n"
        f"{'─'*24}\n"
        f"ID: `{uid}`\n"
        f"Username: @{user.username or '—'}\n"
        f"Alias: *{nick}*\n"
        f"Rol: {admin}\n\n"
        f"📊 Apuestas: {wins}W / {losses}L / {pending} pendientes",
        parse_mode=ParseMode.MARKDOWN
    )

# =========================
# COMANDOS DE ADMIN DE USUARIOS
# =========================
async def cmd_adduser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ Solo admins")
        return

    args = context.args or []
    if not args:
        await update.message.reply_text("Uso: `/adduser USER_ID Nombre`")
        return

    try:
        target_id = int(args[0])
    except ValueError:
        await update.message.reply_text("El ID debe ser un número")
        return

    nickname = " ".join(args[1:]) if len(args) > 1 else ""
    add_user(target_id, nickname)

    await update.message.reply_text(f"✅ Usuario `{target_id}` añadido")

async def cmd_removeuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ Solo admins")
        return

    args = context.args or []
    if not args:
        await update.message.reply_text("Uso: `/removeuser USER_ID`")
        return

    try:
        target_id = int(args[0])
    except ValueError:
        await update.message.reply_text("El ID debe ser un número")
        return

    remove_user(target_id)
    await update.message.reply_text(f"✅ Usuario `{target_id}` eliminado")

async def cmd_usuarios(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ Solo admins")
        return

    users = load_users()
    lines = ["👥 *Usuarios autorizados:*"]

    for uid in users.get("allowed", []):
        nick = users["nicknames"].get(str(uid), "—")
        admin = "👑 " if uid in users.get("admins", []) else "• "
        lines.append(f"{admin}`{uid}` — {nick}")

    if ADMIN_ID and ADMIN_ID not in users.get("allowed", []):
        lines.append(f"👑 `{ADMIN_ID}` — Admin (env)")

    if len(lines) == 1:
        lines.append("_Sin usuarios registrados_")

    lines.append(f"\nTotal: {len(users.get('allowed', []))} usuarios")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

# =========================
# BACKGROUND JOBS
# =========================
async def background_scan(context: ContextTypes.DEFAULT_TYPE):
    """Escanea partidos en vivo y envía alertas"""
    chat_id = context.job.chat_id

    props_manual = await asyncio.to_thread(load_props)
    props_pm = PM_CACHE.get("props", [])
    if not props_pm:
        props_pm = await asyncio.to_thread(polymarket_props_today_from_scoreboard)

    props = props_manual + props_pm
    if not props:
        return

    state = await asyncio.to_thread(load_alert_state)

    # Indexar por PID
    by_pid = {}
    for p in props:
        pid = await asyncio.to_thread(get_pid_for_name, p.player)
        if pid:
            by_pid.setdefault(pid, []).append(p)

    try:
        games = await asyncio.to_thread(lambda: scoreboard.ScoreBoard().get_dict()["scoreboard"]["games"])
    except Exception:
        return

    for g in games:
        if g.get("gameStatus") != 2:  # Solo juegos en vivo
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
            box = await asyncio.to_thread(lambda: boxscore.BoxScore(gid).get_dict()["game"])
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
                    pre_val, meta = await asyncio.to_thread(pre_score, pid, pr.tipo, pr.line, pr.side)

                    if pr.side == "over":
                        faltante = pr.line - actual
                        lo, hi = THRESH_POINTS_OVER if pr.tipo == "puntos" else THRESH_REB_AST_OVER

                        if not (lo <= faltante <= hi):
                            continue
                        if should_gate_by_minutes("over", pr.tipo, faltante, mins, elapsed_min, is_blowout):
                            continue
                        if is_blowout and diff >= BLOWOUT_STRONG:
                            if (pr.tipo == "puntos" and faltante > 1.0) or faltante > 0.8:
                                continue

                        live = compute_over_score(
                            pr.tipo, faltante, mins, pf, period, clock_sec,
                            diff, is_clutch, is_blowout
                        )
                        final = int(clamp(0.55 * live + 0.45 * pre_val, 0, 100))
                        key = f"{gid}|{pid}|{pr.tipo}|{pr.side}|{pr.line}"

                        if final >= FINAL_ALERT_THRESHOLD or (is_clutch and final >= FINAL_ALERT_THRESHOLD_CLUTCH):
                            if can_send_alert(state, key):
                                msg = (
                                    f"🎯 *ALERTA OVER* | *FINAL* `{final}/100`\n"
                                    f"👤 *{name}*\n"
                                    f"📊 {pr.tipo.upper()} {actual:.0f}/{pr.line} (faltan {faltante:.1f})\n"
                                    f"⏱️ {status} | Q{period} {game_clock}\n"
                                    f"🧠 MIN {mins:.1f} | PF {pf} | Diff {diff}"
                                )
                                await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)

                    else:  # under
                        margin = pr.line - actual
                        if should_gate_by_minutes("under", pr.tipo, margin, mins, elapsed_min, is_blowout):
                            continue

                        live = compute_under_score(
                            pr.tipo, margin, mins, pf, period, clock_sec,
                            diff, is_clutch, is_blowout
                        )
                        final = int(clamp(0.65 * live + 0.35 * pre_val, 0, 100))
                        key = f"{gid}|{pid}|{pr.tipo}|{pr.side}|{pr.line}"

                        if final >= FINAL_ALERT_THRESHOLD or (is_blowout and final >= FINAL_ALERT_THRESHOLD_CLUTCH):
                            if can_send_alert(state, key):
                                msg = (
                                    f"🧊 *ALERTA UNDER* | *FINAL* `{final}/100`\n"
                                    f"👤 *{name}*\n"
                                    f"📊 {pr.tipo.upper()} {actual:.0f}/{pr.line} (colchón {margin:.1f})\n"
                                    f"⏱️ {status} | Q{period} {game_clock}\n"
                                    f"🧠 MIN {mins:.1f} | PF {pf} | Diff {diff}"
                                )
                                await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)

    await asyncio.to_thread(save_alert_state, state)

async def send_morning_digest(context: ContextTypes.DEFAULT_TYPE):
    """Envía resumen matutino"""
    chat_id = context.job.chat_id
    today = date.today().isoformat()

    state = await asyncio.to_thread(load_morning_state)
    if state.get("last_date") == today:
        return

    state["last_date"] = today
    await asyncio.to_thread(save_morning_state, state)

    try:
        games = await asyncio.to_thread(lambda: scoreboard.ScoreBoard().get_dict()["scoreboard"]["games"])
    except Exception:
        return

    if not games:
        return

    today_fmt = date.today().strftime("%A %d/%m/%Y").capitalize()
    header = f"🌅 *Buenos días NBA — {today_fmt}*"

    game_lines = []
    for g in games:
        away = g.get("awayTeam", {})
        home = g.get("homeTeam", {})
        at = away.get("teamTricode", "?")
        ht = home.get("teamTricode", "?")
        status = g.get("gameStatusText", "")
        game_lines.append(f"• {at} @ {ht} — {status}")

    msg = header + "\n" + "\n".join(game_lines)
    await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)

async def background_check_morning(context: ContextTypes.DEFAULT_TYPE):
    """Verifica si es hora del resumen matutino"""
    from datetime import datetime
    if datetime.now().hour == MORNING_HOUR:
        await send_morning_digest(context)

def can_send_alert(state: dict, key: str) -> bool:
    now = now_ts()
    last = int(state.get(key, 0))
    if now - last >= COOLDOWN_SECONDS:
        state[key] = now
        return True
    return False

# =========================
# REGISTRO DE JOBS AL INICIAR
# =========================
async def register_job(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Registra un chat para recibir jobs"""
    uid = update.effective_user.id
    uname = update.effective_user.first_name or str(uid)
    chat_id = update.effective_chat.id

    users = load_users()
    if not users["allowed"] and not users["admins"]:
        add_user(uid, uname, admin=True)
        await update.message.reply_text(
            f"👑 *Eres el primer usuario — eres Admin*\nTu ID: `{uid}`",
            parse_mode=ParseMode.MARKDOWN
        )
    elif not is_allowed(uid):
        await update.message.reply_text(
            f"🔒 *Acceso restringido*\nTu ID: `{uid}`",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    else:
        add_user(uid, uname)

    # Job 1: scan en vivo
    if not context.job_queue.get_jobs_by_name(f"scan:{chat_id}"):
        context.job_queue.run_repeating(
            background_scan, interval=POLL_SECONDS, first=10,
            chat_id=chat_id, name=f"scan:{chat_id}",
        )

    # Job 2: resumen matutino
    if not context.job_queue.get_jobs_by_name(f"morning:{chat_id}"):
        context.job_queue.run_repeating(
            background_check_morning, interval=3600, first=60,
            chat_id=chat_id, name=f"morning:{chat_id}",
        )

    await update.message.reply_text(
        f"✅ *¡Bienvenido, {uname}!*\n"
        f"Jobs activados.\n\n"
        f"Usa `/odds` para ver los partidos disponibles.",
        parse_mode=ParseMode.MARKDOWN
    )
    await cmd_help(update, context)

# =========================
# MAIN
# =========================
BOT_COMMANDS = [
    BotCommand("start", "Activar bot"),
    BotCommand("odds", "Props por partido"),
    BotCommand("games", "Partidos hoy"),
    BotCommand("live", "Props en vivo"),
    BotCommand("lineup", "Alineaciones"),
    BotCommand("analisis", "Análisis de jugador"),
    BotCommand("contexto", "Contexto defensivo"),
    BotCommand("bet", "Registrar apuesta"),
    BotCommand("misapuestas", "Ver pendientes"),
    BotCommand("historial", "Estadísticas"),
    BotCommand("resultado", "Cerrar apuesta"),
    BotCommand("miperfil", "Ver perfil"),
    BotCommand("help", "Ayuda"),
]

async def on_startup(app: Application):
    try:
        await app.bot.set_my_commands(BOT_COMMANDS)
        log.info("Comandos registrados en Telegram ✅")
    except Exception as e:
        log.warning(f"Error registrando comandos: {e}")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    def guarded(fn):
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if not await guard(update):
                return
            return await fn(update, context)
        wrapper.__name__ = fn.__name__
        return wrapper

    # Handlers
    app.add_handler(CommandHandler("start", register_job))
    app.add_handler(CommandHandler("help", guarded(cmd_help)))
    app.add_handler(CommandHandler("games", guarded(cmd_games)))
    app.add_handler(CommandHandler("odds", guarded(cmd_odds)))
    app.add_handler(CommandHandler("live", guarded(cmd_live)))
    app.add_handler(CommandHandler("lineup", guarded(cmd_lineup)))  # ¡AHORA SÍ DEFINIDO!
    app.add_handler(CommandHandler("analisis", guarded(cmd_analisis)))
    app.add_handler(CommandHandler("contexto", guarded(cmd_contexto)))
    app.add_handler(CommandHandler("bet", guarded(cmd_bet)))
    app.add_handler(CommandHandler("misapuestas", guarded(cmd_misapuestas)))
    app.add_handler(CommandHandler("historial", guarded(cmd_historial)))
    app.add_handler(CommandHandler("resultado", guarded(cmd_resultado)))
    app.add_handler(CommandHandler("miperfil", guarded(cmd_miperfil)))
    app.add_handler(CommandHandler("debug", guarded(cmd_debug)))

    # Admin commands
    app.add_handler(CommandHandler("adduser", cmd_adduser))
    app.add_handler(CommandHandler("removeuser", cmd_removeuser))
    app.add_handler(CommandHandler("usuarios", cmd_usuarios))

    # Message handler para selección de partidos
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_game_selection))

    app.post_init = on_startup
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
