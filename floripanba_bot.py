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
# CONFIG
# =========================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
if not TELEGRAM_TOKEN:
    raise RuntimeError("Missing TELEGRAM_TOKEN. Set it as env var (Railway Variables / Windows set).")

POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "120"))

# Persistencia opcional (manual). Polymarket es la fuente principal.
PROPS_FILE = "props.json"

ALERTS_STATE_FILE = "alerts_state.json"
IDS_CACHE_FILE = "player_ids_cache.json"
GLOG_CACHE_FILE = "gamelog_cache.json"

SEASON = os.environ.get("NBA_SEASON", "2025-26")

# Thresholds / scoring
FINAL_ALERT_THRESHOLD = 75
FINAL_ALERT_THRESHOLD_CLUTCH = 68

COOLDOWN_SECONDS = 8 * 60  # anti spam por prop
BLOWOUT_IS = 20
BLOWOUT_STRONG = 22

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

PM_HEADERS = {
    "User-Agent": NBA_HEADERS["User-Agent"],
    "Accept": "application/json",
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
    player: str              # "Stephen Curry"
    tipo: str                # "puntos" | "rebotes" | "asistencias"
    line: float              # 26.5
    side: str                # "over" | "under"
    source: str = "polymarket"  # polymarket | manual
    event: Optional[str] = None  # "Thunder vs Pistons"
    added_by: Optional[int] = None
    added_at: Optional[int] = None


# =========================
# Manual props (opcional)
# =========================
def load_manual_props() -> List[Prop]:
    raw = load_json(PROPS_FILE, {"props": []})
    out: List[Prop] = []
    for p in raw.get("props", []):
        try:
            pp = Prop(**p)
            pp.source = pp.source or "manual"
            out.append(pp)
        except Exception:
            continue
    return out

def save_manual_props(props: List[Prop]):
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
    pid = pick.get("id")
    return int(pid) if pid else None

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
# Gamelog cache + fetch (stats.nba.com)
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

    time.sleep(0.6 + random.random() * 0.4)

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
# PRE SCORE (forma 5/10)
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
        "hits5": h5, "n5": n5,
        "hits10": h10, "n10": n10,
        "avg5": round(avg5, 2), "avg10": round(avg10, 2),
        "std10": round(std10, 2), "w_margin": round(w_margin, 2),
    }
    return PRE, meta


# =========================
# Live helpers (nba_api live)
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
# Polymarket: discover NBA tag_id + events + parse player props P/R/A
# =========================
def pm_get_json(path: str, params: dict = None, timeout: int = 20):
    url = f"{GAMMA}{path}"
    r = SESSION_PM.get(url, params=params or {}, timeout=timeout)
    r.raise_for_status()
    return r.json()

def polymarket_get_nba_tag_id() -> Optional[int]:
    """
    /sports devuelve metadata y tags; buscamos NBA y nos quedamos con su tag_id.
    """
    try:
        data = pm_get_json("/sports")
    except Exception as e:
        log.warning(f"PM /sports error: {e}")
        return None

    # data suele ser lista o dict
    sports = data if isinstance(data, list) else data.get("sports") or data.get("data") or []
    for s in sports:
        name = (s.get("name") or "").strip().lower()
        league = (s.get("league") or "").strip().lower()
        if name == "nba" or league == "nba":
            tid = s.get("tag_id") or s.get("tagId") or s.get("tagID") or s.get("tag")
            try:
                return int(tid)
            except Exception:
                continue

    # fallback: buscar un tag_id dentro
    for s in sports:
        txt = json.dumps(s).lower()
        if "nba" in txt:
            tid = s.get("tag_id") or s.get("tagId")
            if tid:
                try:
                    return int(tid)
                except Exception:
                    pass
    return None

def _pm_event_start_date(ev: dict) -> Optional[date]:
    for k in ("start_date", "startDate", "start_time", "startTime", "date"):
        v = ev.get(k)
        if not v:
            continue
        # ISO
        try:
            if isinstance(v, (int, float)):
                return datetime.fromtimestamp(float(v), tz=timezone.utc).date()
            s = str(v)
            if s.endswith("Z"):
                s = s.replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).date()
        except Exception:
            continue
    return None

