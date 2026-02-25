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
# CONFIG (ENV)
# =========================
TELEGRAM_TOKEN = (os.environ.get("TELEGRAM_TOKEN") or "").strip()
if not TELEGRAM_TOKEN:
    raise RuntimeError("Missing TELEGRAM_TOKEN env var")

NBA_SEASON = (os.environ.get("NBA_SEASON") or "2025-26").strip()

POLL_SECONDS = int(os.environ.get("POLL_SECONDS") or "120")

# Background scan thresholds
FINAL_ALERT_THRESHOLD = 75
FINAL_ALERT_THRESHOLD_CLUTCH = 68
COOLDOWN_SECONDS = 8 * 60

# Blowout / clutch
BLOWOUT_IS = 20
BLOWOUT_STRONG = 22

# “cerca de la línea”
THRESH_POINTS_OVER = (0.5, 4.0)
THRESH_REB_AST_OVER = (0.5, 1.5)

MIN_MINUTES_POINTS = 10.0
MIN_MINUTES_REB_AST = 14.0

# Pre-game
PREGAME_CHECK_EVERY = 60        # chequea cada 1 min
PREGAME_WINDOW_MIN = 95         # manda alertas dentro de ~95 min
PREGAME_TOLERANCE_MIN = 6
TOP_PREGAME_PROPS = 10

# Files
STATE_DIR = "."
ALERTS_STATE_FILE = os.path.join(STATE_DIR, "alerts_state.json")
IDS_CACHE_FILE = os.path.join(STATE_DIR, "player_ids_cache.json")
GLOG_CACHE_FILE = os.path.join(STATE_DIR, "gamelog_cache.json")
PM_CACHE_FILE = os.path.join(STATE_DIR, "pm_props_today.json")
PREGAME_STATE_FILE = os.path.join(STATE_DIR, "pregame_state.json")
INJURY_STATE_FILE = os.path.join(STATE_DIR, "injury_state.json")

# Polymarket
GAMMA = "https://gamma-api.polymarket.com"
# NBA series_id (comúnmente usado en ejemplos / comunidad)
NBA_SERIES_ID = int(os.environ.get("PM_NBA_SERIES_ID") or "10345")

# Injury report official
NBA_INJURY_PAGE = os.environ.get("NBA_INJURY_PAGE") or "https://official.nba.com/nba-injury-report-2025-26-season/"


# =========================
# HTTP Sessions
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
SESSION_WEB = build_session({"User-Agent": NBA_HEADERS["User-Agent"], "Accept": "text/html, */*"})


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
# Player ID cache
# =========================
def obtener_id_jugador(nombre: str) -> Optional[int]:
    time.sleep(0.25 + random.random() * 0.2)
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
        try:
            return int(cache[name])
        except Exception:
            pass
    pid = obtener_id_jugador(name)
    if pid:
        cache[name] = int(pid)
        save_ids_cache(cache)
    return pid


