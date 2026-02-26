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

def load_alert_state() -> dict: return load_json(ALERTS_STATE_FILE, {})
def save_alert_state(state: dict) -> None: save_json(ALERTS_STATE_FILE, state)
def load_smart_alerts_state() -> dict: return load_json(SMART_ALERTS_FILE, {})
def save_smart_alerts_state(state: dict) -> None: save_json(SMART_ALERTS_FILE, state)
def load_morning_state() -> dict: return load_json(MORNING_DIGEST_FILE, {})
def save_morning_state(state: dict) -> None: save_json(MORNING_DIGEST_FILE, state)
def load_users() -> dict: return load_json(USERS_FILE, {"allowed": [], "admins": [], "nicknames": {}})
def save_users(data: dict) -> None: save_json(USERS_FILE, data)

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
    if user_id in users["allowed"]: users["allowed"].remove(user_id)
    if user_id in users["admins"]: users["admins"].remove(user_id)
    users["nicknames"].pop(str(user_id), None)
    save_users(users)
    return True

def user_display(user_id: int) -> str:
    users = load_users()
    return users["nicknames"].get(str(user_id), f"#{user_id}")

async def guard(update: Update) -> bool:
    user = update.effective_user
    if not user: return False
    if is_allowed(user.id): return True
    await update.message.reply_text(
        f"🔒 *Acceso restringido*\n\nTu ID es: `{user.id}`\n"
        f"Pide al admin que ejecute: `/adduser {user.id} {user.first_name}`",
        parse_mode=ParseMode.MARKDOWN
    )
    log.info(f"Acceso denegado a {user.first_name} (id={user.id})")
    return False

# =========================
# CACHE DE JUGADORES Y GAMELOG
# =========================
def obtener_id_jugador(nombre: str) -> Optional[int]:
    time.sleep(0.2 + random.random() * 0.1)
    res = players.find_players_by_full_name(nombre)
    if not res: return None
    exact = [p for p in res if (p.get("full_name") or "").lower() == nombre.lower()]
    return int((exact[0] if exact else res[0]).get("id"))

def get_pid_for_name(name: str) -> Optional[int]:
    cache = load_json(IDS_CACHE_FILE, {})
    if name in cache: return int(cache[name])
    pid = obtener_id_jugador(name)
    if pid:
        cache[name] = pid
        save_json(IDS_CACHE_FILE, cache)
    return pid

GLOG_TTL_SECONDS = 6 * 60 * 60

def get_gamelog_table(pid: int) -> Tuple[List[str], List[list]]:
    cache = load_json(GLOG_CACHE_FILE, {})
    k = str(pid); now = now_ts()
    if k in cache and (now - int(cache[k].get("ts", 0))) < GLOG_TTL_SECONDS:
        return cache[k].get("headers", []), cache[k].get("rows", [])

    time.sleep(0.5 + random.random() * 0.25)
    url = "https://stats.nba.com/stats/playergamelog"
    params = {"DateFrom": "", "DateTo": "", "LeagueID": "00", "PlayerID": str(pid), "Season": SEASON, "SeasonType": "Regular Season"}

    try:
        resp = SESSION_NBA.get(url, params=params, timeout=(12, 30))
        if resp.status_code != 200: return cache.get(k, {}).get("headers", []), cache.get(k, {}).get("rows", [])
        rs = resp.json().get("resultSets") or []
        if not rs: rs = [resp.json().get("resultSet")] if resp.json().get("resultSet") else []
        hdrs = rs[0].get("headers", []) if rs else []
        rows = rs[0].get("rowSet", []) if rs else []
        cache[k] = {"ts": now, "headers": hdrs, "rows": rows}
        save_json(GLOG_CACHE_FILE, cache)
        return hdrs, rows
    except Exception as e:
        log.warning(f"Error en get_gamelog_table: {e}")
        return cache.get(k, {}).get("headers", []), cache.get(k, {}).get("rows", [])

# =========================
# FUNCIONES ESTADÍSTICAS BASE
# =========================
def clamp(x: float, lo: float = 0, hi: float = 100) -> float: return max(lo, min(hi, x))

def stdev(vals: List[float]) -> float:
    if not vals or len(vals) < 2: return 0.0
    mu = sum(vals) / len(vals)
    return math.sqrt(sum((x - mu) ** 2 for x in vals) / (len(vals) - 1))

def last_n_values(pid: int, tipo: str, n: int = 10) -> List[float]:
    hdrs, rows = get_gamelog_table(pid)
    if not hdrs or not rows: return []
    try: idx = hdrs.index(STAT_COL.get(tipo))
    except ValueError: return []
    vals = []
    for r in rows[:n]:
        if idx < len(r):
            try: vals.append(float(r[idx]))
            except Exception: pass
    return vals

def hit_counts(values: List[float], line: float, side: str) -> Tuple[int, int]:
    if not values: return 0, 0
    hits = sum(1 for v in values if (v > line if side == "over" else v < line))
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

    w_margin = max(0.0, (0.65 * (sum(m10) / len(m10) if m10 else 0.0)) + (0.35 * (sum(m5) / len(m5) if m5 else 0.0)))
    HitScore = 100.0 * (0.65 * hit10 + 0.35 * hit5)
    MarginScore = clamp((w_margin / MARGIN_CAP.get(tipo, 3.0)) * 100.0, 0, 100)

    std10 = stdev(v10)
    ConsistencyScore = 100.0 - clamp((std10 / STD_CAP.get(tipo, 4.0)) * 60.0, 0, 60)
    PRE = int(clamp(0.55 * HitScore + 0.25 * MarginScore + 0.20 * ConsistencyScore, 0, 100))

    return PRE, {
        "hit5": round(hit5, 2), "hit10": round(hit10, 2), "hits5": h5, "n5": n5, "hits10": h10, "n10": n10,
        "avg5": round(sum(v5)/len(v5), 2) if v5 else 0.0, "avg10": round(sum(v10)/len(v10), 2) if v10 else 0.0,
        "std10": round(std10, 2), "w_margin": round(w_margin, 2), "vals": v10
    }

