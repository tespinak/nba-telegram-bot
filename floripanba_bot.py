import os
import re
import json
import time
import math
import random
import asyncio
import logging
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime, timezone, date

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
    raise RuntimeError("Missing TELEGRAM_TOKEN env var (set it in Railway Variables)")

POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))
SEASON = os.environ.get("NBA_SEASON", "2025-26")

PROPS_FILE = "props.json"
ALERTS_STATE_FILE = "alerts_state.json"
IDS_CACHE_FILE = "player_ids_cache.json"
GLOG_CACHE_FILE = "gamelog_cache.json"
PREGAME_STATE_FILE = "pregame_state.json"
SIGNALS_LOG_FILE = "signals_log.jsonl"
MAX_ALERTS_PER_SCAN = int(os.environ.get("MAX_ALERTS_PER_SCAN", "8"))
RESULTS_DEFAULT_LIMIT = int(os.environ.get("RESULTS_DEFAULT_LIMIT", "8"))
RAILWAY_PUBLIC_DOMAIN = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()

# thresholds / scoring
FINAL_ALERT_THRESHOLD = 75
FINAL_ALERT_THRESHOLD_CLUTCH = 68

COOLDOWN_SECONDS = 8 * 60
BLOWOUT_IS = 20
BLOWOUT_STRONG = 22

# Para estar "cerca" de la línea
THRESH_POINTS_OVER = (0.5, 4.0)
THRESH_REB_AST_OVER = (0.5, 1.5)

MIN_MINUTES_POINTS = 10.0
MIN_MINUTES_REB_AST = 14.0

# PRE score caps
STAT_COL = {"puntos": "PTS", "rebotes": "REB", "asistencias": "AST"}
STD_CAP = {"puntos": 8.0, "rebotes": 4.0, "asistencias": 3.0}
MARGIN_CAP = {"puntos": 8.0, "rebotes": 3.0, "asistencias": 3.0}

# Polymarket Gamma API
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