def polymarket_events_today_nba(limit: int = 200) -> List[dict]:
    tag_id = polymarket_get_nba_tag_id()
    if not tag_id:
        return []
    out: List[dict] = []
    offset = 0
    today_utc = datetime.now(timezone.utc).date()

    while True:
        params = {
            "tag_id": str(tag_id),
            "active": "true",
            "closed": "false",
            "limit": str(min(100, limit - len(out))),
            "offset": str(offset),
            "order": "start_date",
            "ascending": "true",
        }
        try:
            page = pm_get_json("/events", params=params, timeout=25)
        except Exception as e:
            log.warning(f"PM /events error: {e}")
            break

        events = page if isinstance(page, list) else page.get("events") or page.get("data") or []
        if not events:
            break

        for ev in events:
            d = _pm_event_start_date(ev)
            if d == today_utc:
                out.append(ev)

        if len(events) < int(params["limit"]) or len(out) >= limit:
            break
        offset += int(params["limit"])

    return out

def _infer_tipo_from_market(m: dict) -> Optional[str]:
    text = " ".join([
        str(m.get("question") or ""),
        str(m.get("title") or ""),
        str(m.get("marketType") or ""),
        str(m.get("sportsMarketType") or ""),
        str(m.get("group") or ""),
    ]).lower()

    # Polymarket suele usar "Points/Assists/Rebounds" en title/question
    if "point" in text or "pts" in text:
        return "puntos"
    if "assist" in text or "ast" in text:
        return "asistencias"
    if "rebound" in text or "reb" in text:
        return "rebotes"
    return None

def _extract_player_from_market(m: dict) -> Optional[str]:
    q = (m.get("question") or m.get("title") or "").strip()
    if not q:
        return None
    # Limpieza básica: quitar sufijos típicos
    q2 = re.sub(r"\b(points?|assists?|rebounds?)\b.*$", "", q, flags=re.IGNORECASE).strip()
    # Si quedó algo coherente, usarlo
    if len(q2.split()) >= 2:
        return q2
    # fallback: usar el original si parece nombre
    if len(q.split()) >= 2:
        return q
    return None

def _parse_ou_line_from_outcome_name(name: str) -> Optional[Tuple[str, float]]:
    """
    "Over 6.5" / "Under 6.5"
    """
    s = (name or "").strip()
    m = re.match(r"^(Over|Under)\s+([0-9]+(?:\.[0-9]+)?)$", s, flags=re.IGNORECASE)
    if not m:
        return None
    side = m.group(1).lower()
    line = float(m.group(2))
    return side, line

def polymarket_player_props_today() -> List[Prop]:
    """
    Devuelve lista de Props (P/R/A) para los partidos NBA de HOY (UTC),
    parseando markets con outcomes Over/Under.
    """
    events = polymarket_events_today_nba(limit=250)
    if not events:
        return []

    props_out: List[Prop] = []
    for ev in events:
        ev_title = ev.get("title") or ev.get("name") or ev.get("slug") or "NBA Event"
        markets = ev.get("markets") or ev.get("Markets") or []
        if not isinstance(markets, list):
            continue

        for mkt in markets:
            tipo = _infer_tipo_from_market(mkt)
            if tipo not in ("puntos", "rebotes", "asistencias"):
                continue

            outcomes = mkt.get("outcomes") or mkt.get("Outcomes") or []
            if not isinstance(outcomes, list) or len(outcomes) < 2:
                continue

            # Muchos markets traen outcomes como dicts con "name"
            parsed = []
            for oc in outcomes:
                oc_name = oc.get("name") if isinstance(oc, dict) else str(oc)
                p = _parse_ou_line_from_outcome_name(oc_name)
                if p:
                    parsed.append(p)

            # necesitamos OVER y UNDER con la misma línea
            if len(parsed) < 2:
                continue

            # tomar la línea más común
            lines = [ln for _, ln in parsed]
            line = max(set(lines), key=lines.count)

            player = _extract_player_from_market(mkt)
            if not player:
                continue

            # Emitimos ambos lados siempre (Over/Under), como pediste
            props_out.append(Prop(player=player, tipo=tipo, side="over", line=line, source="polymarket", event=str(ev_title)))
            props_out.append(Prop(player=player, tipo=tipo, side="under", line=line, source="polymarket", event=str(ev_title)))

    # Dedupe fuerte
    uniq: Dict[str, Prop] = {}
    for p in props_out:
        k = f"{p.event}|{p.player.lower()}|{p.tipo}|{p.side}|{p.line}"
        uniq[k] = p

    return list(uniq.values())