# =========================
# Gamelog cache + fetch (stats.nba.com direct)
# =========================
GLOG_TTL_SECONDS = 6 * 60 * 60  # 6h
STAT_COL = {"puntos": "PTS", "rebotes": "REB", "asistencias": "AST"}
STD_CAP = {"puntos": 8.0, "rebotes": 4.0, "asistencias": 3.0}
MARGIN_CAP = {"puntos": 8.0, "rebotes": 3.0, "asistencias": 3.0}

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

    time.sleep(0.6 + random.random() * 0.4)

    url = "https://stats.nba.com/stats/playergamelog"
    params = {
        "DateFrom": "",
        "DateTo": "",
        "LeagueID": "00",
        "PlayerID": str(pid),
        "Season": NBA_SEASON,
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
        "hits5": h5, "n5": n5,
        "hits10": h10, "n10": n10,
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
# Polymarket props fetch + parse
# =========================
@dataclass
class PMProp:
    event_id: Optional[int]
    market_id: Optional[str]
    game: str
    start_time: Optional[str]
    player: str
    tipo: str         # puntos/rebotes/asistencias
    line: float
    side: str         # over/under
    title: str

def _today_utc() -> date:
    return datetime.now(timezone.utc).date()

def pm_fetch_events_nba_today(limit: int = 200) -> List[dict]:
    """
    Estrategia robusta:
      - Pide events NBA activos/no cerrados por series_id
      - Filtra por startTime/endTime que caigan hoy (UTC)
      - Cada event viene con markets embebidos (en Gamma suele venir).
    """
    url = f"{GAMMA}/events"
    params = {
        "series_id": str(NBA_SERIES_ID),
        "active": "true",
        "closed": "false",
        "limit": str(limit),
        "offset": "0",
        "order": "startTime",
        "ascending": "true",
    }
    r = SESSION_PM.get(url, params=params, timeout=30)
    r.raise_for_status()
    events = r.json() or []
    today = _today_utc()

    out = []
    for ev in events:
        st = ev.get("startTime") or ev.get("start_time") or ev.get("startDate") or ev.get("start_date")
        if not st:
            continue
        try:
            # ISO string
            dt = datetime.fromisoformat(st.replace("Z", "+00:00")).astimezone(timezone.utc)
            if dt.date() == today:
                out.append(ev)
        except Exception:
            # si parse falla, lo dejamos pasar (pero rara vez)
            continue
    return out

# Regex multi-formato para capturar props P/R/A.
# Ejemplos típicos:
#  - "Caris LeVert Points Over/Under 6.5"
#  - "Caris LeVert - Points - 6.5"
#  - "Will Caris LeVert score over 6.5 points?"
RE_LINE = re.compile(r"(?P<line>\d+(?:\.\d)?)")
RE_PLAYER_STAT = [
    re.compile(r"^(?P<player>.+?)\s+(?P<stat>points|rebounds|assists)\b", re.I),
    re.compile(r"^(?P<player>.+?)\s*[-|•]\s*(?P<stat>points|rebounds|assists)\b", re.I),
    re.compile(r"will\s+(?P<player>.+?)\s+.*?(?P<stat>points|rebounds|assists)\b", re.I),
]

def _normalize_stat(s: str) -> Optional[str]:
    s = s.lower().strip()
    if "point" in s: return "puntos"
    if "rebound" in s: return "rebotes"
    if "assist" in s: return "asistencias"
    return None

def pm_parse_props_from_event(ev: dict) -> List[PMProp]:
    """
    Parse de markets del event:
    - intenta leer ev["markets"] (lista)
    - toma "question"/"title" del market
    - busca stat + línea y genera 2 props (over/under) aunque el market venga con outcomes Yes/No.
    """
    markets = ev.get("markets") or ev.get("Markets") or []
    if not isinstance(markets, list):
        return []

    event_id = ev.get("id")
    game = ev.get("title") or ev.get("name") or ev.get("slug") or "NBA Game"
    start_time = ev.get("startTime") or ev.get("start_time")

    out: List[PMProp] = []
    for m in markets:
        title = (m.get("question") or m.get("title") or m.get("name") or "").strip()
        if not title:
            continue

        # filtra sólo P/R/A
        stat = None
        player = None
        for rx in RE_PLAYER_STAT:
            mm = rx.search(title)
            if mm:
                player = (mm.group("player") or "").strip()
                stat = _normalize_stat(mm.group("stat") or "")
                break
        if not stat or not player:
            continue

        lm = RE_LINE.search(title)
        if not lm:
            # a veces la línea está en outcomes; intentamos buscar en outcomes
            outcomes = m.get("outcomes") or []
            joined = " ".join([str(o.get("name") or "") for o in outcomes if isinstance(o, dict)])
            lm = RE_LINE.search(joined)
        if not lm:
            continue

        try:
            line = float(lm.group("line"))
        except Exception:
            continue

        market_id = str(m.get("id") or "")

        # Generamos dos “lados” siempre
        out.append(PMProp(event_id=event_id, market_id=market_id, game=game, start_time=start_time,
                          player=player, tipo=stat, line=line, side="over", title=title))
        out.append(PMProp(event_id=event_id, market_id=market_id, game=game, start_time=start_time,
                          player=player, tipo=stat, line=line, side="under", title=title))

    return out

def pm_refresh_cache() -> List[PMProp]:
    """
    Descarga props de hoy y guarda cache. Devuelve lista.
    """
    events = pm_fetch_events_nba_today()
    props: List[PMProp] = []
    for ev in events:
        props.extend(pm_parse_props_from_event(ev))

    # dedupe (player,tipo,line,side,game)
    seen = set()
    uniq = []
    for p in props:
        k = (p.game, p.player.lower(), p.tipo, float(p.line), p.side)
        if k in seen:
            continue
        seen.add(k)
        uniq.append(p)

    save_json(PM_CACHE_FILE, {
        "ts": now_ts(),
        "date_utc": str(_today_utc()),
        "count": len(uniq),
        "props": [asdict(x) for x in uniq]
    })
    return uniq

def pm_load_cache() -> List[PMProp]:
    raw = load_json(PM_CACHE_FILE, {})
    if not raw:
        return []
    if raw.get("date_utc") != str(_today_utc()):
        return []
    out = []
    for d in raw.get("props", []):
        try:
            out.append(PMProp(**d))
        except Exception:
            pass
    return out


# =========================
# Injury report watcher (PDF oficial NBA)
# =========================
def injury_find_latest_pdf_url(html: str) -> Optional[str]:
    # Busca URLs del estilo Injury-Report_YYYY-MM-DD_...
    m = re.findall(r"https?://[^\s\"']+Injury-Report_[0-9]{4}-[0-9]{2}-[0-9]{2}[^\"']+\.pdf", html)
    if not m:
        # fallback: PDFs en ak-static
        m = re.findall(r"https?://ak-static\.cms\.nba\.com/referee/injury/Injury-Report_[^\"']+\.pdf", html)
    if not m:
        return None
    # el “último” suele ser el más reciente por fecha en string; ordenamos desc
    m_sorted = sorted(set(m), reverse=True)
    return m_sorted[0]

def injury_check_update() -> Optional[str]:
    try:
        r = SESSION_WEB.get(NBA_INJURY_PAGE, timeout=25)
        if r.status_code != 200:
            return None
        pdf = injury_find_latest_pdf_url(r.text or "")
        if not pdf:
            return None
        st = load_json(INJURY_STATE_FILE, {"last_pdf": ""})
        if st.get("last_pdf") != pdf:
            st["last_pdf"] = pdf
            st["ts"] = now_ts()
            save_json(INJURY_STATE_FILE, st)
            return pdf
        return None
    except Exception:
        return None


# =========================
# Schedule + records (NBA live scoreboard)
# =========================
def nba_games_today() -> List[dict]:
    try:
        games = scoreboard.ScoreBoard().get_dict()["scoreboard"]["games"]
        return games or []
    except Exception:
        return []

def minutes_to_start(game_obj: dict) -> Optional[float]:
    # gameTimeUTC suele venir como ISO; si no, fallback a gameStatusText no sirve.
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
                start = datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
            else:
                start = datetime.fromisoformat(s)
                if start.tzinfo is None:
                    start = start.replace(tzinfo=timezone.utc)
                start = start.astimezone(timezone.utc)
            now = datetime.now(timezone.utc)
            return (start - now).total_seconds() / 60.0
        except Exception:
            continue
    return None

def in_window(m: float, target: float, tol: float) -> bool:
    return (target - tol) <= m <= (target + tol)


# =========================
# Commands
# =========================
HELP_TEXT = (
    "🧠 *NBA Interactive Bot*\n\n"
    "Comandos:\n"
    "• `/start`  → activa background scan (cada 120s) + pregame + injury watcher\n"
    "• `/odds`   → props NBA (P/R/A) de hoy en Polymarket + ranking (PRE)\n"
    "• `/live`   → props en vivo con score FINAL (LIVE+PRE)\n"
    "• `/today`  → programación de hoy + records\n"
)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)