def log_signal_event(event: dict):
    try:
        payload = dict(event)
        payload.setdefault("ts", now_ts())
        with open(SIGNALS_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning("No pude guardar señal en %s: %s", SIGNALS_LOG_FILE, e)


def read_signal_events(limit: int = 20) -> List[dict]:
    if limit <= 0:
        return []
    if not os.path.exists(SIGNALS_LOG_FILE):
        return []
    try:
        with open(SIGNALS_LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as e:
        log.warning("No pude leer %s: %s", SIGNALS_LOG_FILE, e)
        return []

    out: List[dict] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                out.append(obj)
        except Exception:
            continue
    return out


def ts_to_hhmm(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%H:%M UTC")
    except Exception:
        return "--:-- UTC"


def env_check_summary() -> List[str]:
    token_status = "OK" if TELEGRAM_TOKEN else "MISSING"
    return [
        f"TELEGRAM_TOKEN: {token_status}",
        f"NBA_SEASON: {SEASON}",
        f"POLL_SECONDS: {POLL_SECONDS}",
        f"MAX_ALERTS_PER_SCAN: {MAX_ALERTS_PER_SCAN}",
        f"RAILWAY_PUBLIC_DOMAIN: {RAILWAY_PUBLIC_DOMAIN or '(no configurado)'}",
    ]


# =========================
# Data model
# =========================
@dataclass
class Prop:
    player: str              # "Stephen Curry"
    tipo: str                # "puntos" | "rebotes" | "asistencias"
    line: float              # 26.5
    side: str                # "over" | "under"
    added_by: Optional[int] = None
    added_at: Optional[int] = None

def load_props() -> List[Prop]:
    raw = load_json(PROPS_FILE, {"props": []})
    out = []
    for p in raw.get("props", []):
        try:
            out.append(Prop(**p))
        except Exception as e:
            log.warning("Prop inválida en %s: %s (%s)", PROPS_FILE, p, e)
            continue
    return out

def save_props(props: List[Prop]):
    save_json(PROPS_FILE, {"props": [asdict(p) for p in props]})


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
    return int(pick.get("id"))

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

    time.sleep(0.55 + random.random() * 0.45)

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
# PRE SCORE (forma 5/10) for over/under
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
        min_weight = 12.0
    else:
        near_max, ideal_max = 1.5, 0.9
        close_weight, ideal_bonus = 65, 12
        min_floor = 14.0
        foul_mult, blow_mult = 1.25, 1.35
        min_weight = 12.0

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

    min_score = clamp((mins - min_floor) / 18 * min_weight, 0, min_weight)

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
# Alert state / cooldown per prop
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
# Pregame helpers (featured + T-90)
# =========================
def load_pregame_state():
    return load_json(PREGAME_STATE_FILE, {})

def save_pregame_state(st):
    save_json(PREGAME_STATE_FILE, st)

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

def mark_once(st: dict, key: str) -> bool:
    if key in st:
        return False
    st[key] = now_ts()
    return True


# =========================
# Polymarket: universo P/R/A (sin cuotas)
# =========================
PAR_LINE_RE = re.compile(
    r"^(?P<player>.+?)\s+"
    r"(?P<stat>puntos|rebotes|asistencias|points|rebounds|assists|pts|reb|ast)\b.*?"
    r"(?:(?:\bO(?:ver)?\s*(?P<o1>\d+(\.\d+)?)\b.*?\bU(?:nder)?\s*(?P<u1>\d+(\.\d+)?)\b)"
    r"|(?:\bOver\s*(?P<o2>\d+(\.\d+)?)\b.*?\bUnder\s*(?P<u2>\d+(\.\d+)?)\b))",
    re.IGNORECASE
)

STAT_MAP = {
    "points": "puntos", "pts": "puntos", "puntos": "puntos",
    "rebounds": "rebotes", "reb": "rebotes", "rebotes": "rebotes",
    "assists": "asistencias", "ast": "asistencias", "asistencias": "asistencias",
}

def parse_polymarket_ou_title(title: str) -> List[dict]:
    t = (title or "").strip().replace("•", " ").replace("|", " ")
    m = PAR_LINE_RE.search(t)
    if not m:
        return []

    player = (m.group("player") or "").strip().strip(".").title()
    stat_raw = (m.group("stat") or "").lower().strip()
    tipo = STAT_MAP.get(stat_raw)
    if not player or not tipo:
        return []

    line_s = m.group("o1") or m.group("o2")
    if not line_s:
        return []

    line = float(line_s)

    return [
        {"player": player, "tipo": tipo, "side": "over", "line": line},
        {"player": player, "tipo": tipo, "side": "under", "line": line},
    ]

def polymarket_fetch_events(limit: int = 250) -> List[dict]:
    url = f"{GAMMA}/events"
    params = {
        "active": "true",
        "closed": "false",
        "archived": "false",
        "limit": str(limit),
        "offset": "0",
    }
    r = SESSION_PM.get(url, params=params, timeout=25)
    r.raise_for_status()
    return r.json() or []

def build_polymarket_universe_par(limit_events: int = 250) -> List[dict]:
    events = polymarket_fetch_events(limit=limit_events)
    universe = []

    for ev in events:
        title_ev = (ev.get("title") or "").lower()
        if "nba" not in title_ev:
            continue

        markets = ev.get("markets") or []
        for m in markets:
            q = m.get("question") or m.get("title") or ""
            low = q.lower()
            if not any(k in low for k in ["puntos", "rebotes", "asistencias", "points", "rebounds", "assists", "pts", "reb", "ast"]):
                continue

            props = parse_polymarket_ou_title(q)
            if props:
                universe.extend(props)

    seen = set()
    out = []
    for p in universe:
        key = (p["player"].lower(), p["tipo"], p["side"], float(p["line"]))
        if key not in seen:
            seen.add(key)
            out.append(p)

    return out


# =========================
# Commands
# =========================
HELP_TEXT = (
    "🧠 *NBA Interactive Bot*\n\n"
    "Comandos:\n"
    "• `/today` → programación NBA de hoy\n"
    "• `/odds`  → props P/R/A de Polymarket (sin cuotas)\n"
    "• `/live`  → estado en vivo de tus props (si cargaste props.json)\n"
    "• `/results [N]` → últimas señales emitidas (desde signals_log.jsonl)\n"
    "• `/status` → chequeo rápido para deploy en Railway\n\n"
    "Opcional:\n"
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
    return Prop(player=name, tipo=tipo, side=side, line=line)

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

    await update.message.reply_text(
        f"✅ Agregado:\n• {p.player} — {p.tipo.upper()} {p.side.upper()} {p.line}",
    )

async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        games = scoreboard.ScoreBoard().get_dict()["scoreboard"]["games"]
    except Exception as e:
        await update.message.reply_text(f"⚠️ No pude leer scoreboard: {e}")
        return

    if not games:
        await update.message.reply_text("No hay juegos en el scoreboard ahora mismo.")
        return

    def team_str(t: dict) -> str:
        name = t.get("teamName") or t.get("teamCity") or "TEAM"
        w = t.get("wins")
        l = t.get("losses")
        rec = f" ({w}-{l})" if (w is not None and l is not None) else ""
        return f"{name}{rec}"

    lines = ["🏀 *NBA Hoy — Programación*"]
    for g in games:
        home = team_str(g.get("homeTeam", {}) or {})
        away = team_str(g.get("awayTeam", {}) or {})
        st = int(g.get("gameStatus", 0) or 0)
        st_text = g.get("gameStatusText", "") or ""
        tag = "PRE" if st == 1 else ("LIVE" if st == 2 else "FINAL" if st == 3 else "—")
        lines.append(f"• *{away} @ {home}* — `{tag}` — {st_text}")

    msg = "\n".join(lines)
    await update.message.reply_text(msg[:3800], parse_mode=ParseMode.MARKDOWN)

async def cmd_odds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        props = build_polymarket_universe_par(limit_events=250)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error Polymarket: {e}")
        return

    if not props:
        await update.message.reply_text(
            "No pude parsear props P/R/A desde Polymarket.\n"
            "Si me pegas 2-3 títulos exactos, ajusto el parser en 1 minuto."
        )
        return

    lines = ["🟣 *Polymarket — Props P/R/A (sin cuotas)*"]
    for p in props[:35]:
        lines.append(f"• {p['player']} — {p['tipo']} {p['side'].upper()} {p['line']}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["🛠️ *Estado del bot*"]
    lines.extend([f"• {x}" for x in env_check_summary()])

    props_count = len(load_props())
    lines.append(f"• Props manuales cargadas: {props_count}")

    recent = read_signal_events(limit=1)
    if recent:
        ev = recent[-1]
        lines.append(
            f"• Última señal: {ts_to_hhmm(ev.get('ts'))} | {ev.get('player', '?')} | {str(ev.get('tipo', '?')).upper()} {str(ev.get('side', '?')).upper()} {ev.get('line', '?')}"
        )
    else:
        lines.append("• Última señal: (sin datos todavía)")

    lines.append("\nTip Railway: despliega, abre Telegram, ejecuta /start y luego /status.")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_results(update: Update, context: ContextTypes.DEFAULT_TYPE):
    n = RESULTS_DEFAULT_LIMIT
    if context.args and len(context.args) >= 1:
        try:
            n = int(context.args[0])
        except Exception:
            n = RESULTS_DEFAULT_LIMIT
    n = max(1, min(30, n))

    events = read_signal_events(limit=n)
    if not events:
        await update.message.reply_text("No hay señales registradas todavía en signals_log.jsonl.")
        return

    lines = ["📒 *Últimas señales emitidas*"]
    for ev in reversed(events):
        player = ev.get("player", "?")
        tipo = str(ev.get("tipo", "?")).upper()
        side = str(ev.get("side", "?")).upper()
        line = ev.get("line", "?")
        actual = ev.get("actual", "?")
        final = ev.get("final", "?")
        game_status = ev.get("status", "")
        t = ts_to_hhmm(ev.get("ts"))
        lines.append(f"• {t} | {player} — {tipo} {side} {line} | act {actual} | score {final} | {game_status}")

    msg = "\n".join(lines)
    await update.message.reply_text(msg[:3900], parse_mode=ParseMode.MARKDOWN)


async def cmd_live(update: Update, context: ContextTypes.DEFAULT_TYPE):
    props = load_props()
    if not props:
        await update.message.reply_text("No hay props cargados en props.json. (Este comando es para props manuales).")
        return

    by_player: Dict[str, List[Prop]] = {}
    for p in props:
        by_player.setdefault(p.player, []).append(p)

    try:
        games = scoreboard.ScoreBoard().get_dict()["scoreboard"]["games"]
    except Exception as e:
        await update.message.reply_text(f"⚠️ No pude leer scoreboard: {e}")
        return

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
        except Exception as e:
            log.warning("No pude leer boxscore gameId=%s: %s", gid, e)
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
        await update.message.reply_text("No hay partidos en vivo o ninguno de tus jugadores está jugando ahora.")
        return

    msg = "\n".join(out)
    if len(msg) > 3500:
        msg = msg[:3500] + "\n…(recortado)"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


# =========================
# Background scan: pregame + scoring + alerts
# =========================
async def background_scan(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id

    # games
    try:
        games = scoreboard.ScoreBoard().get_dict()["scoreboard"]["games"]
    except Exception as e:
        log.warning("No pude leer scoreboard en background_scan: %s", e)
        return

    # --- Pregame: Featured + T-90 ---
    pre = load_pregame_state()

    upcoming = []
    for g in games:
        if int(g.get("gameStatus", 0) or 0) != 1:
            continue
        m = minutes_to_start(g)
        if m is None:
            continue
        upcoming.append((m, g))
    upcoming.sort(key=lambda x: x[0])

    # Partido destacado (una vez al día por juego)
    if upcoming:
        m, g0 = upcoming[0]
        gid0 = g0.get("gameId")
        if 0 < m <= 240:
            key_feat = f"{gid0}:featured:{date.today().isoformat()}"
            if mark_once(pre, key_feat):
                ht = (g0.get("homeTeam") or {}).get("teamName", "HOME")
                at = (g0.get("awayTeam") or {}).get("teamName", "AWAY")
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"⭐ *Partido destacado*\n"
                        f"🏀 *{at} @ {ht}*\n"
                        f"⏱️ Faltan aprox: `{int(round(m))} min`"
                    ),
                    parse_mode=ParseMode.MARKDOWN
                )

    # T-90 (±6 min)
    for m, g in upcoming:
        if not (84 <= m <= 96):
            continue
        gid = g.get("gameId")
        key90 = f"{gid}:t90"
        if mark_once(pre, key90):
            ht = (g.get("homeTeam") or {}).get("teamName", "HOME")
            at = (g.get("awayTeam") or {}).get("teamName", "AWAY")
            st_text = g.get("gameStatusText", "")
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"⏳ *PRE-GAME T-90*\n"
                    f"🏀 *{at} @ {ht}*\n"
                    f"🕒 {st_text}\n"
                    f"📌 Ventana: empieza en ~1h30."
                ),
                parse_mode=ParseMode.MARKDOWN
            )

    save_pregame_state(pre)

    # --- Props fuente: props.json o universo Polymarket ---
    props = load_props()
    if not props:
        # modo automático: Polymarket
        try:
            pm = build_polymarket_universe_par(limit_events=250)
            props = [Prop(player=p["player"], tipo=p["tipo"], side=p["side"], line=float(p["line"])) for p in pm]
        except Exception as e:
            log.warning("No pude construir universo Polymarket automático: %s", e)
            return

    if not props:
        return

    state = load_alert_state()

    # pid -> props
    by_pid: Dict[int, List[Prop]] = {}
    for p in props:
        pid = get_pid_for_name(p.player)
        if pid:
            by_pid.setdefault(pid, []).append(p)

    # LIVE scan
    alerts_sent_scan = 0
    stop_scan = False
    for g in games:
        if stop_scan:
            break
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
        except Exception as e:
            log.warning("No pude leer boxscore gameId=%s: %s", gid, e)
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
                    if stop_scan:
                        break
                    actual = pts if pr.tipo == "puntos" else (reb if pr.tipo == "rebotes" else ast)

                    # PRE
                    pre_sc, meta = pre_score(pid, pr.tipo, pr.line, pr.side)

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
                        final = int(clamp(0.55 * live + 0.45 * pre_sc, 0, 100))
                        key = f"{gid}|{pid}|{pr.tipo}|{pr.side}|{pr.line}"

                        if final >= FINAL_ALERT_THRESHOLD or (is_clutch and final >= FINAL_ALERT_THRESHOLD_CLUTCH):
                            if can_send_alert(state, key):
                                msg = (
                                    f"🎯 *ALERTA OVER* | *FINAL* `{final}/100` (LIVE {live} | PRE {pre_sc})\n"
                                    f"👤 *{name}*\n"
                                    f"📊 {pr.tipo.upper()} {actual}/{pr.line} (faltan {faltante:.1f})\n"
                                    f"⏱️ {status} | Q{period} {game_clock}\n"
                                    f"🧠 MIN {mins:.1f} | PF {pf} | Diff {diff}\n"
                                    f"📈 Forma: hit5 {meta['hits5']}/{meta['n5']} | hit10 {meta['hits10']}/{meta['n10']}\n"
                                )
                                await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)
                                log_signal_event({
                                    "chat_id": chat_id, "game_id": gid, "player_id": pid, "player": name,
                                    "tipo": pr.tipo, "side": pr.side, "line": pr.line, "actual": actual,
                                    "final": final, "live": live, "pre": pre_sc, "status": status, "period": period
                                })
                                alerts_sent_scan += 1
                                if alerts_sent_scan >= MAX_ALERTS_PER_SCAN:
                                    stop_scan = True
                                    break

                    else:
                        margin_under = float(pr.line) - float(actual)
                        if should_gate_by_minutes("under", pr.tipo, margin_under, mins, elapsed_min, is_blowout):
                            continue

                        live = compute_under_score(pr.tipo, margin_under, mins, pf, period, clock_sec, diff, is_clutch, is_blowout)
                        final = int(clamp(0.65 * live + 0.35 * pre_sc, 0, 100))
                        key = f"{gid}|{pid}|{pr.tipo}|{pr.side}|{pr.line}"

                        if final >= FINAL_ALERT_THRESHOLD or (is_blowout and final >= FINAL_ALERT_THRESHOLD_CLUTCH):
                            if can_send_alert(state, key):
                                msg = (
                                    f"🧊 *ALERTA UNDER* | *FINAL* `{final}/100` (LIVE {live} | PRE {pre_sc})\n"
                                    f"👤 *{name}*\n"
                                    f"📊 {pr.tipo.upper()} {actual}/{pr.line} (colchón {margin_under:.1f})\n"
                                    f"⏱️ {status} | Q{period} {game_clock}\n"
                                    f"🧠 MIN {mins:.1f} | PF {pf} | Diff {diff}\n"
                                    f"📈 Forma: hit5 {meta['hits5']}/{meta['n5']} | hit10 {meta['hits10']}/{meta['n10']}\n"
                                )
                                await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)
                                log_signal_event({
                                    "chat_id": chat_id, "game_id": gid, "player_id": pid, "player": name,
                                    "tipo": pr.tipo, "side": pr.side, "line": pr.line, "actual": actual,
                                    "final": final, "live": live, "pre": pre_sc, "status": status, "period": period
                                })
                                alerts_sent_scan += 1
                                if alerts_sent_scan >= MAX_ALERTS_PER_SCAN:
                                    stop_scan = True
                                    break

    save_alert_state(state)


# =========================
# Startup / main
# =========================
async def on_startup(app: Application):
    log.info("Bot arrancado. Background scan via /start.")
    for line in env_check_summary():
        log.info("%s", line)

async def register_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    app.post_init = on_startup

    app.add_handler(CommandHandler("start", register_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("odds", cmd_odds))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("live", cmd_live))
    app.add_handler(CommandHandler("results", cmd_results))
    app.add_handler(CommandHandler("status", cmd_status))

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