# =========================
# Build final prop universe (Polymarket + manual optional)
# =========================
def load_all_props_universe() -> List[Prop]:
    pm = polymarket_player_props_today()
    manual = load_manual_props()

    # Dedupe manual vs pm
    uniq: Dict[str, Prop] = {}
    for p in pm + manual:
        k = f"{(p.event or '').lower()}|{p.player.lower()}|{p.tipo}|{p.side}|{float(p.line)}"
        uniq[k] = p
    return list(uniq.values())


# =========================
# Telegram commands
# =========================
HELP_TEXT = (
    "🧠 *NBA Interactive Bot*\n\n"
    "Comandos:\n"
    "• `/odds`  → props NBA (P/R/A) en Polymarket para HOY\n"
    "• `/live`  → estado en vivo + scoring para props de hoy\n"
    "• `/add Nombre | tipo | side | linea` → (opcional) agregar manual\n"
    "• `/help` → ayuda\n"
)

def parse_add(text: str) -> Optional[Prop]:
    """
    /add Nombre | tipo | side | linea
    """
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

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)

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
    for existing in props:
        if (existing.player.lower() == p.player.lower()
            and existing.tipo == p.tipo and existing.side == p.side and float(existing.line) == float(p.line)):
            await update.message.reply_text("✅ Ya estaba agregado en props.json.")
            return

    props.append(p)
    save_manual_props(props)

    await update.message.reply_text(
        f"✅ Agregado (manual):\n• {p.player} — {p.tipo.upper()} {p.side.upper()} {p.line}",
    )