# =========================
# CONTEXTO DEFENSIVO Y SPLITS
# =========================
CONTEXT_CACHE: Dict[str, dict] = {}
CONTEXT_TTL = 4 * 60 * 60
_TRICODE_TO_TEAM_ID_CACHE: Dict[str, int] = {}

def get_team_id_cached(tricode: str) -> Optional[int]:
    if tricode in _TRICODE_TO_TEAM_ID_CACHE: return _TRICODE_TO_TEAM_ID_CACHE[tricode]
    for t in nba_teams_static.get_teams():
        if t.get("abbreviation", "").upper() == tricode.upper():
            _TRICODE_TO_TEAM_ID_CACHE[tricode] = int(t["id"])
            return int(t["id"])
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
        if sk and sk in ts_opp[tid]:
            res["opp_stat"] = ts_opp[tid][sk]
            all_opp = sorted(ts_opp.values(), key=lambda x: x.get(sk, 0), reverse=True)
            res["opp_stat_rank"] = next((i+1 for i, s in enumerate(all_opp) if s.get(sk) == ts_opp[tid][sk]), None)

    vd = []
    if res["def_rank"]: vd.append("defensa débil" if res["def_rank"] >= 25 else ("defensa élite" if res["def_rank"] <= 5 else ""))
    if res["pace_rank"]: vd.append("ritmo alto" if res["pace_rank"] <= 5 else ("ritmo lento" if res["pace_rank"] >= 25 else ""))
    res["verdict"] = " · ".join(filter(bool, vd)) or "contexto neutro"
    return res

def home_away_splits(pid: int, tipo: str) -> dict:
    hdrs, rows = get_gamelog_table(pid)
    try: idx = hdrs.index(STAT_COL.get(tipo)); m_idx = hdrs.index("MATCHUP")
    except: return {}
    hv, av = [], []
    for r in rows:
        if m_idx >= len(r) or idx >= len(r): continue
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

def is_back_to_back(pid: int) -> bool:
    hdrs, rows = get_gamelog_table(pid)
    try: date_idx = hdrs.index("GAME_DATE")
    except: return False
    try:
        last_date = datetime.strptime(str(rows[0][date_idx]), "%b %d, %Y").date()
        return last_date == date.today() - timedelta(days=1)
    except: return False

# =========================
# PRE SCORE V2 (CON CONTEXTO)
# =========================
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
# FUNCIONES PARA TIEMPO DE JUEGO
# =========================
def parse_minutes(min_str: str) -> float:
    if not min_str: return 0.0
    try:
        if ":" in min_str:
            mm, ss = min_str.split(":")
            return float(mm) + float(ss) / 60.0
    except: pass
    return 0.0

def clock_to_seconds(game_clock: str) -> Optional[int]:
    if not game_clock: return None
    gc = str(game_clock)
    if gc.startswith("PT") and "M" in gc:
        try: return int(gc.split("PT")[1].split("M")[0]) * 60 + int(gc.split("M")[1].replace("S", "").split(".")[0])
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

def should_gate_by_minutes(side: str, tipo: str, value: float, mins: float, elapsed_min: Optional[float], is_blowout: bool) -> bool:
    if side == "over":
        if elapsed_min is not None and elapsed_min >= 18: return False
        return mins < (MIN_MINUTES_POINTS if tipo == "puntos" else MIN_MINUTES_REB_AST)
    else:
        if elapsed_min is None: return True
        if is_blowout and elapsed_min >= 16: return False
        return elapsed_min < 22

def compute_over_score(tipo: str, faltante: float, mins: float, pf: int, period: int, clock_seconds: Optional[int], diff: int, is_clutch: bool, is_blowout: bool) -> int:
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

def compute_under_score(tipo: str, margin_under: float, mins: float, pf: int, period: int, clock_seconds: Optional[int], diff: int, is_clutch: bool, is_blowout: bool) -> int:
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
# POLYMARKET FETCHER (AMPLIO Y ROBUSTO)
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
    
    # 1. Obtener TODOS los eventos NBA activos de una vez
    all_events = []
    try:
        r = SESSION_PM.get(f"{GAMMA}/events", params={"tag_slug": "nba", "closed": "false", "limit": 100}, timeout=15)
        if r.status_code == 200:
            data = r.json()
            all_events = data if isinstance(data, list) else data.get("events", [])
    except Exception as e:
        log.warning(f"Error fetching Polymarket /events: {e}")

    # 2. Emparejar cada juego de la NBA con los eventos descargados
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
                        props_all.extend([
                            Prop(player, tipo, line_val, "over", "polymarket", local_slug, str(m.get("id"))),
                            Prop(player, tipo, line_val, "under", "polymarket", local_slug, str(m.get("id")))
                        ])
            except: pass

    # 3. Deduplicar
    seen = set()
    uniq = []
    for p in props_all:
        k = (p.game_slug, p.player.lower(), p.tipo, p.line, p.side)
        if k not in seen:
            seen.add(k)
            uniq.append(p)

    # 4. Fallback de emergencia
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
    return uniq

# =========================
# UTILIDADES DE UI
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