async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    games = nba_games_today()
    if not games:
        await update.message.reply_text("No pude leer el scoreboard de hoy.")
        return

    lines = ["📅 *NBA hoy*"]
    for g in games:
        home = g.get("homeTeam", {}) or {}
        away = g.get("awayTeam", {}) or {}
        hs = home.get("teamTricode") or home.get("teamName") or "HOME"
        as_ = away.get("teamTricode") or away.get("teamName") or "AWAY"
        hw = home.get("wins")
        hl = home.get("losses")
        aw = away.get("wins")
        al = away.get("losses")
        rec_h = f"({hw}-{hl})" if hw is not None and hl is not None else ""
        rec_a = f"({aw}-{al})" if aw is not None and al is not None else ""
        st = g.get("gameStatusText", "")
        lines.append(f"• *{as_}* {rec_a} @ *{hs}* {rec_h} — {st}")

    msg = "\n".join(lines)
    await update.message.reply_text(msg[:3900], parse_mode=ParseMode.MARKDOWN)

def _rank_props_by_pre(props: List[PMProp]) -> List[Tuple[PMProp, int, dict]]:
    ranked = []
    for p in props:
        pid = get_pid_for_name(p.player)
        if not pid:
            continue
        pre, meta = pre_score(pid, p.tipo, p.line, p.side)
        ranked.append((p, pre, meta))
        time.sleep(0.12 + random.random() * 0.08)
    ranked.sort(key=lambda x: x[1], reverse=True)
    return ranked