async def cmd_odds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        props = polymarket_player_props_today()
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error Polymarket: {e}")
        return

    if not props:
        await update.message.reply_text("No pude parsear props P/R/A desde Polymarket (0 encontrados).")
        return

    # agrupar por evento
    by_event: Dict[str, List[Prop]] = {}
    for p in props:
        by_event.setdefault(p.event or "NBA", []).append(p)

    lines = ["🟣 *Polymarket NBA (P/R/A) — HOY*"]
    # mostrar resumen: jugadores únicos por partido
    for ev, ps in list(by_event.items())[:10]:
        players_unique = len(set(p.player.lower() for p in ps))
        markets_unique = len(set((p.player.lower(), p.tipo, p.line) for p in ps))
        lines.append(f"\n🏀 *{ev}*")
        lines.append(f"• Jugadores con props: `{players_unique}` | Markets: `{markets_unique}`")

    lines.append("\nTip: `/live` te calcula score LIVE+PRE y dispara alertas automáticamente.")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def cmd_live(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        props = load_all_props_universe()
    except Exception:
        props = []

    if not props:
        await update.message.reply_text("No pude cargar props de Polymarket (0).")
        return

    # players in scope -> pid
    pid_map: Dict[int, str] = {}
    props_by_pid: Dict[int, List[Prop]] = {}

    for p in props:
        pid = get_pid_for_name(p.player)
        if not pid:
            continue
        pid_map[pid] = p.player
        props_by_pid.setdefault(pid, []).append(p)

    # live games
    try:
        games = scoreboard.ScoreBoard().get_dict()["scoreboard"]["games"]
    except Exception as e:
        await update.message.reply_text(f"⚠️ No pude leer scoreboard: {e}")
        return

    out = []
    found_any = False

    for g in games:
        if g.get("gameStatus") != 2:
            continue

        gid = g.get("gameId")
        status = g.get("gameStatusText", "")
        period = int(g.get("period", 0) or 0)
        clock = g.get("gameClock", "") or ""

        home = g.get("homeTeam", {})
        away = g.get("awayTeam", {})
        diff = abs(int(home.get("score", 0)) - int(away.get("score", 0)))
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
                if pid not in props_by_pid:
                    continue

                found_any = True
                name = pid_map.get(pid, "Player")
                stats = pl.get("statistics", {})
                pts = stats.get("points", 0) or 0
                reb = stats.get("reboundsTotal", 0) or 0
                ast = stats.get("assists", 0) or 0
                pf = stats.get("foulsPersonal", 0) or 0
                mins = parse_minutes(stats.get("minutes", ""))

                out.append(f"🏀 *{name}* — {status} (Q{period} {clock}) | MIN {mins:.1f} | Diff {diff}")

                # listar props de ese jugador y dar score (sin spamear demasiado)
                ps = props_by_pid.get(pid, [])
                for pr in ps[:6]:
                    actual = pts if pr.tipo == "puntos" else (reb if pr.tipo == "rebotes" else ast)

                    pre, meta = pre_score(pid, pr.tipo, pr.line, pr.side)

                    if pr.side == "over":
                        faltante = float(pr.line) - float(actual)
                        live = compute_over_score(pr.tipo, faltante, mins, pf, period, clock_sec, diff, is_clutch, is_blowout)
                        final = int(clamp(0.55 * live + 0.45 * pre, 0, 100))
                        out.append(
                            f"  • {pr.tipo} OVER {pr.line}: {actual} (faltan {faltante:.1f}) | "
                            f"FINAL {final}/100 (PRE {pre} hit10 {meta['hits10']}/{meta['n10']})"
                        )
                    else:
                        margin_under = float(pr.line) - float(actual)
                        live = compute_under_score(pr.tipo, margin_under, mins, pf, period, clock_sec, diff, is_clutch, is_blowout)
                        final = int(clamp(0.65 * live + 0.35 * pre, 0, 100))
                        out.append(
                            f"  • {pr.tipo} UNDER {pr.line}: {actual} (colchón {margin_under:.1f}) | "
                            f"FINAL {final}/100 (PRE {pre} hit10 {meta['hits10']}/{meta['n10']})"
                        )

                out.append("")

    if not found_any:
        await update.message.reply_text("No hay partidos en vivo (o ninguno de los jugadores con props está jugando ahora).")
        return

    msg = "\n".join(out)
    if len(msg) > 3500:
        msg = msg[:3500] + "\n…(recortado)"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


# =========================
# Background scan: alerts automáticas para TODOS los props del día
# =========================
async def background_scan(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id

    props = []
    try:
        props = load_all_props_universe()
    except Exception:
        props = []

    if not props:
        return

    state = load_alert_state()

    # pid -> props
    by_pid: Dict[int, List[Prop]] = {}
    for p in props:
        pid = get_pid_for_name(p.player)
        if pid:
            by_pid.setdefault(pid, []).append(p)

    # games
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
                                    f"👤 *{name}*  ({pr.event or 'NBA'})\n"
                                    f"📊 {pr.tipo.upper()} {actual}/{pr.line} (faltan {faltante:.1f})\n"
                                    f"⏱️ {status} | Q{period} {game_clock}\n"
                                    f"🧠 MIN {mins:.1f} | PF {pf} | Diff {diff}\n"
                                    f"📈 Forma: hit10 {meta['hits10']}/{meta['n10']} | avg10 {meta['avg10']} | std10 {meta['std10']}\n"
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
                                    f"👤 *{name}*  ({pr.event or 'NBA'})\n"
                                    f"📊 {pr.tipo.upper()} {actual}/{pr.line} (colchón {margin_under:.1f})\n"
                                    f"⏱️ {status} | Q{period} {game_clock}\n"
                                    f"🧠 MIN {mins:.1f} | PF {pf} | Diff {diff}\n"
                                    f"📈 Forma: hit10 {meta['hits10']}/{meta['n10']} | avg10 {meta['avg10']} | std10 {meta['std10']}\n"
                                )
                                await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)

    save_alert_state(state)


# =========================
# Startup + register background job per chat
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
    await cmd_start(update, context)


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", register_job), group=0)
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("odds", cmd_odds))
    app.add_handler(CommandHandler("live", cmd_live))
    app.add_handler(CommandHandler("add", cmd_add))

    log.info("Bot arrancado.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