async def _send_long_message(update: Update, text: str, max_len: int = 3800) -> None:
    if len(text) <= max_len:
        try: await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        except: await update.message.reply_text(text.replace("*", "").replace("_", "").replace("`", ""))
        return

    parts = []
    remaining = text

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
        except: await update.message.reply_text((prefix + part).replace("*", "").replace("_", "").replace("`", ""))
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
    "• `/contexto AWAY HOME` → contexto defensivo\n"
    "• `/alertas` → top props pre-partido\n\n"
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
        games = await asyncio.wait_for(asyncio.to_thread(lambda: scoreboard.ScoreBoard().get_dict()["scoreboard"]["games"]), timeout=20.0)
    except: return await update.message.reply_text("⚠️ Error leyendo scoreboard de la NBA")
    if not games: return await update.message.reply_text("No hay juegos hoy")

    lines = ["📅 *NBA hoy*"]
    for g in games:
        at = g.get("awayTeam", {}).get("teamTricode", "?")
        ht = g.get("homeTeam", {}).get("teamTricode", "?")
        lines.append(f"• {at} @ {ht} — {g.get('gameStatusText', '')}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

# =========================
# COMANDO ODDS INTERACTIVO
# =========================
async def cmd_odds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ *Cargando partidos de hoy...*", parse_mode=ParseMode.MARKDOWN)
    props_pm = await asyncio.to_thread(polymarket_props_today_from_scoreboard)

    if not props_pm: return await msg.edit_text("❌ No pude obtener props. Usa `/debug`.")

    games_dict: Dict[str, List[Prop]] = {}
    for p in props_pm:
        slug = p.game_slug or "unknown"
        games_dict.setdefault(slug, []).append(p)

    args = context.args or []
    if args:
        slug_filter = " ".join(args).strip().lower()
        if slug_filter in games_dict:
            await msg.delete()
            return await show_game_props_advanced(update, context, slug_filter, games_dict[slug_filter])
        for slug in games_dict.keys():
            if slug_filter.upper() in _slug_to_matchup(slug):
                await msg.delete()
                return await show_game_props_advanced(update, context, slug, games_dict[slug])
        return await msg.edit_text(f"❌ No encontré el partido `{slug_filter}`")

    today_str = date.today().strftime("%d/%m/%Y")
    header = f"📋 *NBA Props — {today_str}*\n🎮 *Selecciona un partido:*\n{'─'*30}\n"
    game_lines = []
    for i, (slug, props) in enumerate(games_dict.items(), 1):
        matchup = _slug_to_matchup(slug)
        players = set(p.player for p in props)
        game_lines.append(f"{i}. *{matchup}*\n   `{slug}`\n   👤 {len(players)} jug | 📊 {len(props)//2} líneas\n")

    footer = f"{'─'*30}\nResponde con el número del partido (1,2,3...)\nO usa `/odds BOS` para buscar por equipo"
    context.user_data['games_menu'] = list(games_dict.keys())
    await msg.edit_text(header + "\n".join(game_lines) + footer, parse_mode=ParseMode.MARKDOWN)

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
        else: await update.message.reply_text(f"❌ Error cargando props")
    else: await update.message.reply_text(f"❌ Número inválido")

async def show_game_props_advanced(update: Update, context: ContextTypes.DEFAULT_TYPE, slug: str, props: List[Prop]) -> None:
    matchup = _slug_to_matchup(slug)
    msg = await update.message.reply_text(f"⚡ *Calculando scores para {matchup}...*", parse_mode=ParseMode.MARKDOWN)

    parts_slug = slug.replace("nba-", "").split("-")
    away_tri = parts_slug[0].upper() if len(parts_slug) >= 2 else "???"
    home_tri = parts_slug[1].upper() if len(parts_slug) >= 2 else "???"

    unique_lines: Dict[str, List[Tuple[str, float]]] = {}
    seen_lines = set()
    for p in props:
        if p.side != "over": continue
        if (p.player, p.tipo, p.line) in seen_lines: continue
        seen_lines.add((p.player, p.tipo, p.line))
        unique_lines.setdefault(p.player, []).append((p.tipo, p.line))

    def _calc_player(player: str, lines: List[Tuple[str, float]]) -> Tuple[str, List[dict]]:
        pid = get_pid_for_name(player)
        if not pid: return player, []

        opp_tricode = home_tri
        is_home = False
        try:
            for team_tri, team_id in [(away_tri, get_team_id_cached(away_tri)), (home_tri, get_team_id_cached(home_tri))]:
                if team_id:
                    roster = commonteamroster.CommonTeamRoster(team_id=team_id, season=SEASON).get_data_frames()[0]
                    if pid in roster['PLAYER_ID'].values:
                        opp_tricode = home_tri if team_tri == away_tri else away_tri
                        is_home = (team_tri == home_tri)
                        break
        except: pass

        rest = 0 if is_back_to_back(pid) else 1
        results = []
        for tipo, line in lines:
            po, meta_o = pre_score_v2(pid, tipo, line, "over", opp_tricode, is_home, rest)
            pu, _ = pre_score_v2(pid, tipo, line, "under", opp_tricode, is_home, rest)
            results.append({"tipo": tipo, "line": line, "po": po, "pu": pu, "meta": meta_o})
        return player, results

    sem = asyncio.Semaphore(3)
    async def _safe_calc(player, lines):
        async with sem: return await asyncio.wait_for(asyncio.to_thread(_calc_player, player, lines), timeout=30.0)

    tasks = [_safe_calc(pl, ln) for pl, ln in unique_lines.items()]
    try: results = await asyncio.gather(*tasks, return_exceptions=True)
    except Exception as e: return await msg.edit_text(f"❌ Error global: {e}")

    players_data = {}
    for item in results:
        if isinstance(item, Exception):
            log.warning(f"Error en jugador: {item}")
            continue
        pl, res = item
        if res: players_data[pl] = res

    if not players_data: return await msg.edit_text("❌ No se pudieron calcular scores. API de la NBA puede estar lenta.")

    tipo_order = {"puntos": 0, "rebotes": 1, "asistencias": 2}
    players_sorted = sorted(players_data.keys(), key=lambda pl: max(max(e["po"], e["pu"]) for e in players_data[pl]), reverse=True)

    lines = [f"🟣 *{matchup}*\n`{slug}`\n{'─'*28}"]
    for pl in players_sorted:
        lines.append(f"\n👤 *{pl}*")
        for e in sorted(players_data[pl], key=lambda e: tipo_order.get(e["tipo"], 9)):
            tipo, po, pu, meta = e["tipo"], e["po"], e["pu"], e["meta"]
            avg_str = f"prom10: *{meta.get('avg10'):.1f}*" if meta.get("avg10") is not None else ""
            adj_str = f"  _(adj: {', '.join(meta.get('v2_adjustments', []))[:30]})_\n" if meta.get("v2_adjustments") else ""
            
            ctx_parts = []
            if meta.get("ctx_def_rank"): ctx_parts.append(f"Def#{meta['ctx_def_rank']}")
            if meta.get("ctx_pace_rank"): ctx_parts.append(f"Pace#{meta['ctx_pace_rank']}")
            ctx_line = f"  🛡️ `{' · '.join(ctx_parts)}`\n" if ctx_parts else ""

            lines.append(
                f"{TIPO_ICON.get(tipo, '•')} *{tipo.upper()}* — `{e['line']}`\n"
                f"  OVER  {_pre_rating_emoji(po)} `{po:>3}/100` {_pre_bar(po)}\n"
                f"  UNDER {_pre_rating_emoji(pu)} `{pu:>3}/100` {_pre_bar(pu)}\n"
                f"{adj_str}{ctx_line}  📊 `{meta.get('hits5','?')}/{meta.get('n5','?')}` últ5 | `{meta.get('hits10','?')}/{meta.get('n10','?')}` últ10  {avg_str}"
            )

    await msg.delete()
    await _send_long_message(update, "\n".join(lines))

# =========================
# COMANDO LIVE OPTIMIZADO
# =========================
async def cmd_live(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ *Cargando datos en vivo...*", parse_mode=ParseMode.MARKDOWN)

    try:
        games = await asyncio.wait_for(asyncio.to_thread(lambda: scoreboard.ScoreBoard().get_dict()["scoreboard"]["games"]), timeout=20.0)
    except Exception: return await msg.edit_text("⚠️ Error leyendo scoreboard de la NBA.")

    live_games = [g for g in games if g.get("gameStatus") == 2]
    if not live_games: return await msg.edit_text("⏸️ No hay partidos en vivo ahora mismo.")

    await msg.edit_text(f"🔄 *{len(live_games)} partido(s) en vivo* — calculando...", parse_mode=ParseMode.MARKDOWN)

    props_pm = PM_CACHE.get("props", [])
    if not props_pm: props_pm = await asyncio.to_thread(polymarket_props_today_from_scoreboard)

    all_props = await asyncio.to_thread(load_props) + props_pm
    pbn = {}
    for p in all_props: pbn.setdefault(p.player.lower(), []).append(p)

    async def fetch_box(gid: str):
        try: return gid, await asyncio.wait_for(asyncio.to_thread(lambda: boxscore.BoxScore(gid).get_dict()["game"]), timeout=15.0)
        except: return gid, None

    box_results = await asyncio.gather(*[fetch_box(g["gameId"]) for g in live_games])
    scored_rows = []

    for g, (gid, box) in zip(live_games, box_results):
        if not box: continue

        try:
            sc_home, sc_away = g.get("homeTeam", {}).get("score"), g.get("awayTeam", {}).get("score")
            diff = abs(int(sc_home if sc_home else 0) - int(sc_away if sc_away else 0))
        except: diff = 0

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
                        if pl.get("familyName").lower() in k: m = lst; break
                if not m: continue

                s = pl.get("statistics", {})
                pts, reb, ast = float(s.get("points") or 0), float(s.get("reboundsTotal") or 0), float(s.get("assists") or 0)
                pf, mins = float(s.get("foulsPersonal") or 0), parse_minutes(s.get("minutes", ""))
                pid = pl.get("personId")

                for pr in m:
                    try:
                        act = pts if pr.tipo == "puntos" else (reb if pr.tipo == "rebotes" else ast)
                        if pr.side == "over":
                            delta = float(pr.line) - act
                            if 0.5 <= delta <= 4.0 and not should_gate_by_minutes("over", pr.tipo, delta, mins, elapsed_min, diff>=20):
                                pv, meta = await asyncio.to_thread(pre_score, pid, pr.tipo, pr.line, pr.side)
                                live_sc = compute_over_score(pr.tipo, delta, mins, pf, q, clock_sec, diff, diff<=8, diff>=20)
                                f = int(clamp(0.55 * live_sc + 0.45 * pv, 0, 100))
                                scored_rows.append((f, pr, act, delta, q, clk, diff, meta, mins))
                        else:
                            delta = float(pr.line) - act
                            if delta >= 2.0 and not should_gate_by_minutes("under", pr.tipo, delta, mins, elapsed_min, diff>=20):
                                pv, meta = await asyncio.to_thread(pre_score, pid, pr.tipo, pr.line, pr.side)
                                live_sc = compute_under_score(pr.tipo, delta, mins, pf, q, clock_sec, diff, diff<=8, diff>=20)
                                f = int(clamp(0.65 * live_sc + 0.35 * pv, 0, 100))
                                scored_rows.append((f, pr, act, delta, q, clk, diff, meta, mins))
                    except Exception as sub_e: log.warning(f"Error procesando prop de {pr.player}: {sub_e}")

    await msg.delete()
    if not scored_rows: return await update.message.reply_text("📭 *Sin señal en vivo ahora*\nNinguna prop está lo suficientemente cerca de su línea.", parse_mode=ParseMode.MARKDOWN)

    scored_rows.sort(key=lambda x: x[0], reverse=True)
    out = [f"🔥 *LIVE — {len(live_games)} partido(s)*\n{'─'*28}"]

    for (f, pr, act, d, q, clk, df, m, mns) in scored_rows[:15]:
        lbl = "faltan" if pr.side == "over" else "colchón"
        out.append(f"\n{_pre_rating_emoji(f)} `{f}/100` — *{pr.player}*\n"
                   f"{TIPO_ICON.get(pr.tipo, '•')} {pr.tipo.upper()} {pr.side.upper()} `{pr.line}` | actual `{act:.0f}` ({lbl} `{d:.1f}`)\n"
                   f"⏱️ Q{q} {clk} | MIN `{mns:.0f}` Dif `{df}` | H5: `{m.get('hits5','?')}`")

    await _send_long_message(update, "\n".join(out))

# =========================
# COMANDO ANÁLISIS
# =========================
async def cmd_analisis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    body = re.sub(r"^/analisis(@\w+)?\s*", "", (update.message.text or "")).strip()
    if "|" not in body: return await update.message.reply_text("Formato: `/analisis Nombre | tipo | side | linea`", parse_mode=ParseMode.MARKDOWN)
    
    parts = [x.strip() for x in body.split("|")]
    if len(parts) != 4: return await update.message.reply_text("Necesito 4 campos separados por `|`")

    pl_name, tipo, side = parts[0], parts[1].lower(), parts[2].lower()
    try: line = float(parts[3])
    except: return await update.message.reply_text("La línea debe ser un número")

    msg = await update.message.reply_text(f"🔍 Analizando *{pl_name}*...", parse_mode=ParseMode.MARKDOWN)

    def _run():
        pid = get_pid_for_name(pl_name)
        if not pid: return None, None, None, None, None
        po, pu, meta = pre_score_cached(pid, tipo, line)
        pre = po if side == "over" else pu
        opp_tricode, is_home = "???", True

        for p in PM_CACHE.get("props", []):
            if p.player.lower() == pl_name.lower() and p.game_slug:
                slug_parts = (p.game_slug or "").replace("nba-", "").split("-")
                if len(slug_parts) >= 2:
                    opp_tricode = slug_parts[1].upper()
                    is_home = False
                break
        return pid, pre, meta, opp_tricode, is_home

    pid, pre, meta, opp_tricode, is_home = await asyncio.to_thread(_run)
    if not pid: return await msg.edit_text(f"⚠️ No encontré al jugador: *{pl_name}*")

    v10 = meta.get("vals", [])
    avg10, avg5 = meta.get("avg10", 0), meta.get("avg5", 0)
    hits, total = hit_counts(v10, line, side)
    racha = f"{hits}/{total} cumplidos" if total else "sin datos"

    split_str = ""
    splits = home_away_splits(pid, tipo)
    if splits:
        loc = "local" if is_home else "visitante"
        loc_avg = splits.get(f"{'home' if is_home else 'away'}_avg")
        opp_avg = splits.get(f"{'away' if is_home else 'home'}_avg")
        if loc_avg: split_str = f"\n   • Promedio como {loc}: `{loc_avg}`"
        if opp_avg: split_str += f"\n   • Promedio como {'visitante' if is_home else 'local'}: `{opp_avg}`"

    ctx = get_defensive_context(opp_tricode, tipo)
    ctx_str = ""
    if ctx.get("def_rating"):
        ctx_str = f"\n   • Def Rating rival: `{ctx['def_rating']:.1f}` (rank #{ctx.get('def_rank','?')})"
        if ctx.get("opp_stat"): ctx_str += f"\n   • {tipo.capitalize()} permitidos: `{ctx['opp_stat']:.1f}` (rank #{ctx.get('opp_stat_rank','?')})"

    analysis = (f"🔬 *ANÁLISIS DE {pl_name}*\n{'─'*30}\n📊 *Estadísticas recientes*\n"
                f"   • Promedio últ.5: `{avg5}`\n   • Promedio últ.10: `{avg10}`\n   • Racha: {racha}\n{split_str}\n\n"
                f"🛡️ *Contexto vs {opp_tricode}*\n{ctx_str}\n\n📈 *PRE Score*\n"
                f"   {_pre_rating_emoji(pre)} `{pre}/100` {_pre_bar(pre)} _{_pre_label(pre)}_\n   Ajustes: {', '.join(meta.get('v2_adjustments', ['ninguno']))}")

    await msg.edit_text(analysis, parse_mode=ParseMode.MARKDOWN)

# =========================
# COMANDO CONTEXTO
# =========================
async def cmd_contexto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if len(args) < 2: return await update.message.reply_text("Uso: `/contexto AWAY HOME`\nEj: `/contexto BOS DEN`", parse_mode=ParseMode.MARKDOWN)
    aw, hm = args[0].upper(), args[1].upper()
    msg = await update.message.reply_text(f"⏳ Cargando contexto *{aw} @ {hm}*...", parse_mode=ParseMode.MARKDOWN)

    def _fetch():
        fetch_league_team_stats()
        fetch_opp_position_stats()
        return (
            {"pts": get_defensive_context(hm, "puntos"), "reb": get_defensive_context(hm, "rebotes"), "ast": get_defensive_context(hm, "asistencias")},
            {"pts": get_defensive_context(aw, "puntos"), "reb": get_defensive_context(aw, "rebotes"), "ast": get_defensive_context(aw, "asistencias")}
        )

    aw_ctx, hm_ctx = await asyncio.to_thread(_fetch)

    def _fmt(tri: str, ctx_dict: dict, label: str) -> str:
        lines = [f"*{label} — {tri}*"]
        for tipo, key in [("Puntos", "pts"), ("Rebotes", "reb"), ("Asistencias", "ast")]:
            ctx = ctx_dict[key]
            if ctx.get("def_rating"):
                lines.append(f"  • {tipo}: {ctx.get('opp_stat', 0):.1f}/j (rank #{ctx.get('opp_stat_rank','?')})\n"
                             f"    Def Rating {ctx['def_rating']:.1f} (#{ctx.get('def_rank','?')}) · Pace {ctx['pace']:.1f} (#{ctx.get('pace_rank','?')})")
            else: lines.append(f"  • {tipo}: sin datos")
        return "\n".join(lines)

    await msg.edit_text(f"🛡️ *CONTEXTO: {aw} @ {hm}*\n{'─'*30}\n\n{_fmt(hm, aw_ctx, 'Defensa del rival de los visitantes')}\n\n{'─'*30}\n\n{_fmt(aw, hm_ctx, 'Defensa del rival de los locales')}\n\n_Rank #1 = mejor defensa / permite menos_", parse_mode=ParseMode.MARKDOWN)

# =========================
# COMANDO LINEUP
# =========================
async def cmd_lineup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Obteniendo alineaciones...", parse_mode=ParseMode.MARKDOWN)
    try: games = await asyncio.wait_for(asyncio.to_thread(lambda: scoreboard.ScoreBoard().get_dict()["scoreboard"]["games"]), timeout=20.0)
    except: return await msg.edit_text("⚠️ Error leyendo scoreboard")

    ft = " ".join(context.args or []).strip().upper() if context.args else None
    await msg.edit_text("🔄 Cargando datos...", parse_mode=ParseMode.MARKDOWN)

    def _get_bx(gid):
        r = {}
        try:
            bx = boxscore.BoxScore(gid).get_dict().get("game", {})
            for t_k in ["homeTeam", "awayTeam"]:
                tri = bx.get(t_k, {}).get("teamTricode", "")
                if tri:
                    r[tri] = [{"name": f"{p.get('firstName','')} {p.get('familyName','')}".strip(), "status": p.get("status","Active"), "starter": p.get("starter","0")=="1", "pos": p.get("position",""), "not_playing_reason": p.get("notPlayingReason","") or p.get("inactiveReason","")} for p in bx.get(t_k, {}).get("players", [])]
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
            if st: r.append("  5️⃣ Titulares:\n    " + "\n    ".join(f"• {p['name']} [{p['pos']}]" for p in st[:5]))
            if out: r.append("  🔴 Bajas:\n    " + "\n    ".join(f"• {p['name']} _{p['not_playing_reason'][:30]}_" for p in out))
            return "\n".join(r) if pl else f"*{tr}*\n  _(sin datos)_"

        await update.message.reply_text(f"{hdr}\n\n{_f(aw, ap)}\n\n{_f(hm, hp)}", parse_mode=ParseMode.MARKDOWN)
        snt = True
        await asyncio.sleep(0.5)

    await msg.delete()
    if not snt: await update.message.reply_text("No encontré datos para ese equipo.")

# =========================
# COMANDOS DE APUESTAS
# =========================
def _parse_bet_command(text: str) -> Optional[dict]:
    body = re.sub(r"^/bet(@\w+)?\s*", "", text).strip()
    parts = [x.strip() for x in body.split("|")]
    if len(parts) < 4: return None
    player, tipo, side, line_s = parts[0], parts[1].lower(), parts[2].lower(), parts[3]
    amount_s = parts[4] if len(parts) >= 5 else "1"
    if tipo not in ("puntos", "rebotes", "asistencias"): return None
    if side not in ("over", "under"): return None
    try: return {"player": player, "tipo": tipo, "side": side, "line": float(line_s), "amount": float(amount_s)}
    except: return None

async def cmd_bet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parsed = _parse_bet_command(update.message.text or "")
    if not parsed: return await update.message.reply_text("Formato: `/bet Jugador | tipo | side | linea | monto`", parse_mode=ParseMode.MARKDOWN)

    msg = await update.message.reply_text("⏳ Registrando apuesta...", parse_mode=ParseMode.MARKDOWN)
    
    def _calc():
        pid = get_pid_for_name(parsed["player"])
        if not pid: return None, 0, ""
        po, pu, _ = pre_score_cached(pid, parsed["tipo"], parsed["line"])
        return pid, po if parsed["side"] == "over" else pu, next((p.game_slug for p in PM_CACHE.get("props", []) if p.player.lower() == parsed["player"].lower()), "")

    pid, pre, slug = await asyncio.to_thread(_calc)
    if not pid: return await msg.edit_text(f"⚠️ Jugador no encontrado: *{parsed['player']}*")

    bet = Bet(_new_bet_id(), update.effective_user.id, parsed["player"], parsed["tipo"], parsed["side"], parsed["line"], parsed["amount"], pre, slug, now_ts())
    bets = load_bets(); bets.append(bet); save_bets(bets)

    await msg.edit_text(f"✅ *Apuesta registrada* `#{bet.id}`\n{'─'*24}\n👤 *{bet.player}*\n{TIPO_ICON.get(bet.tipo, '•')} {bet.tipo.upper()} {bet.side.upper()} `{bet.line}`\n💰 Monto: `{bet.amount}` unidades\n{_pre_rating_emoji(pre)} PRE Score: `{pre}/100`\n_Usa `/resultado {bet.id} WIN` al terminar_", parse_mode=ParseMode.MARKDOWN)

async def cmd_misapuestas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = [b for b in load_bets() if b.user_id == update.effective_user.id and not b.result]
    if not pending: return await update.message.reply_text("No tienes apuestas pendientes.")
    lines = [f"⏳ *Apuestas pendientes* ({len(pending)})"]
    for b in sorted(pending, key=lambda x: x.placed_at, reverse=True):
        lines.append(f"\n`#{b.id}` {TIPO_ICON.get(b.tipo, '•')} *{b.player}*\n  {b.tipo.upper()} {b.side.upper()} `{b.line}` — `{b.amount}`u\n  {_pre_rating_emoji(b.pre_score)} PRE `{b.pre_score}/100`")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def cmd_resultado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if len(args) < 2: return await update.message.reply_text("Formato: `/resultado ID WIN|LOSS|PUSH`", parse_mode=ParseMode.MARKDOWN)

    bet_id, result = args[0].upper(), args[1].upper()
    if result not in ("WIN", "LOSS", "PUSH"): return await update.message.reply_text("Resultado debe ser WIN, LOSS o PUSH")

    bets = load_bets()
    found = next((b for b in bets if b.id == bet_id), None)
    if not found: return await update.message.reply_text(f"No encontré la apuesta `{bet_id}`")

    found.result, found.resolved_at = result.lower(), now_ts()
    save_bets(bets)
    await update.message.reply_text(f"{ {'win': '✅', 'loss': '❌', 'push': '🔁'}.get(result.lower()) } Apuesta `#{bet_id}` → *{result}*", parse_mode=ParseMode.MARKDOWN)

async def cmd_historial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    days = int(context.args[0]) if context.args and context.args[0].isdigit() else 30
    cutoff = now_ts() - days * 86400

    mine = [b for b in load_bets() if b.user_id == update.effective_user.id and b.placed_at >= cutoff]
    if not mine: return await update.message.reply_text(f"No tienes apuestas en los últimos {days} días")

    resolved = [b for b in mine if b.result in ("win", "loss", "push")]
    w, l, p = sum(1 for b in resolved if b.result == "win"), sum(1 for b in resolved if b.result == "loss"), sum(1 for b in resolved if b.result == "push")
    net = sum(b.amount for b in resolved if b.result == "win") - sum(b.amount for b in resolved if b.result == "loss")

    await update.message.reply_text(f"📊 *Mi historial — últimos {days} días*\n{'─'*24}\nTotal: `{len(mine)}` (resueltas: `{len(resolved)}`)\n✅ Wins: `{w}`  ❌ Losses: `{l}`  🔁 Push: `{p}`\n🎯 Win rate: *{round(w/(w+l)*100, 1) if w+l else 0}%*\n💰 Neto: `{net:.1f}` unidades", parse_mode=ParseMode.MARKDOWN)

# =========================
# COMANDOS DE ADMIN/DEBUG
# =========================
async def cmd_alertas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔍 Buscando top props pre-partido...", parse_mode=ParseMode.MARKDOWN)
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
        ls.append(f"*{i}.* {_pre_rating_emoji(po)} `{po}/100` — *{p.player}*\n    {p.tipo.upper()} OVER `{p.line}` | {_slug_to_matchup(p.game_slug or '')}")
    await msg.edit_text("\n".join(ls), parse_mode=ParseMode.MARKDOWN)

async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["🔍 *DEBUG*"]
    try:
        games = await asyncio.to_thread(lambda: scoreboard.ScoreBoard().get_dict()["scoreboard"]["games"])
        lines.append(f"📅 Partidos NBA hoy: {len(games)}")
    except Exception as e: lines.append(f"❌ Error scoreboard: {e}")

    props = PM_CACHE.get("props", [])
    lines.append(f"\n📦 Props en cache: {len(props)}")
    lines.append(f"⚡ PRE Score cache: {len(PRE_SCORE_CACHE)} entradas")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def cmd_miperfil(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    mine = [b for b in load_bets() if b.user_id == uid]
    w, l, p = sum(1 for b in mine if b.result == "win"), sum(1 for b in mine if b.result == "loss"), sum(1 for b in mine if not b.result)

    await update.message.reply_text(f"👤 *Mi perfil*\n{'─'*24}\nID: `{uid}`\nUsername: @{update.effective_user.username or '—'}\nAlias: *{user_display(uid)}*\nRol: {'👑 Admin' if is_admin(uid) else '👤 Usuario'}\n\n📊 Apuestas: {w}W / {l}L / {p} pendientes", parse_mode=ParseMode.MARKDOWN)

async def cmd_adduser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return await update.message.reply_text("⛔ Solo admins")
    if not context.args: return await update.message.reply_text("Uso: `/adduser USER_ID Nombre`")
    try: tid = int(context.args[0])
    except: return await update.message.reply_text("El ID debe ser numérico")
    add_user(tid, " ".join(context.args[1:]))
    await update.message.reply_text(f"✅ Usuario `{tid}` añadido")

async def cmd_removeuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return await update.message.reply_text("⛔ Solo admins")
    if not context.args: return await update.message.reply_text("Uso: `/removeuser USER_ID`")
    try: tid = int(context.args[0])
    except: return await update.message.reply_text("El ID debe ser numérico")
    remove_user(tid)
    await update.message.reply_text(f"✅ Usuario `{tid}` eliminado")

async def cmd_usuarios(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return await update.message.reply_text("⛔ Solo admins")
    users = load_users()
    lines = ["👥 *Usuarios autorizados:*"]
    for uid in users.get("allowed", []): lines.append(f"{'👑 ' if uid in users.get('admins', []) else '• '}`{uid}` — {users['nicknames'].get(str(uid), '—')}")
    if ADMIN_ID and ADMIN_ID not in users.get("allowed", []): lines.append(f"👑 `{ADMIN_ID}` — Admin (env)")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

# =========================
# BACKGROUND JOBS
# =========================
async def background_scan(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    props_pm = PM_CACHE.get("props", [])
    if not props_pm: props_pm = await asyncio.to_thread(polymarket_props_today_from_scoreboard)
    props = await asyncio.to_thread(load_props) + props_pm
    if not props: return

    state = await asyncio.to_thread(load_alert_state)
    by_pid = {}
    for p in props:
        pid = await asyncio.to_thread(get_pid_for_name, p.player)
        if pid: by_pid.setdefault(pid, []).append(p)

    try: games = await asyncio.to_thread(lambda: scoreboard.ScoreBoard().get_dict()["scoreboard"]["games"])
    except: return

    for g in games:
        if g.get("gameStatus") != 2: continue
        gid, status = g.get("gameId"), g.get("gameStatusText", "")
        
        try:
            sc_home, sc_away = g.get("homeTeam", {}).get("score"), g.get("awayTeam", {}).get("score")
            diff = abs(int(sc_home if sc_home else 0) - int(sc_away if sc_away else 0))
        except: diff = 0
        
        q, clk = int(g.get("period", 0)), g.get("gameClock", "")
        c_sec, el_m = clock_to_seconds(clk), game_elapsed_minutes(q, clock_to_seconds(clk))

        try: box = await asyncio.to_thread(lambda: boxscore.BoxScore(gid).get_dict()["game"])
        except: continue

        for tk in ["homeTeam", "awayTeam"]:
            for pl in box.get(tk, {}).get("players", []):
                pid = pl.get("personId")
                if pid not in by_pid: continue
                
                s = pl.get("statistics", {})
                pts, reb, ast = float(s.get("points") or 0), float(s.get("reboundsTotal") or 0), float(s.get("assists") or 0)
                pf, mins = float(s.get("foulsPersonal") or 0), parse_minutes(s.get("minutes", ""))

                for pr in by_pid[pid]:
                    try:
                        act = pts if pr.tipo == "puntos" else (reb if pr.tipo == "rebotes" else ast)
                        if pr.side == "over":
                            delta = pr.line - act
                            if 0.5 <= delta <= 4.0 and not should_gate_by_minutes("over", pr.tipo, delta, mins, el_m, diff>=20):
                                pv, meta = await asyncio.to_thread(pre_score, pid, pr.tipo, pr.line, pr.side)
                                live = compute_over_score(pr.tipo, delta, mins, pf, q, c_sec, diff, diff<=8, diff>=20)
                                final = int(clamp(0.55 * live + 0.45 * pv, 0, 100))
                                key = f"{gid}|{pid}|{pr.tipo}|{pr.side}|{pr.line}"
                                if final >= FINAL_ALERT_THRESHOLD or (diff<=8 and final >= FINAL_ALERT_THRESHOLD_CLUTCH):
                                    if str(state.get(key, 0)) != str(now_ts()) and now_ts() - int(state.get(key, 0)) >= COOLDOWN_SECONDS:
                                        state[key] = now_ts()
                                        await context.bot.send_message(chat_id, f"🎯 *ALERTA OVER* | *FINAL* `{final}/100`\n👤 *{pr.player}*\n📊 {pr.tipo.upper()} {act:.0f}/{pr.line} (faltan {delta:.1f})\n⏱️ {status} | Q{q} {clk}\n🧠 MIN {mins:.1f} | PF {pf} | Diff {diff}", parse_mode=ParseMode.MARKDOWN)
                        else:
                            delta = pr.line - act
                            if delta >= 2.0 and not should_gate_by_minutes("under", pr.tipo, delta, mins, el_m, diff>=20):
                                pv, meta = await asyncio.to_thread(pre_score, pid, pr.tipo, pr.line, pr.side)
                                live = compute_under_score(pr.tipo, delta, mins, pf, q, c_sec, diff, diff<=8, diff>=20)
                                final = int(clamp(0.65 * live + 0.35 * pv, 0, 100))
                                key = f"{gid}|{pid}|{pr.tipo}|{pr.side}|{pr.line}"
                                if final >= FINAL_ALERT_THRESHOLD or (diff>=20 and final >= FINAL_ALERT_THRESHOLD_CLUTCH):
                                    if str(state.get(key, 0)) != str(now_ts()) and now_ts() - int(state.get(key, 0)) >= COOLDOWN_SECONDS:
                                        state[key] = now_ts()
                                        await context.bot.send_message(chat_id, f"🧊 *ALERTA UNDER* | *FINAL* `{final}/100`\n👤 *{pr.player}*\n📊 {pr.tipo.upper()} {act:.0f}/{pr.line} (colchón {delta:.1f})\n⏱️ {status} | Q{q} {clk}\n🧠 MIN {mins:.1f} | PF {pf} | Diff {diff}", parse_mode=ParseMode.MARKDOWN)
                    except: pass
    await asyncio.to_thread(save_alert_state, state)

async def send_morning_digest(context: ContextTypes.DEFAULT_TYPE):
    td = date.today().isoformat()
    st = await asyncio.to_thread(load_morning_state)
    if st.get("last_date") == td: return
    st["last_date"] = td
    await asyncio.to_thread(save_morning_state, st)

    try: games = await asyncio.to_thread(lambda: scoreboard.ScoreBoard().get_dict()["scoreboard"]["games"])
    except: return
    if not games: return

    gl = [f"• {g.get('awayTeam',{}).get('teamTricode','?')} @ {g.get('homeTeam',{}).get('teamTricode','?')} — {g.get('gameStatusText','')}" for g in games]
    await context.bot.send_message(context.job.chat_id, f"🌅 *Buenos días NBA — {date.today().strftime('%A %d/%m/%Y').capitalize()}*\n" + "\n".join(gl), parse_mode=ParseMode.MARKDOWN)

async def background_check_morning(context: ContextTypes.DEFAULT_TYPE):
    if datetime.now().hour == MORNING_HOUR: await send_morning_digest(context)

# =========================
# REGISTRO Y MAIN
# =========================
async def register_job(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid, uname = update.effective_user.id, update.effective_user.first_name
    cid = update.effective_chat.id
    u = load_users()

    if not u["allowed"] and not u["admins"]:
        add_user(uid, uname, admin=True)
        await update.message.reply_text(f"👑 *Eres el primer usuario — eres Admin*\nTu ID: `{uid}`", parse_mode=ParseMode.MARKDOWN)
    elif not is_allowed(uid): return await update.message.reply_text(f"🔒 *Acceso restringido*\nTu ID: `{uid}`", parse_mode=ParseMode.MARKDOWN)
    else: add_user(uid, uname)

    if not context.job_queue.get_jobs_by_name(f"scan:{cid}"): context.job_queue.run_repeating(background_scan, interval=POLL_SECONDS, first=10, chat_id=cid, name=f"scan:{cid}")
    if not context.job_queue.get_jobs_by_name(f"morning:{cid}"): context.job_queue.run_repeating(background_check_morning, interval=3600, first=60, chat_id=cid, name=f"morning:{cid}")

    await update.message.reply_text(f"✅ *¡Bienvenido, {uname}!*\nJobs activados.\n\nUsa `/odds` para ver los partidos disponibles.", parse_mode=ParseMode.MARKDOWN)
    await cmd_help(update, context)

BOT_COMMANDS = [
    BotCommand("start", "Activar bot"), BotCommand("odds", "Props por partido"),
    BotCommand("games", "Partidos hoy"), BotCommand("live", "Props en vivo"),
    BotCommand("lineup", "Alineaciones"), BotCommand("analisis", "Análisis de jugador"),
    BotCommand("contexto", "Contexto defensivo"), BotCommand("alertas", "Top props de hoy"),
    BotCommand("bet", "Registrar apuesta"), BotCommand("misapuestas", "Ver pendientes"),
    BotCommand("historial", "Estadísticas"), BotCommand("resultado", "Cerrar apuesta"),
    BotCommand("miperfil", "Ver perfil"), BotCommand("help", "Ayuda")
]

async def on_startup(app: Application):
    try: await app.bot.set_my_commands(BOT_COMMANDS)
    except: pass

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    def guarded(fn):
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if not await guard(update): return
            return await fn(update, context)
        return wrapper

    app.add_handler(CommandHandler("start", register_job))
    app.add_handler(CommandHandler("help", guarded(cmd_help)))
    app.add_handler(CommandHandler("games", guarded(cmd_games)))
    app.add_handler(CommandHandler("odds", guarded(cmd_odds)))
    app.add_handler(CommandHandler("live", guarded(cmd_live)))
    app.add_handler(CommandHandler("lineup", guarded(cmd_lineup)))
    app.add_handler(CommandHandler("analisis", guarded(cmd_analisis)))
    app.add_handler(CommandHandler("contexto", guarded(cmd_contexto)))
    app.add_handler(CommandHandler("alertas", guarded(cmd_alertas)))
    app.add_handler(CommandHandler("bet", guarded(cmd_bet)))
    app.add_handler(CommandHandler("misapuestas", guarded(cmd_misapuestas)))
    app.add_handler(CommandHandler("historial", guarded(cmd_historial)))
    app.add_handler(CommandHandler("resultado", guarded(cmd_resultado)))
    app.add_handler(CommandHandler("miperfil", guarded(cmd_miperfil)))
    app.add_handler(CommandHandler("debug", guarded(cmd_debug)))
    app.add_handler(CommandHandler("adduser", cmd_adduser))
    app.add_handler(CommandHandler("removeuser", cmd_removeuser))
    app.add_handler(CommandHandler("usuarios", cmd_usuarios))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_game_selection))

    app.post_init = on_startup
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