async def cmd_odds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # refresh cache si no hay
    props = pm_load_cache()
    if not props:
        try:
            props = pm_refresh_cache()
        except Exception as e:
            await update.message.reply_text(f"⚠️ Polymarket error: {e}")
            return

    if not props:
        await update.message.reply_text("No pude parsear props P/R/A desde Polymarket (0 encontrados).")
        return

    ranked = _rank_props_by_pre(props)[:15]

    lines = ["🟣 *Polymarket NBA Props (P/R/A) — Ranking PRE (forma)*"]
    for p, pre, meta in ranked:
        lines.append(
            f"• *{p.player}* — {p.tipo.upper()} *{p.side.upper()}* {p.line}  | PRE `{pre}/100` "
            f"(hit10 {meta['hits10']}/{meta['n10']})"
        )

    await update.message.reply_text("\n".join(lines)[:3900], parse_mode=ParseMode.MARKDOWN)

async def cmd_live(update: Update, context: ContextTypes.DEFAULT_TYPE):
    props = pm_load_cache()
    if not props:
        try:
            props = pm_refresh_cache()
        except Exception:
            props = []

    if not props:
        await update.message.reply_text("No pude cargar props de Polymarket (0).")
        return

    # Map props by pid
    by_pid: Dict[int, List[PMProp]] = {}
    for p in props:
        pid = get_pid_for_name(p.player)
        if pid:
            by_pid.setdefault(pid, []).append(p)

    games = nba_games_today()
    live_games = [g for g in games if g.get("gameStatus") == 2]
    if not live_games:
        await update.message.reply_text("No hay partidos en vivo ahora.")
        return

    results = []
    for g in live_games:
        gid = g.get("gameId")
        status = g.get("gameStatusText", "")
        home = g.get("homeTeam", {}) or {}
        away = g.get("awayTeam", {}) or {}
        diff = abs(int(home.get("score", 0) or 0) - int(away.get("score", 0) or 0))
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

                    pre, meta = pre_score(pid, pr.tipo, pr.line, pr.side)

                    if pr.side == "over":
                        faltante = float(pr.line) - float(actual)
                        lo, hi = THRESH_POINTS_OVER if pr.tipo == "puntos" else THRESH_REB_AST_OVER
                        if not (lo <= faltante <= hi):
                            continue
                        if should_gate_by_minutes("over", pr.tipo, mins, elapsed_min, is_blowout):
                            continue
                        if diff >= BLOWOUT_STRONG:
                            if (pr.tipo == "puntos" and faltante > 1.0) or (pr.tipo != "puntos" and faltante > 0.8):
                                continue

                        live = compute_over_score(pr.tipo, faltante, mins, pf, period, clock_sec, diff, is_clutch, is_blowout)
                        final = int(clamp(0.55 * live + 0.45 * pre, 0, 100))
                        results.append((final, pr, actual, status, period, game_clock, mins, pf, diff, meta, live, pre, faltante))
                    else:
                        margin_under = float(pr.line) - float(actual)
                        if should_gate_by_minutes("under", pr.tipo, mins, elapsed_min, is_blowout):
                            continue
                        live = compute_under_score(pr.tipo, margin_under, mins, pf, period, clock_sec, diff, is_clutch, is_blowout)
                        final = int(clamp(0.65 * live + 0.35 * pre, 0, 100))
                        results.append((final, pr, actual, status, period, game_clock, mins, pf, diff, meta, live, pre, margin_under))

    if not results:
        await update.message.reply_text("Hay juegos en vivo, pero no encontré spots cercanos para props (según thresholds).")
        return

    results.sort(key=lambda x: x[0], reverse=True)
    top = results[:15]

    lines = ["🏀 *LIVE — Top props por FINAL score*"]
    for (final, pr, actual, status, period, clock, mins, pf, diff, meta, live, pre, delta) in top:
        tag = "🎯" if pr.side == "over" else "🧊"
        extra = f"faltan {delta:.1f}" if pr.side == "over" else f"colchón {delta:.1f}"
        lines.append(
            f"{tag} *{pr.player}* — {pr.tipo.upper()} *{pr.side.upper()}* {pr.line} | "
            f"ACT {actual} ({extra}) | FINAL `{final}` (LIVE {live} PRE {pre}) | Q{period} {clock} | Diff {diff} | hit10 {meta['hits10']}/{meta['n10']}"
        )

    await update.message.reply_text("\n".join(lines)[:3900], parse_mode=ParseMode.MARKDOWN)


# =========================
# Background jobs
# =========================
async def job_refresh_polymarket(context: ContextTypes.DEFAULT_TYPE):
    try:
        pm_refresh_cache()
    except Exception:
        pass

async def job_injury_watcher(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    pdf = injury_check_update()
    if pdf:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🩺 *Injury Report actualizado*\nPDF: {pdf}",
            parse_mode=ParseMode.MARKDOWN
        )

def load_pregame_state():
    return load_json(PREGAME_STATE_FILE, {})

def save_pregame_state(st):
    save_json(PREGAME_STATE_FILE, st)

async def job_pregame(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    games = nba_games_today()
    if not games:
        return

    st = load_pregame_state()

    props = pm_load_cache()
    if not props:
        try:
            props = pm_refresh_cache()
        except Exception:
            props = []

    ranked = _rank_props_by_pre(props) if props else []

    # Partido destacado: primer juego que arranca pronto y tiene status 1
    upcoming = []
    for g in games:
        if g.get("gameStatus") != 1:
            continue
        m = minutes_to_start(g)
        if m is None:
            continue
        if 0 < m <= PREGAME_WINDOW_MIN:
            upcoming.append((m, g))
    upcoming.sort(key=lambda x: x[0])

    if not upcoming:
        save_pregame_state(st)
        return

    # T-90 para el primer partido “más cercano”
    m, g = upcoming[0]
    gid = g.get("gameId")
    key90 = f"{gid}:t90:{str(_today_utc())}"

    if in_window(m, 90, PREGAME_TOLERANCE_MIN) and not st.get(key90):
        st[key90] = now_ts()

        home = (g.get("homeTeam") or {})
        away = (g.get("awayTeam") or {})
        hs = home.get("teamTricode") or home.get("teamName") or "HOME"
        as_ = away.get("teamTricode") or away.get("teamName") or "AWAY"
        status_text = g.get("gameStatusText", "")

        lines = [
            "⭐ *Partido destacado de hoy*",
            f"🏀 *{as_} @ {hs}*",
            f"🕒 {status_text}",
            "",
            "📌 *Top props por forma (PRE)*"
        ]
        for p, pre, meta in ranked[:TOP_PREGAME_PROPS]:
            lines.append(f"• *{p.player}* — {p.tipo.upper()} {p.side.upper()} {p.line} | PRE `{pre}` (hit10 {meta['hits10']}/{meta['n10']})")

        await context.bot.send_message(chat_id=chat_id, text="\n".join(lines)[:3900], parse_mode=ParseMode.MARKDOWN)

    save_pregame_state(st)

async def job_background_scan(context: ContextTypes.DEFAULT_TYPE):
    """
    Cada 120s:
    - lee props Polymarket cache
    - escanea boxscore live
    - manda alertas si FINAL >= threshold
    """
    chat_id = context.job.chat_id

    props = pm_load_cache()
    if not props:
        try:
            props = pm_refresh_cache()
        except Exception:
            props = []

    if not props:
        return

    state = load_alert_state()

    by_pid: Dict[int, List[PMProp]] = {}
    for p in props:
        pid = get_pid_for_name(p.player)
        if pid:
            by_pid.setdefault(pid, []).append(p)

    games = nba_games_today()
    for g in games:
        if g.get("gameStatus") != 2:
            continue

        gid = g.get("gameId")
        status = g.get("gameStatusText", "")

        home = g.get("homeTeam", {}) or {}
        away = g.get("awayTeam", {}) or {}
        diff = abs(int(home.get("score", 0) or 0) - int(away.get("score", 0) or 0))

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
                    pre, meta = pre_score(pid, pr.tipo, pr.line, pr.side)

                    if pr.side == "over":
                        faltante = float(pr.line) - float(actual)
                        lo, hi = THRESH_POINTS_OVER if pr.tipo == "puntos" else THRESH_REB_AST_OVER
                        if not (lo <= faltante <= hi):
                            continue
                        if should_gate_by_minutes("over", pr.tipo, mins, elapsed_min, is_blowout):
                            continue
                        if diff >= BLOWOUT_STRONG:
                            if (pr.tipo == "puntos" and faltante > 1.0) or (pr.tipo != "puntos" and faltante > 0.8):
                                continue

                        live = compute_over_score(pr.tipo, faltante, mins, pf, period, clock_sec, diff, is_clutch, is_blowout)
                        final = int(clamp(0.55 * live + 0.45 * pre, 0, 100))

                        if final >= FINAL_ALERT_THRESHOLD or (is_clutch and final >= FINAL_ALERT_THRESHOLD_CLUTCH):
                            key = f"{gid}|{pid}|{pr.tipo}|{pr.side}|{pr.line}"
                            if can_send_alert(state, key):
                                msg = (
                                    f"🎯 *ALERTA OVER* | FINAL `{final}/100` (LIVE {live} PRE {pre})\n"
                                    f"👤 *{pr.player}*\n"
                                    f"📊 {pr.tipo.upper()} {actual}/{pr.line} (faltan {faltante:.1f})\n"
                                    f"⏱️ {status} | Q{period} {game_clock}\n"
                                    f"🧠 MIN {mins:.1f} | PF {pf} | Diff {diff}\n"
                                    f"📈 Forma hit10 {meta['hits10']}/{meta['n10']} | avg10 {meta['avg10']}\n"
                                )
                                await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)

                    else:
                        margin_under = float(pr.line) - float(actual)
                        if should_gate_by_minutes("under", pr.tipo, mins, elapsed_min, is_blowout):
                            continue

                        live = compute_under_score(pr.tipo, margin_under, mins, pf, period, clock_sec, diff, is_clutch, is_blowout)
                        final = int(clamp(0.65 * live + 0.35 * pre, 0, 100))

                        if final >= FINAL_ALERT_THRESHOLD or (is_blowout and final >= FINAL_ALERT_THRESHOLD_CLUTCH):
                            key = f"{gid}|{pid}|{pr.tipo}|{pr.side}|{pr.line}"
                            if can_send_alert(state, key):
                                msg = (
                                    f"🧊 *ALERTA UNDER* | FINAL `{final}/100` (LIVE {live} PRE {pre})\n"
                                    f"👤 *{pr.player}*\n"
                                    f"📊 {pr.tipo.upper()} {actual}/{pr.line} (colchón {margin_under:.1f})\n"
                                    f"⏱️ {status} | Q{period} {game_clock}\n"
                                    f"🧠 MIN {mins:.1f} | PF {pf} | Diff {diff}\n"
                                    f"📈 Forma hit10 {meta['hits10']}/{meta['n10']} | avg10 {meta['avg10']}\n"
                                )
                                await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)

    save_alert_state(state)


# =========================
# /start registers jobs per chat
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    # scan job
    name_scan = f"scan:{chat_id}"
    if not context.job_queue.get_jobs_by_name(name_scan):
        context.job_queue.run_repeating(job_background_scan, interval=POLL_SECONDS, first=7, chat_id=chat_id, name=name_scan)

    # polymarket refresh (cada 15 min)
    name_pm = f"pm:{chat_id}"
    if not context.job_queue.get_jobs_by_name(name_pm):
        context.job_queue.run_repeating(job_refresh_polymarket, interval=15 * 60, first=3, chat_id=chat_id, name=name_pm)

    # pregame watcher (cada 60s)
    name_pg = f"pregame:{chat_id}"
    if not context.job_queue.get_jobs_by_name(name_pg):
        context.job_queue.run_repeating(job_pregame, interval=PREGAME_CHECK_EVERY, first=10, chat_id=chat_id, name=name_pg)

    # injury watcher (cada 10 min)
    name_inj = f"inj:{chat_id}"
    if not context.job_queue.get_jobs_by_name(name_inj):
        context.job_queue.run_repeating(job_injury_watcher, interval=10 * 60, first=20, chat_id=chat_id, name=name_inj)

    await update.message.reply_text(
        "✅ Background scan activado (cada 120s) + pregame (T-90) + injury watcher.\n\n" + HELP_TEXT,
        parse_mode=ParseMode.MARKDOWN
    )


# =========================
# Main
# =========================
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("odds", cmd_odds))
    app.add_handler(CommandHandler("live", cmd_live))

    log.info("Bot running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

       
