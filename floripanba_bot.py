import os
import re
import json
import time
import math
import random
import asyncio
import logging
import sqlite3
import unicodedata
from dataclasses import dataclass, asdict, field
from typing import Dict, List, Optional, Tuple
from datetime import date, datetime

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from telegram import Update, BotCommand
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

logging.basicConfig(
level=logging.INFO,
format=”%(asctime)s [%(levelname)s] %(name)s: %(message)s”
)
log = logging.getLogger(“nba-bot”)

TELEGRAM_TOKEN = os.environ.get(“TELEGRAM_TOKEN”, “”).strip()
if not TELEGRAM_TOKEN:
raise RuntimeError(“Missing TELEGRAM_TOKEN env var”)

POLL_SECONDS = int(os.environ.get(“POLL_SECONDS”, “120”))

PROPS_FILE         = “props.json”
ALERTS_STATE_FILE  = “alerts_state.json”
IDS_CACHE_FILE     = “player_ids_cache.json”
GLOG_CACHE_FILE    = “gamelog_cache.json”
DB_FILE            = os.environ.get(“DB_FILE”, “nba_signals.db”)   # SQLite

SEASON = os.environ.get(“NBA_SEASON”, “2025-26”)

FINAL_ALERT_THRESHOLD       = 75
FINAL_ALERT_THRESHOLD_CLUTCH = 68

# Umbrales de edge para señales (Día 4-5)

EDGE_THRESH_PREGAME  = float(os.environ.get(“EDGE_THRESH_PREGAME”,  “4.0”))   # % mínimo
EDGE_THRESH_INGAME   = float(os.environ.get(“EDGE_THRESH_INGAME”,   “6.0”))
CONF_THRESH_PREGAME  = int(os.environ.get(“CONF_THRESH_PREGAME”,   “65”))
MAX_SIGNALS_DAY      = int(os.environ.get(“MAX_SIGNALS_DAY”,        “20”))
MAX_SIGNALS_PLAYER   = int(os.environ.get(“MAX_SIGNALS_PLAYER”,      “2”))

COOLDOWN_SECONDS  = 8 * 60
BLOWOUT_IS        = 20
BLOWOUT_STRONG    = 22

THRESH_POINTS_OVER  = (0.5, 4.0)
THRESH_REB_AST_OVER = (0.5, 1.5)

MIN_MINUTES_POINTS  = 10.0
MIN_MINUTES_REB_AST = 14.0

STAT_COL    = {“puntos”: “PTS”, “rebotes”: “REB”, “asistencias”: “AST”}
STD_CAP     = {“puntos”: 8.0,  “rebotes”: 4.0,  “asistencias”: 3.0}
MARGIN_CAP  = {“puntos”: 8.0,  “rebotes”: 3.0,  “asistencias”: 3.0}

GAMMA = “https://gamma-api.polymarket.com”

# ================================================================

# DÍA 1 - ESQUEMA CANÓNICO DE SEÑAL

# ================================================================

@dataclass
class Signal:
“”“Señal completa accionable, con toda la trazabilidad del roadmap.”””
signal_id:    str             # UUID corto determinístico
ts:           int             # unix timestamp de creación
kind:         str             # “pregame” | “ingame”
player:       str             # nombre normalizado
player_id:    Optional[int]   # NBA player ID
market:       str             # “puntos” | “rebotes” | “asistencias”
line:         float
side:         str             # “over” | “under”
game_slug:    str
implied_prob: float           # probabilidad implícita del mercado (0-1)
model_prob:   float           # probabilidad del modelo (0-1)
edge:         float           # model_prob - implied_prob (porcentual)
confidence:   int             # 0-100
reason_codes: List[str]       # [“hit_rate_high”, “pace_favorable”, …]
risk_flags:   List[str]       # [“foul_risk”, “blowout_possible”, …]
level:        str             # “watch” | “entry” | “avoid”
result:       Optional[str]   = None   # “win”|“loss”|“push”|None
actual_stat:  Optional[float] = None
resolved_at:  Optional[int]   = None
market_id:    str             = “”
source:       str             = “polymarket”

def _signal_id(player: str, market: str, line: float, side: str, kind: str) -> str:
“”“ID determinístico: misma señal = mismo ID (evita duplicados).”””
today = date.today().isoformat()
raw = f”{today}|{player.lower()}|{market}|{line}|{side}|{kind}”
import hashlib
return hashlib.md5(raw.encode()).hexdigest()[:10].upper()

# ================================================================

# DÍA 2 - NORMALIZACIÓN ROBUSTA DE NOMBRES

# ================================================================

# Sufijos a eliminar

_NAME_SUFFIXES = re.compile(r’\b(jr.?|sr.?|ii|iii|iv)\s*$’, re.IGNORECASE)

# Caracteres especiales → ASCII

def _strip_accents(s: str) -> str:
return ‘’.join(
c for c in unicodedata.normalize(‘NFD’, s)
if unicodedata.category(c) != ‘Mn’
)

def normalize_name(name: str) -> str:
“””
Normalización canónica de nombre de jugador.
‘Nikola Jokić’ → ‘nikola jokic’
‘LeBron James Jr.’ → ‘lebron james’
“””
if not name:
return “”
n = _strip_accents(name.strip())
n = _NAME_SUFFIXES.sub(’’, n).strip()
n = re.sub(r’[^a-z0-9 ]’, ‘’, n.lower())
n = re.sub(r’\s+’, ’ ’, n).strip()
return n

# Alias manuales: nombre normalizado Polymarket → nombre normalizado NBA

_NAME_ALIASES: Dict[str, str] = {
“nikola jokic”:        “nikola jokic”,
“nikola jovic”:        “nikola jovic”,
“lebron james”:        “lebron james”,
“shai gilgeous alexander”: “shai gilgeous-alexander”,
“oj anunoby”:          “og anunoby”,
“jaren jackson”:       “jaren jackson jr”,
“wendell carter”:      “wendell carter jr”,
“gary trent”:          “gary trent jr”,
“kelly oubre”:         “kelly oubre jr”,
“marvin bagley”:       “marvin bagley iii”,
“jabari smith”:        “jabari smith jr”,
“kevin knox”:          “kevin knox ii”,
“nic claxton”:         “nicolas claxton”,
“mo bamba”:            “mohamed bamba”,
“naz reid”:            “nazreid”,           # override si aplica
“tj mcconnell”:        “t.j. mcconnell”,
“tj warren”:           “t.j. warren”,
“pj tucker”:           “p.j. tucker”,
“pj washington”:       “p.j. washington”,
“cj mccollum”:         “c.j. mccollum”,
“cj mccollum”:         “c.j. mccollum”,
“dj augustin”:         “d.j. augustin”,
“gg jackson”:          “gregory jackson ii”,
“gg jackson ii”:       “gregory jackson ii”,
“cam thomas”:          “cameron thomas”,
“cam johnson”:         “cameron johnson”,
}

def resolve_player_name(raw_name: str) -> str:
“””
Dado un nombre crudo de Polymarket, devuelve el nombre canónico
para buscar en la NBA API.
“””
n = normalize_name(raw_name)
return _NAME_ALIASES.get(n, n)

# Matching fuzzy por apellido (fallback cuando no hay match exacto)

def fuzzy_match_player(raw_name: str, nba_players_list: List[dict]) -> Optional[dict]:
“”“Busca jugador por apellido cuando el nombre exacto falla.”””
n = normalize_name(raw_name)
parts = n.split()
if not parts:
return None

```
last = parts[-1]
candidates = []
for p in nba_players_list:
    full = normalize_name(p.get("full_name", ""))
    if last in full.split():
        candidates.append(p)

if len(candidates) == 1:
    return candidates[0]

# Si hay varios, intentar también primer nombre
if len(parts) >= 2:
    first = parts[0]
    for c in candidates:
        full = normalize_name(c.get("full_name", ""))
        if first in full.split():
            return c

return None
```

# ================================================================

# DÍA 3 - PERSISTENCIA SQLite

# ================================================================

def db_connect() -> sqlite3.Connection:
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
conn.row_factory = sqlite3.Row
return conn

def db_init():
“”“Crea las tablas si no existen.”””
conn = db_connect()
cur  = conn.cursor()

```
cur.executescript("""
CREATE TABLE IF NOT EXISTS signals (
    signal_id    TEXT PRIMARY KEY,
    ts           INTEGER NOT NULL,
    kind         TEXT NOT NULL,
    player       TEXT NOT NULL,
    player_id    INTEGER,
    market       TEXT NOT NULL,
    line         REAL NOT NULL,
    side         TEXT NOT NULL,
    game_slug    TEXT NOT NULL,
    implied_prob REAL,
    model_prob   REAL,
    edge         REAL,
    confidence   INTEGER,
    reason_codes TEXT,
    risk_flags   TEXT,
    level        TEXT,
    result       TEXT,
    actual_stat  REAL,
    resolved_at  INTEGER,
    market_id    TEXT,
    source       TEXT
);

CREATE TABLE IF NOT EXISTS markets_snapshot (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           INTEGER NOT NULL,
    game_slug    TEXT NOT NULL,
    player       TEXT NOT NULL,
    market       TEXT NOT NULL,
    line         REAL NOT NULL,
    implied_over REAL,
    implied_under REAL,
    source       TEXT
);

CREATE TABLE IF NOT EXISTS player_game_state (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         INTEGER NOT NULL,
    game_slug  TEXT NOT NULL,
    player_id  INTEGER NOT NULL,
    player     TEXT NOT NULL,
    pts        REAL, reb REAL, ast REAL,
    mins       REAL, fouls INTEGER,
    period     INTEGER, clock TEXT,
    score_diff INTEGER
);

CREATE TABLE IF NOT EXISTS daily_risk (
    date_str      TEXT PRIMARY KEY,
    signals_sent  INTEGER DEFAULT 0,
    player_counts TEXT DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_signals_ts     ON signals(ts);
CREATE INDEX IF NOT EXISTS idx_signals_player ON signals(player);
CREATE INDEX IF NOT EXISTS idx_signals_result ON signals(result);
""")

conn.commit()
conn.close()
log.info(f"DB inicializada: {DB_FILE}")
```

def db_save_signal(sig: Signal):
conn = db_connect()
try:
conn.execute(”””
INSERT OR IGNORE INTO signals
(signal_id, ts, kind, player, player_id, market, line, side,
game_slug, implied_prob, model_prob, edge, confidence,
reason_codes, risk_flags, level, result, actual_stat,
resolved_at, market_id, source)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
“””, (
sig.signal_id, sig.ts, sig.kind, sig.player, sig.player_id,
sig.market, sig.line, sig.side, sig.game_slug,
sig.implied_prob, sig.model_prob, sig.edge, sig.confidence,
json.dumps(sig.reason_codes), json.dumps(sig.risk_flags),
sig.level, sig.result, sig.actual_stat, sig.resolved_at,
sig.market_id, sig.source
))
conn.commit()
except Exception as e:
log.warning(f”db_save_signal error: {e}”)
finally:
conn.close()

def db_resolve_signal(signal_id: str, result: str, actual_stat: float):
conn = db_connect()
try:
conn.execute(”””
UPDATE signals SET result=?, actual_stat=?, resolved_at=?
WHERE signal_id=?
“””, (result, actual_stat, int(time.time()), signal_id))
conn.commit()
finally:
conn.close()

def db_get_signals(days: int = 30, player: str = None,
kind: str = None, result: str = None) -> List[dict]:
conn = db_connect()
cutoff = int(time.time()) - days * 86400
where  = [“ts >= ?”]
params: list = [cutoff]
if player:
where.append(“player LIKE ?”)
params.append(f”%{normalize_name(player)}%”)
if kind:
where.append(“kind = ?”)
params.append(kind)
if result:
where.append(“result = ?”)
params.append(result)

```
sql = f"SELECT * FROM signals WHERE {' AND '.join(where)} ORDER BY ts DESC"
rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
conn.close()
return rows
```

def db_save_market_snapshot(game_slug: str, player: str, market: str,
line: float, implied_over: float,
implied_under: float, source: str = “polymarket”):
conn = db_connect()
try:
conn.execute(”””
INSERT INTO markets_snapshot
(ts, game_slug, player, market, line, implied_over, implied_under, source)
VALUES (?,?,?,?,?,?,?,?)
“””, (int(time.time()), game_slug, normalize_name(player),
market, line, implied_over, implied_under, source))
conn.commit()
except Exception as e:
log.warning(f”db_save_market_snapshot: {e}”)
finally:
conn.close()

def db_save_player_state(game_slug: str, player_id: int, player: str,
pts, reb, ast, mins, fouls, period, clock, score_diff):
conn = db_connect()
try:
conn.execute(”””
INSERT INTO player_game_state
(ts, game_slug, player_id, player, pts, reb, ast, mins,
fouls, period, clock, score_diff)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
“””, (int(time.time()), game_slug, player_id,
normalize_name(player), pts, reb, ast,
mins, fouls, period, clock, score_diff))
conn.commit()
except Exception as e:
log.warning(f”db_save_player_state: {e}”)
finally:
conn.close()

# ── Gestión de riesgo diario ──

def db_get_daily_risk() -> dict:
today = date.today().isoformat()
conn  = db_connect()
row   = conn.execute(
“SELECT * FROM daily_risk WHERE date_str=?”, (today,)
).fetchone()
conn.close()
if not row:
return {“date_str”: today, “signals_sent”: 0, “player_counts”: {}}
return {
“date_str”:     row[“date_str”],
“signals_sent”: row[“signals_sent”],
“player_counts”: json.loads(row[“player_counts”] or “{}”),
}

def db_increment_risk(player: str):
today = date.today().isoformat()
risk  = db_get_daily_risk()
risk[“signals_sent”] += 1
pc = risk[“player_counts”]
pc[player] = pc.get(player, 0) + 1
conn = db_connect()
conn.execute(”””
INSERT INTO daily_risk (date_str, signals_sent, player_counts)
VALUES (?,?,?)
ON CONFLICT(date_str) DO UPDATE
SET signals_sent=excluded.signals_sent,
player_counts=excluded.player_counts
“””, (today, risk[“signals_sent”], json.dumps(pc)))
conn.commit()
conn.close()

def risk_check(player: str) -> Tuple[bool, str]:
“””
Retorna (ok, reason). Si ok=False no se debe enviar la señal.
“””
risk = db_get_daily_risk()
if risk[“signals_sent”] >= MAX_SIGNALS_DAY:
return False, f”límite diario alcanzado ({MAX_SIGNALS_DAY} señales)”
pc = risk[“player_counts”]
if pc.get(player, 0) >= MAX_SIGNALS_PLAYER:
return False, f”máx señales por jugador ({MAX_SIGNALS_PLAYER}) para {player}”
return True, “”

# ================================================================

# DÍA 4 - MODELO PROBABILÍSTICO PREGAME

# ================================================================

def _normal_cdf(x: float) -> float:
“”“CDF de distribución normal estándar (aproximación).”””
return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def model_probability(avg: float, std: float, line: float, side: str) -> float:
“””
Convierte proyección estadística en probabilidad.
Usa distribución normal con media=avg y desviación=std.
“””
if std <= 0:
std = max(avg * 0.20, 1.0)   # fallback: 20% de la media
z = (line - avg) / std
p_over = 1.0 - _normal_cdf(z)
return p_over if side == “over” else 1.0 - p_over

def implied_probability(pre_score: int, side: str) -> float:
“””
Estima probabilidad implícita del mercado desde el PRE score.
PRE=50 → 50%, PRE=80 → ~72%, PRE=20 → ~28%.
Para UNDER invertimos.
“””
# Mapeo suave: PRE 0-100 → probabilidad 25-75%
p = 0.25 + (pre_score / 100.0) * 0.50
return p if side == “over” else 1.0 - p

def compute_edge(model_prob: float, implied_prob: float) -> float:
“”“Edge = (model_prob - implied_prob) * 100 en porcentaje.”””
return round((model_prob - implied_prob) * 100.0, 2)

def build_pregame_signal(pid: int, player: str, market: str, line: float,
side: str, game_slug: str, market_id: str = “”,
opp_tricode: str = “”, is_home: bool = True) -> Optional[Signal]:
“””
Construye una señal pregame completa con edge y reason_codes.
Retorna None si no supera los umbrales.
“””
v10 = last_n_values(pid, market, 10)
v5  = last_n_values(pid, market, 5)
if len(v10) < 3:
return None

```
avg10 = sum(v10) / len(v10)
std10 = stdev(v10)
avg5  = (sum(v5) / len(v5)) if v5 else avg10

# Probabilidad del modelo
m_prob = model_probability(avg10, std10, line, side)

# PRE score base
pre, meta = pre_score(pid, market, line, side)

# Probabilidad implícita estimada
i_prob = implied_probability(pre, side)

edge = compute_edge(m_prob, i_prob)
conf = pre   # usamos PRE como confidence base

# Reason codes
reasons: List[str] = []
risk_flags: List[str] = []

h5,  n5  = hit_counts(v5,  line, side)
h10, n10 = hit_counts(v10, line, side)

if n10 and h10/n10 >= 0.70:
    reasons.append("hit_rate_alto_10j")
if n5  and h5/n5  >= 0.80:
    reasons.append("racha_fuerte_5j")

gap = (avg10 - line) if side == "over" else (line - avg10)
if gap > 2:
    reasons.append("promedio_supera_linea")
elif gap < -2:
    risk_flags.append("promedio_bajo_linea")

trend = trend_arrow(v10)
if trend == "📈":
    reasons.append("tendencia_alcista")
elif trend == "📉":
    risk_flags.append("tendencia_bajista")

# Splits H/A
splits = home_away_splits(pid, market)
loc = "home" if is_home else "away"
loc_avg = splits.get(f"{loc}_avg")
if loc_avg:
    loc_gap = (loc_avg - line) if side == "over" else (line - loc_avg)
    if loc_gap > 1.5:
        reasons.append(f"split_{loc}_favorable")
    elif loc_gap < -1.5:
        risk_flags.append(f"split_{loc}_desfavorable")

# Back-to-back
if is_back_to_back(pid):
    risk_flags.append("back_to_back")
    conf -= 8

# Contexto defensivo
if opp_tricode:
    ctx = get_defensive_context(opp_tricode, market)
    dr  = ctx.get("def_rank")
    if dr:
        if side == "over" and dr >= 25:
            reasons.append("rival_defensa_debil")
            conf += 5
        elif side == "over" and dr <= 5:
            risk_flags.append("rival_defensa_elite")
            conf -= 5

conf = int(clamp(conf, 0, 100))

# Nivel de señal
if edge >= EDGE_THRESH_PREGAME and conf >= CONF_THRESH_PREGAME:
    level = "entry"
elif edge >= EDGE_THRESH_PREGAME * 0.6 and conf >= CONF_THRESH_PREGAME - 10:
    level = "watch"
else:
    return None   # no supera umbrales

return Signal(
    signal_id    = _signal_id(player, market, line, side, "pregame"),
    ts           = int(time.time()),
    kind         = "pregame",
    player       = normalize_name(player),
    player_id    = pid,
    market       = market,
    line         = line,
    side         = side,
    game_slug    = game_slug,
    implied_prob = round(i_prob, 3),
    model_prob   = round(m_prob, 3),
    edge         = edge,
    confidence   = conf,
    reason_codes = reasons,
    risk_flags   = risk_flags,
    level        = level,
    market_id    = market_id,
)
```

# ================================================================

# DÍA 5 - FORMATO ESTÁNDAR DE ALERTA + DASHBOARD

# ================================================================

def format_signal_message(sig: Signal) -> str:
“”“Formato canónico de alerta según el roadmap.”””
level_emoji = {“entry”: “🟢 ENTRY”, “watch”: “🟡 WATCH”, “avoid”: “🔴 AVOID”}.get(sig.level, “⚪”)
tipo_icon   = {“puntos”: “PTS”, “rebotes”: “REB”, “asistencias”: “AST”}.get(sig.market, sig.market.upper())
side_str    = “Over” if sig.side == “over” else “Under”

```
impl_pct  = round(sig.implied_prob * 100, 1)
model_pct = round(sig.model_prob   * 100, 1)
edge_sign = "+" if sig.edge >= 0 else ""

reasons_str = "\n".join(f"  ✅ {r.replace('_',' ')}" for r in sig.reason_codes) or "  -"
risks_str   = "\n".join(f"  ⚠️ {r.replace('_',' ')}" for r in sig.risk_flags)   or "  -"

matchup = _slug_to_matchup(sig.game_slug)

return (
    f"{level_emoji} | NBA Props\n"
    f"{'─'*30}\n"
    f"👤 *{sig.player.title()}*\n"
    f"📌 {tipo_icon} {side_str} `{sig.line}` - _{matchup}_\n\n"
    f"📊 Prob implícita: `{impl_pct}%`\n"
    f"🤖 Prob modelo:    `{model_pct}%`\n"
    f"⚡ Edge:           `{edge_sign}{sig.edge}%`\n"
    f"🎯 Confianza:      `{sig.confidence}/100`\n\n"
    f"*Razones:*\n{reasons_str}\n"
    f"*Riesgos:*\n{risks_str}\n"
    f"{'─'*30}\n"
    f"`#{sig.signal_id}` · {sig.kind}"
)
```

async def cmd_signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
“””
/signals - muestra señales pregame activas del día con edge ≥ umbral.
Calcula en tiempo real y guarda en SQLite.
“””
msg_wait = await update.message.reply_text(
“🔍 *Buscando señales pregame…*”, parse_mode=ParseMode.MARKDOWN
)

```
props_pm = await asyncio.to_thread(polymarket_props_today_from_scoreboard)
if not props_pm:
    await msg_wait.edit_text("❌ Sin props disponibles.")
    return

# Solo OVER de partidos pre-partido, sin duplicados
seen = set()
candidates = []
for p in props_pm:
    if p.side != "over":
        continue
    key = (p.player, p.tipo, p.line)
    if key in seen:
        continue
    seen.add(key)
    candidates.append(p)

sem = asyncio.Semaphore(4)

async def _eval(p: Prop) -> Optional[Signal]:
    async with sem:
        def _inner():
            pid = get_pid_for_name(p.player)
            if not pid:
                # Intentar con nombre normalizado + alias
                resolved = resolve_player_name(p.player)
                pid = get_pid_for_name(resolved)
            if not pid:
                return None

            # Detectar rival desde slug
            slug_parts = (p.game_slug or "").replace("nba-","").split("-")
            opp_tri  = slug_parts[1].upper() if len(slug_parts) >= 2 else ""
            is_home  = False

            return build_pregame_signal(
                pid, p.player, p.tipo, p.line, "over",
                p.game_slug, p.market_id, opp_tri, is_home
            )
        try:
            return await asyncio.wait_for(asyncio.to_thread(_inner), timeout=25.0)
        except Exception:
            return None

results = await asyncio.gather(*[_eval(p) for p in candidates[:50]])
signals = [s for s in results if s is not None]
signals.sort(key=lambda s: s.edge, reverse=True)

# Guardar en DB
for s in signals:
    db_save_signal(s)
    db_save_market_snapshot(
        s.game_slug, s.player, s.market, s.line,
        s.implied_prob, 1.0 - s.implied_prob
    )

if not signals:
    await msg_wait.edit_text(
        f"😔 Sin señales con edge ≥ {EDGE_THRESH_PREGAME}% hoy.\n"
        f"_Usa `/alertas` para ver todas las props con PRE score._",
        parse_mode=ParseMode.MARKDOWN
    )
    return

entry_sigs = [s for s in signals if s.level == "entry"]
watch_sigs = [s for s in signals if s.level == "watch"]

today_str = date.today().strftime("%d/%m/%Y")
header = (
    f"⚡ *SEÑALES PREGAME - {today_str}*\n"
    f"_{len(entry_sigs)} ENTRY · {len(watch_sigs)} WATCH_\n"
    f"_{len(signals)} señales (edge ≥ {EDGE_THRESH_PREGAME * 0.6:.1f}%)_\n"
    f"{'─'*32}"
)
await update.message.reply_text(header, parse_mode=ParseMode.MARKDOWN)

# Enviar ENTRY primero, luego WATCH
for sig in signals[:12]:
    msg_txt = format_signal_message(sig)
    try:
        await update.message.reply_text(msg_txt, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        log.warning(f"Error enviando señal: {e}")
    await asyncio.sleep(0.4)

await msg_wait.delete()
```

async def cmd_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
“””
/dashboard - métricas de desempeño: hit rate, edge, ROI por mercado.
“””
args = context.args or []
days = int(args[0]) if args and args[0].isdigit() else 30

```
def _calc():
    sigs = db_get_signals(days=days)
    resolved = [s for s in sigs if s.get("result") in ("win","loss","push")]
    wins   = [s for s in resolved if s["result"] == "win"]
    losses = [s for s in resolved if s["result"] == "loss"]

    total_res = len(wins) + len(losses)
    win_rate  = round(len(wins) / total_res * 100, 1) if total_res else 0.0

    # Edge promedio de señales enviadas
    avg_edge = round(sum(s["edge"] for s in sigs if s.get("edge")) / len(sigs), 2) if sigs else 0.0

    # CLV proxy: edge promedio de señales ganadoras
    clv = round(sum(s["edge"] for s in wins if s.get("edge")) / len(wins), 2) if wins else 0.0

    # Por mercado
    by_market = {}
    for m in ("puntos", "rebotes", "asistencias"):
        m_res = [s for s in resolved if s["market"] == m]
        m_win = sum(1 for s in m_res if s["result"] == "win")
        by_market[m] = {"w": m_win, "total": len(m_res)}

    # Por tipo (pregame/ingame)
    by_kind = {}
    for k in ("pregame", "ingame"):
        k_res = [s for s in resolved if s["kind"] == k]
        k_win = sum(1 for s in k_res if s["result"] == "win")
        by_kind[k] = {"w": k_win, "total": len(k_res)}

    # Risk del día
    risk = db_get_daily_risk()

    return {
        "total": len(sigs), "resolved": total_res,
        "wins": len(wins), "losses": len(losses),
        "pending": len(sigs) - total_res,
        "win_rate": win_rate, "avg_edge": avg_edge, "clv": clv,
        "by_market": by_market, "by_kind": by_kind,
        "risk": risk,
    }

data = await asyncio.to_thread(_calc)

roi_emoji = "🟢" if data["win_rate"] >= 55 else ("🔴" if data["win_rate"] < 45 else "🟡")
today_str = date.today().strftime("%d/%m/%Y")

tipo_icon = {"puntos":"🏀","rebotes":"💪","asistencias":"🎯"}

mkt_lines = []
for m, st in data["by_market"].items():
    rate = round(st["w"]/st["total"]*100, 1) if st["total"] else 0
    mkt_lines.append(f"  {tipo_icon.get(m,'•')} {m.capitalize()}: `{st['w']}/{st['total']}` ({rate}%)")

kind_lines = []
for k, st in data["by_kind"].items():
    rate = round(st["w"]/st["total"]*100, 1) if st["total"] else 0
    kind_lines.append(f"  {'🕐' if k=='pregame' else '🔴'} {k.capitalize()}: `{st['w']}/{st['total']}` ({rate}%)")

risk = data["risk"]
risk_bar = f"`{risk['signals_sent']}/{MAX_SIGNALS_DAY}` señales hoy"

msg = (
    f"📊 *DASHBOARD - últimos {days} días*\n"
    f"_{today_str}_\n"
    f"{'─'*32}\n\n"
    f"📝 Total señales: `{data['total']}`\n"
    f"  ✅ Win: `{data['wins']}` · ❌ Loss: `{data['losses']}` · ⏳ Pend: `{data['pending']}`\n\n"
    f"{roi_emoji} *Win Rate:* `{data['win_rate']}%`\n"
    f"⚡ *Edge promedio:* `{data['avg_edge']:+.1f}%`\n"
    f"💹 *CLV proxy:* `{data['clv']:+.1f}%`\n\n"
    f"{'─'*32}\n"
    f"*Por mercado:*\n" + "\n".join(mkt_lines) + "\n\n"
    f"*Por tipo:*\n" + "\n".join(kind_lines) + "\n\n"
    f"{'─'*32}\n"
    f"🛡️ *Riesgo hoy:* {risk_bar}\n"
    f"_Usa `/signals` para señales · `/resultado ID WIN stat` para cerrar_"
)

await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
```

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
“””
/status - health check: DB, cache, API, señales activas.
“””
def _check():
checks = {}
# DB
try:
conn = db_connect()
n = conn.execute(“SELECT COUNT(*) FROM signals”).fetchone()[0]
conn.close()
checks[“db”] = f”✅ SQLite OK ({n} señales)”
except Exception as e:
checks[“db”] = f”❌ DB error: {e}”

```
    # Cache props
    pm_count = len(PM_CACHE.get("props", []))
    pm_age   = int(time.time()) - PM_CACHE.get("ts", 0)
    checks["pm_cache"] = f"✅ {pm_count} props ({pm_age//60}min)" if pm_count else "⚠️ Cache vacío"

    # Cache gamelog
    try:
        g = load_json(GLOG_CACHE_FILE, {})
        checks["glog"] = f"✅ {len(g)} jugadores cacheados"
    except Exception:
        checks["glog"] = "⚠️ Sin cache gamelog"

    # Riesgo diario
    risk = db_get_daily_risk()
    checks["risk"] = (
        f"{'✅' if risk['signals_sent'] < MAX_SIGNALS_DAY else '🔴'} "
        f"{risk['signals_sent']}/{MAX_SIGNALS_DAY} señales hoy"
    )

    return checks

data = await asyncio.to_thread(_check)
today_str = date.today().strftime("%d/%m/%Y")
lines = [f"🔧 *STATUS - {today_str}*\n"]
for k, v in data.items():
    lines.append(f"*{k}:* {v}")
lines.append(f"\n_Bot corriendo · polling cada {POLL_SECONDS}s_")
await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
```

# =========================

# HTTP sessions

# =========================

NBA_HEADERS = {
“User-Agent”: (
“Mozilla/5.0 (Windows NT 10.0; Win64; x64) “
“AppleWebKit/537.36 (KHTML, like Gecko) “
“Chrome/122.0.0.0 Safari/537.36”
),
“Accept”: “application/json, text/plain, */*”,
“Accept-Language”: “es-ES,es;q=0.9,en;q=0.8”,
“Referer”: “https://www.nba.com/”,
“Origin”: “https://www.nba.com”,
“Connection”: “keep-alive”,
“x-nba-stats-origin”: “stats”,
“x-nba-stats-token”: “true”,
}

PM_HEADERS = {
“User-Agent”: NBA_HEADERS[“User-Agent”],
“Accept”: “application/json, text/plain, */*”,
“Accept-Language”: “es-ES,es;q=0.9,en;q=0.8”,
“Origin”: “https://polymarket.com”,
“Referer”: “https://polymarket.com/”,
“Connection”: “keep-alive”,
}

def build_session(headers: dict) -> requests.Session:
s = requests.Session()
retry = Retry(
total=6,
connect=6,
read=6,
backoff_factor=1.2,
status_forcelist=(403, 408, 429, 500, 502, 503, 504),
allowed_methods=frozenset([“GET”, “POST”]),
raise_on_status=False,
)
adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
s.mount(“https://”, adapter)
s.mount(“http://”, adapter)
s.headers.update(headers)
return s

SESSION_NBA = build_session(NBA_HEADERS)
SESSION_PM = build_session(PM_HEADERS)

# =========================

# Persistence helpers

# =========================

def load_json(path: str, default):
try:
with open(path, “r”, encoding=“utf-8”) as f:
return json.load(f)
except Exception:
return default

def save_json(path: str, data):
tmp = path + “.tmp”
with open(tmp, “w”, encoding=“utf-8”) as f:
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
tipo: str                # “puntos” | “rebotes” | “asistencias”
line: float
side: str                # “over” | “under”
source: str = “manual”
game_slug: Optional[str] = None
market_id: Optional[str] = None
added_by: Optional[int] = None
added_at: Optional[int] = None

def load_props() -> List[Prop]:
raw = load_json(PROPS_FILE, {“props”: []})
out = []
for p in raw.get(“props”, []):
try:
out.append(Prop(**p))
except Exception:
continue
return out

def save_props(props: List[Prop]):
save_json(PROPS_FILE, {“props”: [asdict(p) for p in props]})

# =========================

# Player ID cache

# =========================

def obtener_id_jugador(nombre: str) -> Optional[int]:
time.sleep(0.2 + random.random() * 0.1)
res = players.find_players_by_full_name(nombre)
if not res:
return None
exact = [p for p in res if (p.get(“full_name”) or “”).lower() == nombre.lower()]
pick = exact[0] if exact else res[0]
return int(pick.get(“id”))

def load_ids_cache() -> Dict[str, int]:
return load_json(IDS_CACHE_FILE, {})

def save_ids_cache(c: Dict[str, int]):
save_json(IDS_CACHE_FILE, c)

def get_pid_for_name(name: str) -> Optional[int]:
# Normalizar y resolver alias primero
canonical = resolve_player_name(name)

```
cache = load_ids_cache()

# Buscar en cache por nombre original y canónico
for key in (name, canonical):
    if key in cache:
        return int(cache[key])

# Búsqueda exacta en NBA API con nombre canónico
pid = obtener_id_jugador(canonical)

# Fallback: búsqueda fuzzy si falló
if not pid:
    try:
        all_players = players.get_players()
        match = fuzzy_match_player(canonical, all_players)
        if match:
            pid = int(match["id"])
            log.info(f"Fuzzy match: '{name}' → '{match['full_name']}' (pid={pid})")
    except Exception as e:
        log.warning(f"Fuzzy match error para '{name}': {e}")

if pid:
    cache[name]      = int(pid)
    cache[canonical] = int(pid)
    save_ids_cache(cache)
else:
    log.warning(f"No encontré PID para: '{name}' (canonico: '{canonical}')")
return pid
```

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

```
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
```

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
if side == “over”:
hits = sum(1 for v in values if v > line)
else:
hits = sum(1 for v in values if v < line)
return hits, len(values)

def margin_values(values: List[float], line: float, side: str) -> List[float]:
if side == “over”:
return [v - line for v in values]
return [line - v for v in values]

def pre_score(pid: int, tipo: str, line: float, side: str) -> Tuple[int, dict]:
v5 = last_n_values(pid, tipo, 5)
v10 = last_n_values(pid, tipo, 10)

```
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
```

# =========================

# Live helpers

# =========================

def parse_minutes(min_str) -> float:
if not min_str:
return 0.0
try:
mm, ss = str(min_str).split(”:”)
return float(mm) + float(ss) / 60.0
except Exception:
return 0.0

def clock_to_seconds(game_clock: str) -> Optional[int]:
if not game_clock:
return None
gc = str(game_clock)
if gc.startswith(“PT”) and “M” in gc:
try:
mm = gc.split(“PT”)[1].split(“M”)[0]
ss = gc.split(“M”)[1].replace(“S”, “”).split(”.”)[0]
return int(mm) * 60 + int(ss)
except Exception:
return None
if “:” in gc:
try:
mm, ss = gc.split(”:”)
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
if side == “over”:
if elapsed_min is not None and elapsed_min >= 18:
return False
min_req = MIN_MINUTES_POINTS if tipo == “puntos” else MIN_MINUTES_REB_AST
return mins < min_req
else:
if elapsed_min is None:
return True
if is_blowout and elapsed_min >= 16:
return False
return elapsed_min < 22

def compute_over_score(tipo, faltante, mins, pf, period, clock_seconds, diff, is_clutch, is_blowout) -> int:
if tipo == “puntos”:
near_max, ideal_max = 4.0, 2.0
close_weight, ideal_bonus = 60, 10
min_floor = 10.0
foul_mult, blow_mult = 1.0, 1.0
else:
near_max, ideal_max = 1.5, 0.9
close_weight, ideal_bonus = 65, 12
min_floor = 14.0
foul_mult, blow_mult = 1.25, 1.35

```
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
```

def compute_under_score(tipo, margin_under, mins, pf, period, clock_seconds, diff, is_clutch, is_blowout) -> int:
if tipo == “puntos”:
min_margin, good_margin = 3.0, 6.0
blow_bonus, clutch_pen = 20, 10
else:
min_margin, good_margin = 2.0, 3.5
blow_bonus, clutch_pen = 24, 14

```
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
```

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

# Polymarket - helpers de matching

# =========================

PM_CACHE = {“ts”: 0, “date”: None, “props”: []}
PM_TTL_SECONDS = 8 * 60

_TIPO_MAP = {“points”: “puntos”, “rebounds”: “rebotes”, “assists”: “asistencias”}

# Regex: “Isaiah Hartenstein: Assists O/U 3.5”  OR  “Player Name Points O/U 22.5”

_PM_Q_RE = re.compile(
r”^(?P<player>.+?)(?::\s*|\s+)(?P<stat>Points|Rebounds|Assists)\s*O/U\s*(?P<line>\d+(?:.\d+)?)”,
re.IGNORECASE,
)

# Mapa tricode NBA → nombres posibles en slugs de Polymarket

_TRICODE_TO_SLUG_NAMES = {
“ATL”: [“atlanta”, “hawks”, “atl”],
“BOS”: [“boston”, “celtics”, “bos”],
“BKN”: [“brooklyn”, “nets”, “bkn”, “bk”],
“CHA”: [“charlotte”, “hornets”, “cha”],
“CHI”: [“chicago”, “bulls”, “chi”],
“CLE”: [“cleveland”, “cavaliers”, “cavs”, “cle”],
“DAL”: [“dallas”, “mavericks”, “mavs”, “dal”],
“DEN”: [“denver”, “nuggets”, “den”],
“DET”: [“detroit”, “pistons”, “det”],
“GSW”: [“golden-state”, “golden_state”, “warriors”, “gsw”, “gs”],
“HOU”: [“houston”, “rockets”, “hou”],
“IND”: [“indiana”, “pacers”, “ind”],
“LAC”: [“la-clippers”, “clippers”, “lac”],
“LAL”: [“la-lakers”, “lakers”, “lal”, “la”],
“MEM”: [“memphis”, “grizzlies”, “mem”],
“MIA”: [“miami”, “heat”, “mia”],
“MIL”: [“milwaukee”, “bucks”, “mil”],
“MIN”: [“minnesota”, “timberwolves”, “wolves”, “min”],
“NOP”: [“new-orleans”, “pelicans”, “nop”, “no”],
“NYK”: [“new-york”, “knicks”, “nyk”, “ny”],
“OKC”: [“oklahoma”, “thunder”, “okc”],
“ORL”: [“orlando”, “magic”, “orl”],
“PHI”: [“philadelphia”, “76ers”, “sixers”, “phi”],
“PHX”: [“phoenix”, “suns”, “phx”, “phx”],
“POR”: [“portland”, “trail-blazers”, “blazers”, “por”],
“SAC”: [“sacramento”, “kings”, “sac”],
“SAS”: [“san-antonio”, “spurs”, “sas”, “sa”],
“TOR”: [“toronto”, “raptors”, “tor”],
“UTA”: [“utah”, “jazz”, “uta”],
“WAS”: [“washington”, “wizards”, “was”],
}

def _slug_from_scoreboard_game(g: dict) -> str:
away = (g.get(“awayTeam”, {}) or {}).get(“teamTricode”, “”).lower()
home = (g.get(“homeTeam”, {}) or {}).get(“teamTricode”, “”).lower()
d = date.today().isoformat()
return f”nba-{away}-{home}-{d}”

def _event_matches_game(ev_slug: str, ev_title: str, away_tri: str, home_tri: str) -> bool:
“”“Verifica si un evento de Polymarket corresponde a un partido NBA específico.”””
slug_l = ev_slug.lower()
title_l = ev_title.lower()
combined = slug_l + “ “ + title_l

```
away_names = _TRICODE_TO_SLUG_NAMES.get(away_tri.upper(), [away_tri.lower()])
home_names = _TRICODE_TO_SLUG_NAMES.get(home_tri.upper(), [home_tri.lower()])

away_found = any(name in combined for name in away_names)
home_found = any(name in combined for name in home_names)

return away_found and home_found
```

def polymarket_fetch_all_nba_events() -> List[dict]:
“”“Obtiene todos los eventos NBA activos de Polymarket usando paginación.”””
all_events = []
limit = 100
offset = 0

```
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
```

def polymarket_event_by_slug(slug: str) -> Optional[dict]:
url = f”{GAMMA}/events/slug/{slug}”
try:
r = SESSION_PM.get(url, timeout=20)
log.info(f”Polymarket slug lookup ‘{slug}’ → status {r.status_code}”)
if r.status_code != 200:
return None
return r.json()
except Exception as e:
log.warning(f”polymarket_event_by_slug error: {e}”)
return None

def polymarket_event_markets(event_id: str) -> List[dict]:
“”“Obtiene markets de un evento específico por ID.”””
url = f”{GAMMA}/markets”
params = {“event_id”: event_id, “limit”: 200}
try:
r = SESSION_PM.get(url, params=params, timeout=20)
if r.status_code != 200:
return []
data = r.json()
return data if isinstance(data, list) else data.get(“markets”, [])
except Exception as e:
log.warning(f”polymarket_event_markets error: {e}”)
return []

def _parse_player_stat_from_market(m: dict) -> Tuple[Optional[str], Optional[str], Optional[float]]:
“””
Extrae (player, stat_type, line) de un market de Polymarket.
Retorna (None, None, None) si no aplica.
“””
# Intentamos sportsMarketType primero
smt = (m.get(“sportsMarketType”) or m.get(“sport_market_type”) or “”).lower()

```
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
```

def polymarket_props_from_event(event_json: dict, fallback_slug: str = “”) -> List[Prop]:
out: List[Prop] = []
event_slug = event_json.get(“slug”) or fallback_slug or str(event_json.get(“id”, “unknown”))

```
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
```

# =========================

# FALLBACK PROPS (hoy: 2026-02-25)

# =========================

_FALLBACK_DATE = date.today().isoformat()   # Siempre activo como último recurso

FALLBACK_PROPS: List[Prop] = [
# BOS @ DEN
Prop(“Jaylen Brown”, “puntos”, 28.5, “over”, source=“fallback”, game_slug=“nba-bos-den-2026-02-25”),
Prop(“Jaylen Brown”, “puntos”, 28.5, “under”, source=“fallback”, game_slug=“nba-bos-den-2026-02-25”),
Prop(“Nikola Jokić”, “puntos”, 27.5, “over”, source=“fallback”, game_slug=“nba-bos-den-2026-02-25”),
Prop(“Nikola Jokić”, “puntos”, 27.5, “under”, source=“fallback”, game_slug=“nba-bos-den-2026-02-25”),
Prop(“Jamal Murray”, “puntos”, 23.5, “over”, source=“fallback”, game_slug=“nba-bos-den-2026-02-25”),
Prop(“Jamal Murray”, “puntos”, 23.5, “under”, source=“fallback”, game_slug=“nba-bos-den-2026-02-25”),
Prop(“Payton Pritchard”, “puntos”, 18.5, “over”, source=“fallback”, game_slug=“nba-bos-den-2026-02-25”),
Prop(“Payton Pritchard”, “puntos”, 18.5, “under”, source=“fallback”, game_slug=“nba-bos-den-2026-02-25”),
Prop(“Derrick White”, “puntos”, 17.5, “over”, source=“fallback”, game_slug=“nba-bos-den-2026-02-25”),
Prop(“Derrick White”, “puntos”, 17.5, “under”, source=“fallback”, game_slug=“nba-bos-den-2026-02-25”),
# CLE @ MIL
Prop(“Donovan Mitchell”, “puntos”, 26.5, “over”, source=“fallback”, game_slug=“nba-cle-mil-2026-02-25”),
Prop(“Donovan Mitchell”, “puntos”, 26.5, “under”, source=“fallback”, game_slug=“nba-cle-mil-2026-02-25”),
Prop(“James Harden”, “puntos”, 20.5, “over”, source=“fallback”, game_slug=“nba-cle-mil-2026-02-25”),
Prop(“James Harden”, “puntos”, 20.5, “under”, source=“fallback”, game_slug=“nba-cle-mil-2026-02-25”),
Prop(“Jarrett Allen”, “puntos”, 15.5, “over”, source=“fallback”, game_slug=“nba-cle-mil-2026-02-25”),
Prop(“Jarrett Allen”, “puntos”, 15.5, “under”, source=“fallback”, game_slug=“nba-cle-mil-2026-02-25”),
Prop(“Sam Merrill”, “puntos”, 11.5, “over”, source=“fallback”, game_slug=“nba-cle-mil-2026-02-25”),
Prop(“Sam Merrill”, “puntos”, 11.5, “under”, source=“fallback”, game_slug=“nba-cle-mil-2026-02-25”),
Prop(“Jaylon Tyson”, “puntos”, 11.5, “over”, source=“fallback”, game_slug=“nba-cle-mil-2026-02-25”),
Prop(“Jaylon Tyson”, “puntos”, 11.5, “under”, source=“fallback”, game_slug=“nba-cle-mil-2026-02-25”),
# GSW @ MEM
Prop(“Al Horford”, “rebotes”, 6.5, “over”, source=“fallback”, game_slug=“nba-gsw-mem-2026-02-25”),
Prop(“Al Horford”, “rebotes”, 6.5, “under”, source=“fallback”, game_slug=“nba-gsw-mem-2026-02-25”),
Prop(“Moses Moody”, “puntos”, 18.5, “over”, source=“fallback”, game_slug=“nba-gsw-mem-2026-02-25”),
Prop(“Moses Moody”, “puntos”, 18.5, “under”, source=“fallback”, game_slug=“nba-gsw-mem-2026-02-25”),
Prop(“Brandin Podziemski”, “puntos”, 17.5, “over”, source=“fallback”, game_slug=“nba-gsw-mem-2026-02-25”),
Prop(“Brandin Podziemski”, “puntos”, 17.5, “under”, source=“fallback”, game_slug=“nba-gsw-mem-2026-02-25”),
Prop(“Ty Jerome”, “puntos”, 16.5, “over”, source=“fallback”, game_slug=“nba-gsw-mem-2026-02-25”),
Prop(“Ty Jerome”, “puntos”, 16.5, “under”, source=“fallback”, game_slug=“nba-gsw-mem-2026-02-25”),
Prop(“GG Jackson II”, “puntos”, 14.5, “over”, source=“fallback”, game_slug=“nba-gsw-mem-2026-02-25”),
Prop(“GG Jackson II”, “puntos”, 14.5, “under”, source=“fallback”, game_slug=“nba-gsw-mem-2026-02-25”),
# OKC @ DET
Prop(“Isaiah Hartenstein”, “asistencias”, 3.5, “over”, source=“fallback”, game_slug=“nba-okc-det-2026-02-25”),
Prop(“Isaiah Hartenstein”, “asistencias”, 3.5, “under”, source=“fallback”, game_slug=“nba-okc-det-2026-02-25”),
Prop(“Daniss Jenkins”, “asistencias”, 2.5, “over”, source=“fallback”, game_slug=“nba-okc-det-2026-02-25”),
Prop(“Daniss Jenkins”, “asistencias”, 2.5, “under”, source=“fallback”, game_slug=“nba-okc-det-2026-02-25”),
Prop(“Chet Holmgren”, “puntos”, 17.5, “over”, source=“fallback”, game_slug=“nba-okc-det-2026-02-25”),
Prop(“Chet Holmgren”, “puntos”, 17.5, “under”, source=“fallback”, game_slug=“nba-okc-det-2026-02-25”),
Prop(“Isaiah Joe”, “puntos”, 14.5, “over”, source=“fallback”, game_slug=“nba-okc-det-2026-02-25”),
Prop(“Isaiah Joe”, “puntos”, 14.5, “under”, source=“fallback”, game_slug=“nba-okc-det-2026-02-25”),
Prop(“Cason Wallace”, “puntos”, 11.5, “over”, source=“fallback”, game_slug=“nba-okc-det-2026-02-25”),
Prop(“Cason Wallace”, “puntos”, 11.5, “under”, source=“fallback”, game_slug=“nba-okc-det-2026-02-25”),
# SAC @ HOU
Prop(“Tari Eason”, “rebotes”, 7.5, “over”, source=“fallback”, game_slug=“nba-sac-hou-2026-02-25”),
Prop(“Tari Eason”, “rebotes”, 7.5, “under”, source=“fallback”, game_slug=“nba-sac-hou-2026-02-25”),
Prop(“Precious Achiuwa”, “rebotes”, 6.5, “over”, source=“fallback”, game_slug=“nba-sac-hou-2026-02-25”),
Prop(“Precious Achiuwa”, “rebotes”, 6.5, “under”, source=“fallback”, game_slug=“nba-sac-hou-2026-02-25”),
Prop(“Kevin Durant”, “rebotes”, 5.5, “over”, source=“fallback”, game_slug=“nba-sac-hou-2026-02-25”),
Prop(“Kevin Durant”, “rebotes”, 5.5, “under”, source=“fallback”, game_slug=“nba-sac-hou-2026-02-25”),
Prop(“Keegan Murray”, “rebotes”, 5.5, “over”, source=“fallback”, game_slug=“nba-sac-hou-2026-02-25”),
Prop(“Keegan Murray”, “rebotes”, 5.5, “under”, source=“fallback”, game_slug=“nba-sac-hou-2026-02-25”),
Prop(“Dorian Finney-Smith”, “rebotes”, 3.5, “over”, source=“fallback”, game_slug=“nba-sac-hou-2026-02-25”),
Prop(“Dorian Finney-Smith”, “rebotes”, 3.5, “under”, source=“fallback”, game_slug=“nba-sac-hou-2026-02-25”),
# SAS @ TOR
Prop(“Scottie Barnes”, “asistencias”, 4.5, “over”, source=“fallback”, game_slug=“nba-sas-tor-2026-02-25”),
Prop(“Scottie Barnes”, “asistencias”, 4.5, “under”, source=“fallback”, game_slug=“nba-sas-tor-2026-02-25”),
Prop(“Brandon Ingram”, “puntos”, 21.5, “over”, source=“fallback”, game_slug=“nba-sas-tor-2026-02-25”),
Prop(“Brandon Ingram”, “puntos”, 21.5, “under”, source=“fallback”, game_slug=“nba-sas-tor-2026-02-25”),
Prop(“RJ Barrett”, “puntos”, 17.5, “over”, source=“fallback”, game_slug=“nba-sas-tor-2026-02-25”),
Prop(“RJ Barrett”, “puntos”, 17.5, “under”, source=“fallback”, game_slug=“nba-sas-tor-2026-02-25”),
Prop(“Scottie Barnes”, “puntos”, 17.5, “over”, source=“fallback”, game_slug=“nba-sas-tor-2026-02-25”),
Prop(“Scottie Barnes”, “puntos”, 17.5, “under”, source=“fallback”, game_slug=“nba-sas-tor-2026-02-25”),
Prop(“Scottie Barnes”, “rebotes”, 8.5, “over”, source=“fallback”, game_slug=“nba-sas-tor-2026-02-25”),
Prop(“Scottie Barnes”, “rebotes”, 8.5, “under”, source=“fallback”, game_slug=“nba-sas-tor-2026-02-25”),
]

# =========================

# Polymarket: función principal de carga

# =========================

def polymarket_props_today_from_scoreboard() -> List[Prop]:
today = date.today().isoformat()
now = now_ts()

```
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
    log.warning("⚠️  Sin props de Polymarket - usando fallback hardcodeado")
    uniq = list(FALLBACK_PROPS)

PM_CACHE["date"] = today
PM_CACHE["ts"] = now
PM_CACHE["props"] = uniq
log.info(f"Props cargados: {len(uniq)} ({len(uniq)//2} jugadores/lineas)")
return uniq
```

# =========================

# Commands

# =========================

HELP_TEXT = (
“🧠 *NBA Props Bot v3*\n\n”

```
"*📋 Programación*\n"
"• `/games` `/today` → partidos de hoy\n"
"• `/lineup` → alineaciones + injury report\n"
"   `/lineup BOS` → filtrar por equipo\n\n"

"*📊 Props & Análisis*\n"
"• `/odds` → props con score PRE v2 (contexto incluido)\n"
"   `/odds nba-bos-den-...` → un partido\n"
"   `/odds Jokic` → un jugador\n"
"• `/alertas` → ranking mejores props del día (PRE≥55)\n"
"• `/analisis Jugador | tipo | side | linea`\n"
"   → tendencia · racha · H/A · matchup · veredicto\n"
"   Ej: `/analisis Nikola Jokic | puntos | over | 27.5`\n"
"• `/contexto AWAY HOME`\n"
"   → Def Rating · Pace · stats permitidas por equipo\n"
"   Ej: `/contexto BOS DEN`\n\n"

"*🔴 En vivo*\n"
"• `/live` → top props en vivo con scoring\n\n"

"*💰 Apuestas*\n"
"• `/bet Jugador | tipo | side | linea | monto`\n"
"• `/misapuestas` → pendientes\n"
"• `/resultado ID WIN|LOSS|PUSH stat` → cerrar manual\n"
"• `/historial` → ROI · win rate · rachas · top jugadores\n"
"   `/historial 7` → últimos 7 días\n\n"

"*🤖 Automático (tras /start)*\n"
"• Resumen matutino cada día a las 10:00h\n"
"• Alertas pre-partido cuando PRE≥68\n"
"• Auto-resolución de apuestas al terminar partido\n"
"• Alertas en vivo cuando prop alcanza threshold\n\n"

"*⚙️ Otros*\n"
"• `/add Jugador | tipo | side | linea` → prop manual\n"
"• `/debug` → estado técnico Polymarket\n"
```

)

def parse_add(text: str) -> Optional[Prop]:
body = text.strip()
body = re.sub(r”^/add(@\w+)?\s*”, “”, body).strip()
if “|” not in body:
return None
parts = [p.strip() for p in body.split(”|”)]
if len(parts) != 4:
return None
name, tipo, side, line_s = parts
tipo = tipo.lower()
side = side.lower()
if tipo not in (“puntos”, “rebotes”, “asistencias”):
return None
if side not in (“over”, “under”):
return None
try:
line = float(line_s)
except Exception:
return None
return Prop(player=name, tipo=tipo, side=side, line=line, source=“manual”)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
p = parse_add(update.message.text or “”)
if not p:
await update.message.reply_text(“Formato inválido.\n\n” + HELP_TEXT, parse_mode=ParseMode.MARKDOWN)
return

```
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
await update.message.reply_text(f"✅ Agregado (manual):\n• {p.player} - {p.tipo.upper()} {p.side.upper()} {p.line}")
```

async def cmd_games(update: Update, context: ContextTypes.DEFAULT_TYPE):
try:
games = await asyncio.wait_for(
asyncio.to_thread(lambda: scoreboard.ScoreBoard().get_dict()[“scoreboard”][“games”]),
timeout=20.0
)
except asyncio.TimeoutError:
await update.message.reply_text(“⚠️ Timeout leyendo scoreboard. Intenta de nuevo.”)
return
except Exception as e:
await update.message.reply_text(f”⚠️ No pude leer scoreboard: {e}”)
return

```
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
    lines.append(f"• {at} ({rec_away}) @ {ht} ({rec_home}) - {status}\n  `slug: {slug}`")

msg = "\n".join(lines)
if len(msg) > 3800:
    msg = msg[:3800] + "\n...(recortado)"
await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
```

async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
“”“Comando de debug para ver qué está pasando con Polymarket.”””
lines = [“🔍 *DEBUG Polymarket*\n”]

```
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
    msg = msg[:3800] + "\n..."
await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
```

def _group_props_pretty(props_pm: List[Prop]) -> Dict[str, Dict[str, Dict[Tuple[str, float], Dict[str, bool]]]]:
out: Dict[str, Dict[str, Dict[Tuple[str, float], Dict[str, bool]]]] = {}
for p in props_pm:
slug = p.game_slug or “unknown”
out.setdefault(slug, {})
out[slug].setdefault(p.player, {})
key = (p.tipo, float(p.line))
out[slug][p.player].setdefault(key, {“over”: False, “under”: False})
if p.side in (“over”, “under”):
out[slug][p.player][key][p.side] = True
return out

def _pre_rating_emoji(score: int) -> str:
“”“Convierte PRE score en emoji de rating visual.”””
if score >= 75:
return “🔥”
elif score >= 60:
return “✅”
elif score >= 45:
return “🟡”
elif score >= 30:
return “🟠”
else:
return “❄️”

def _pre_bar(score: int, length: int = 8) -> str:
“”“Barra de progreso visual para el score.”””
filled = round(score / 100 * length)
return “█” * filled + “░” * (length - filled)

def _pre_label(score: int) -> str:
if score >= 75: return “FUERTE”
elif score >= 60: return “BUENA”
elif score >= 45: return “MEDIA”
elif score >= 30: return “DÉBIL”
else: return “BAJA”

def _slug_to_matchup(slug: str) -> str:
“”“Convierte ‘nba-bos-den-2026-02-25’ en ‘BOS @ DEN’.”””
parts = slug.replace(“nba-”, “”).split(”-”)
if len(parts) >= 2:
away = parts[0].upper()
home = parts[1].upper()
return f”{away} @ {home}”
return slug

# =========================

# PRE score cache (evita recalcular en /odds repetidos)

# =========================

PRE_SCORE_CACHE: Dict[str, Tuple[int, int, dict]] = {}  # key → (pre_over, pre_under, meta)
PRE_SCORE_CACHE_TTL = 3 * 60 * 60  # 3 horas

def _pre_cache_key(pid: int, tipo: str, line: float) -> str:
return f”{pid}:{tipo}:{line}”

def pre_score_cached(pid: int, tipo: str, line: float) -> Tuple[int, int, dict]:
“”“Calcula PRE score over+under con cache en memoria.”””
key = _pre_cache_key(pid, tipo, line)
if key in PRE_SCORE_CACHE:
return PRE_SCORE_CACHE[key]
po, meta_o = pre_score(pid, tipo, line, “over”)
pu, _      = pre_score(pid, tipo, line, “under”)
PRE_SCORE_CACHE[key] = (po, pu, meta_o)
return po, pu, meta_o

def _compute_pre_for_player(player_name: str, tipo: str, line: float, source: str) -> dict:
“””
Función bloqueante que calcula PRE score para un jugador.
Se ejecuta en un thread separado para no bloquear el event loop.
“””
pid = get_pid_for_name(player_name)
if not pid:
return {
“tipo”: tipo, “line”: line, “source”: source,
“pre_over”: 0, “pre_under”: 0, “meta_over”: {},
“pid”: None,
}
po, pu, meta = pre_score_cached(pid, tipo, line)
return {
“tipo”: tipo, “line”: line, “source”: source,
“pre_over”: po, “pre_under”: pu, “meta_over”: meta,
“pid”: pid,
}

def _build_game_message(slug: str, players_data: Dict[str, List[dict]]) -> str:
“”“Construye el mensaje formateado de un partido con sus props y scores.”””
tipo_order = {“puntos”: 0, “rebotes”: 1, “asistencias”: 2}
tipo_icon  = {“puntos”: “🏀”, “rebotes”: “💪”, “asistencias”: “🎯”}

```
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
            f"{icon} *{tipo.upper()}* - línea `{ln}`\n"
            f"  OVER  {_pre_rating_emoji(po)} `{po:>3}/100` {_pre_bar(po)} _{_pre_label(po)}_\n"
            f"  UNDER {_pre_rating_emoji(pu)} `{pu:>3}/100` {_pre_bar(pu)} _{_pre_label(pu)}_\n"
            f"  📊 `{h5}/{n5}` últ.5 | `{h10}/{n10}` últ.10  {avg_str}"
        )

return "\n".join(lines)
```

async def cmd_odds(update: Update, context: ContextTypes.DEFAULT_TYPE):
tipo_order = {“puntos”: 0, “rebotes”: 1, “asistencias”: 2}

```
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
    f"📋 *NBA Props - {today_str}*\n"
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
            f"⚠️ *{matchup}* - timeout calculando scores, mostrando sin PRE.",
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
```

async def *send_long_message(update, text: str, max_len: int = 3800):
“”“Envía un mensaje partiéndolo en bloques si supera max_len.”””
if len(text) <= max_len:
try:
await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
except Exception as e:
log.warning(f”Markdown error, enviando sin formato: {e}”)
await update.message.reply_text(text.replace(”*”,””).replace(”*”,””).replace(”`”,””))
return

```
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
```

async def cmd_live(update: Update, context: ContextTypes.DEFAULT_TYPE):
msg_wait = await update.message.reply_text(
“⏳ *Cargando datos en vivo…*”, parse_mode=ParseMode.MARKDOWN
)

```
# ── 1. Scoreboard (no bloqueante) ──
try:
    games = await asyncio.wait_for(
        asyncio.to_thread(
            lambda: scoreboard.ScoreBoard().get_dict()["scoreboard"]["games"]
        ),
        timeout=20.0
    )
except Exception as e:
    await msg_wait.edit_text(f"⚠️ Error scoreboard: {e}")
    return

live_games = [g for g in games if g.get("gameStatus") == 2]
if not live_games:
    await msg_wait.edit_text(
        "⏸️ No hay partidos en vivo ahora.\nUsa `/games` para ver la cartelera.",
        parse_mode=ParseMode.MARKDOWN
    )
    return

await msg_wait.edit_text(
    f"🔄 *{len(live_games)} partido(s) en vivo* - leyendo boxscores...",
    parse_mode=ParseMode.MARKDOWN
)

# ── 2. Props del día (desde cache - no hace requests si ya se cargaron) ──
props_manual = load_props()
props_pm     = PM_CACHE.get("props", [])   # usa cache directamente, SIN request
if not props_pm:
    # Solo si el cache está vacío intentamos cargar (en thread)
    props_pm = await asyncio.to_thread(polymarket_props_today_from_scoreboard)
all_props = (props_manual or []) + (props_pm or [])

# ── 3. Índice de props por nombre de jugador (sin requests de PIDs) ──
# Clave: player_name.lower() → lista de props
props_by_name: Dict[str, List[Prop]] = {}
for p in all_props:
    props_by_name.setdefault(p.player.lower(), []).append(p)

# ── 4. Leer boxscores en paralelo ──
async def fetch_box(gid: str):
    try:
        return gid, await asyncio.wait_for(
            asyncio.to_thread(
                lambda gid=gid: boxscore.BoxScore(gid).get_dict()["game"]
            ),
            timeout=15.0
        )
    except Exception as e:
        log.warning(f"Boxscore error {gid}: {e}")
        return gid, None

box_results = await asyncio.gather(*[fetch_box(g["gameId"]) for g in live_games])

# ── 5. Cruzar boxscore con props por nombre ──
# El boxscore tiene nombre del jugador → buscamos en props_by_name
# También extraemos el PID del boxscore para pre_score (no necesitamos buscarlo)
scored_rows = []

for g, (gid, box) in zip(live_games, box_results):
    if not box:
        continue

    status     = g.get("gameStatusText", "")
    period     = int(g.get("period", 0) or 0)
    game_clock = g.get("gameClock", "") or ""
    clock_sec  = clock_to_seconds(game_clock)
    home       = g.get("homeTeam", {})
    away       = g.get("awayTeam", {})
    diff       = abs(int(home.get("score", 0)) - int(away.get("score", 0)))
    is_clutch  = diff <= 8
    is_blowout = diff >= BLOWOUT_IS
    elapsed_min = game_elapsed_minutes(period, clock_sec)

    for team_key in ["homeTeam", "awayTeam"]:
        for pl in box.get(team_key, {}).get("players", []):
            # Nombre completo del jugador desde el boxscore
            first = pl.get("firstName", "") or ""
            last  = pl.get("familyName", "") or pl.get("lastName", "") or ""
            full_name = f"{first} {last}".strip().lower()
            pid_box   = pl.get("personId")

            # Buscar props que coincidan con este jugador por nombre
            matching_props = props_by_name.get(full_name, [])

            # También intentar con solo apellido (para casos como "Nikola Jokić" vs "Jokic")
            if not matching_props and last:
                for key, plist in props_by_name.items():
                    if last.lower() in key or key in last.lower():
                        matching_props = plist
                        break

            if not matching_props:
                continue

            s    = pl.get("statistics", {})
            pts  = float(s.get("points", 0) or 0)
            reb  = float(s.get("reboundsTotal", 0) or 0)
            ast  = float(s.get("assists", 0) or 0)
            pf   = float(s.get("foulsPersonal", 0) or 0)
            mins = parse_minutes(s.get("minutes", ""))

            # pre_score usa el cache si ya fue calculado por /odds
            # si no, usa el pid del boxscore directamente
            pid = pid_box

            for pr in matching_props:
                actual = pts if pr.tipo == "puntos" else (reb if pr.tipo == "rebotes" else ast)

                # Usar cache de PRE si existe, sino calcular rápido con pid del boxscore
                cache_key_pre = _pre_cache_key(pid, pr.tipo, pr.line)
                if cache_key_pre in PRE_SCORE_CACHE:
                    po, pu, meta = PRE_SCORE_CACHE[cache_key_pre]
                    pre = po if pr.side == "over" else pu
                else:
                    # Calcular sin llamada HTTP (solo desde gamelog cache)
                    pre_val, meta = pre_score(pid, pr.tipo, pr.line, pr.side)
                    pre = pre_val

                if pr.side == "over":
                    faltante = float(pr.line) - actual
                    lo, hi = THRESH_POINTS_OVER if pr.tipo == "puntos" else THRESH_REB_AST_OVER
                    if not (lo <= faltante <= hi):
                        continue
                    if should_gate_by_minutes("over", pr.tipo, faltante, mins, elapsed_min, is_blowout):
                        continue
                    if diff >= BLOWOUT_STRONG:
                        if (pr.tipo == "puntos" and faltante > 1.0) or faltante > 0.8:
                            continue
                    live_sc = compute_over_score(
                        pr.tipo, faltante, mins, pf, period, clock_sec,
                        diff, is_clutch, is_blowout
                    )
                    final = int(clamp(0.55 * live_sc + 0.45 * pre, 0, 100))
                    scored_rows.append((
                        final, live_sc, pre, pr, actual, faltante,
                        status, period, game_clock, mins, pf, diff, meta
                    ))
                else:
                    margin_under = float(pr.line) - actual
                    if should_gate_by_minutes("under", pr.tipo, margin_under, mins, elapsed_min, is_blowout):
                        continue
                    live_sc = compute_under_score(
                        pr.tipo, margin_under, mins, pf, period, clock_sec,
                        diff, is_clutch, is_blowout
                    )
                    final = int(clamp(0.65 * live_sc + 0.35 * pre, 0, 100))
                    scored_rows.append((
                        final, live_sc, pre, pr, actual, margin_under,
                        status, period, game_clock, mins, pf, diff, meta
                    ))

# ── 6. Mostrar resultados ──
try:
    await msg_wait.delete()
except Exception:
    pass

if not scored_rows:
    await update.message.reply_text(
        "📭 *Sin señal en vivo ahora*\n\n"
        "Posibles causas:\n"
        "• Ninguna prop está cerca de su línea\n"
        "• Los jugadores llevan pocos minutos\n"
        "• Usa `/odds` primero para cargar el cache de props\n\n"
        "_El bot alertará automáticamente cuando haya señal._",
        parse_mode=ParseMode.MARKDOWN
    )
    return

scored_rows.sort(key=lambda x: x[0], reverse=True)
top = scored_rows[:15]

tipo_icon = {"puntos": "🏀", "rebotes": "💪", "asistencias": "🎯"}
out = [f"🔥 *LIVE - {len(live_games)} partido(s)*\n{'─'*28}"]

for (final, live_sc, pre, pr, actual, delta, status, period, clock, mins, pf, diff, meta) in top:
    side_tag = "OVER" if pr.side == "over" else "UNDER"
    extra    = f"faltan `{delta:.1f}`" if pr.side == "over" else f"colchón `{delta:.1f}`"
    icon     = tipo_icon.get(pr.tipo, "•")
    pre_e    = _pre_rating_emoji(final)
    out.append(
        f"\n{pre_e} `{final}/100` - *{pr.player}*\n"
        f"{icon} {pr.tipo.upper()} {side_tag} `{pr.line}` | actual `{actual:.0f}` ({extra})\n"
        f"⏱️ {status} Q{period} {clock} | MIN `{mins:.0f}` PF `{pf:.0f}` Dif `{diff}`\n"
        f"📊 `{meta.get('hits5','?')}/{meta.get('n5','?')}` últ5 "
        f"| `{meta.get('hits10','?')}/{meta.get('n10','?')}` últ10"
    )

await _send_long_message(update, "\n".join(out))
```

# =========================

# Background scan

# =========================

async def background_scan(context: ContextTypes.DEFAULT_TYPE):
chat_id = context.job.chat_id

```
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
```

# =========================

# INJURY REPORT + LINEUPS

# =========================

INJURY_CACHE: Dict[str, dict] = {}   # team_id -> {ts, players}
INJURY_TTL = 15 * 60                 # 15 min

# Posiciones estelares (para evaluar impacto de bajas)

STAR_ROLES = {“G”, “F”, “C”, “G-F”, “F-G”, “F-C”, “C-F”}

# Status de injury report NBA

INJURY_STATUS_LABELS = {
“Out”: “🔴 BAJA”,
“Doubtful”: “🟠 DUDA”,
“Questionable”: “🟡 DUDA POSIBLE”,
“Probable”: “🟢 PROBABLE”,
“Available”: “✅ DISPONIBLE”,
“Active”: “✅ ACTIVO”,
}

def get_team_id_by_tricode(tricode: str) -> Optional[int]:
“”“Obtiene el team_id de NBA por tricode (ej: ‘BOS’ → 1610612738).”””
all_teams = nba_teams_static.get_teams()
for t in all_teams:
if t.get(“abbreviation”, “”).upper() == tricode.upper():
return int(t[“id”])
return None

def fetch_team_roster_and_injuries(team_id: int) -> dict:
“””
Obtiene el roster completo + injury status via commonteamroster.
Retorna dict con ‘players’: lista de dicts con name, position, status, injury_desc.
“””
cache_key = str(team_id)
now = now_ts()
if cache_key in INJURY_CACHE and (now - INJURY_CACHE[cache_key].get(“ts”, 0)) < INJURY_TTL:
return INJURY_CACHE[cache_key]

```
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
```

def fetch_injury_report_from_scoreboard(games: list) -> Dict[str, List[dict]]:
“””
Extrae el injury report embebido en el scoreboard de NBA.
Retorna dict: team_tricode → lista de {name, status, description, position}
“””
injuries: Dict[str, List[dict]] = {}
for g in games:
for team_key in [“homeTeam”, “awayTeam”]:
team = g.get(team_key, {}) or {}
tricode = team.get(“teamTricode”, “”)
if not tricode:
continue
injuries.setdefault(tricode, [])
# El scoreboard incluye ‘players’ con gameStatus para live
# Pero para injury report pre-game usamos el campo ‘injuries’ si existe
game_injuries = g.get(“gameLeaders”, {})  # no es la fuente correcta

```
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
```

def fetch_boxscore_injury_data(game_id: str) -> Dict[str, List[dict]]:
“””
Obtiene datos de jugadores del boxscore pre-game / live.
Incluye status (Active, Inactive, etc.).
Retorna dict: team_tricode → lista de jugadores con status
“””
result: Dict[str, List[dict]] = {}
try:
time.sleep(0.3)
box = boxscore.BoxScore(game_id).get_dict().get(“game”, {})
for team_key in [“homeTeam”, “awayTeam”]:
team = box.get(team_key, {}) or {}
tricode = team.get(“teamTricode”, “”)
if not tricode:
continue
result[tricode] = []
for pl in team.get(“players”, []):
status = pl.get(“status”, “Active”)
name = f”{pl.get(‘firstName’, ‘’)} {pl.get(‘familyName’, ‘’)}”.strip()
pos = pl.get(“position”, “”)
starter = pl.get(“starter”, “0”)
not_playing = pl.get(“notPlayingReason”, “”) or pl.get(“inactiveReason”, “”) or “”
result[tricode].append({
“name”: name,
“status”: status,
“position”: pos,
“starter”: starter == “1”,
“not_playing_reason”: not_playing,
“player_id”: pl.get(“personId”, 0),
})
except Exception as e:
log.warning(f”fetch_boxscore_injury_data game_id={game_id}: {e}”)
return result

def _is_star_player(name: str, pid: int) -> bool:
“”“Heurística simple: si tiene historial de 20+ ppg es estrella.”””
try:
_, rows = get_gamelog_table(pid)
if not rows or len(rows) < 3:
return False
pts_vals = []
for r in rows[:10]:
# PTS suele estar en columna 26 aprox, pero usamos last_n_values
pass
v = last_n_values(pid, “puntos”, 10)
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
“””
Analiza el impacto de ausencias/dudas en el partido.
Retorna lista de strings con análisis.
“””
alerts = []

```
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
            f"🏥 *{tri}* tiene {len(inactives)} jugadores inactivos hoy - rotación muy corta"
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
    alerts.append("✅ Sin bajas destacadas - alineaciones completas esperadas")

return alerts
```

def format_team_lineup(tricode: str, players_data: List[dict]) -> str:
“”“Formatea la alineación de un equipo de forma visual.”””
starters = [p for p in players_data if p.get(“starter”) and p.get(“status”, “”).lower() not in (“inactive”, “out”)]
bench = [p for p in players_data if not p.get(“starter”) and p.get(“status”, “”).lower() not in (“inactive”, “out”)]
inactives = [p for p in players_data if p.get(“status”, “”).lower() in (“inactive”, “out”)]

```
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
        reason_str = f" - _{reason[:30]}_" if reason else ""
        lines.append(f"    • {p['name']}{reason_str}")

return "\n".join(lines)
```

async def cmd_lineup(update: Update, context: ContextTypes.DEFAULT_TYPE):
“”“Muestra alineaciones, injury report y análisis de impacto para los partidos de hoy.”””
msg_wait = await update.message.reply_text(
“⏳ Obteniendo alineaciones e injury report…”,
parse_mode=ParseMode.MARKDOWN
)

```
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
        full_msg = full_msg[:3900] + "\n...(recortado)"

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
```

# ================================================================

# BLOQUE 1 - ANÁLISIS ESTADÍSTICO AVANZADO

# ================================================================

def get_full_gamelog(pid: int) -> Tuple[List[str], List[list]]:
“”“Devuelve headers y rows completos del gamelog (usa cache existente).”””
return get_gamelog_table(pid)

def gamelog_col(headers: List[str], rows: List[list], col: str) -> List[float]:
“”“Extrae una columna del gamelog como lista de floats (juego más reciente primero).”””
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
“”“Extrae una columna del gamelog como lista de strings.”””
if not headers or not rows:
return []
try:
idx = headers.index(col)
except ValueError:
return []
return [str(r[idx]) for r in rows if idx < len(r)]

def trend_arrow(values: List[float]) -> str:
“”“Flecha de tendencia basada en los últimos 5 vs anteriores 5.”””
if len(values) < 6:
return “→”
rec  = sum(values[:5])  / 5
prev = sum(values[5:10]) / min(5, len(values[5:10]))
diff = rec - prev
if diff >  1.5: return “📈”
if diff < -1.5: return “📉”
return “→”

def streak_info(values: List[float], line: float, side: str) -> str:
“”“Calcula racha actual (ej: ‘4 en racha ✅’ o ‘2 sin cumplir ❌’).”””
if not values:
return “sin datos”
count = 0
if side == “over”:
hit_fn = lambda v: v > line
else:
hit_fn = lambda v: v < line
first_hit = hit_fn(values[0])
for v in values:
if hit_fn(v) == first_hit:
count += 1
else:
break
emoji = “✅” if first_hit else “❌”
label = “en racha” if first_hit else “sin cumplir”
return f”{count} {label} {emoji}”

def matchup_stats(pid: int, opp_tricode: str, tipo: str) -> Optional[dict]:
“””
Stats del jugador contra ese rival específico esta temporada.
Busca en el gamelog partidos donde MATCHUP contiene el tricode.
“””
headers, rows = get_full_gamelog(pid)
if not headers or not rows:
return None
col = STAT_COL.get(tipo)
if not col:
return None
try:
stat_idx     = headers.index(col)
matchup_idx  = headers.index(“MATCHUP”)
min_idx      = headers.index(“MIN”) if “MIN” in headers else None
except ValueError:
return None

```
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
```

def home_away_splits(pid: int, tipo: str) -> dict:
“”“Promedio home vs away del jugador esta temporada.”””
headers, rows = get_full_gamelog(pid)
if not headers or not rows:
return {}
col = STAT_COL.get(tipo)
if not col:
return {}
try:
stat_idx    = headers.index(col)
matchup_idx = headers.index(“MATCHUP”)
except ValueError:
return {}

```
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
```

def is_back_to_back(pid: int) -> bool:
“””¿Jugó ayer? Compara la fecha más reciente del gamelog con hoy.”””
headers, rows = get_full_gamelog(pid)
if not headers or not rows:
return False
try:
date_idx = headers.index(“GAME_DATE”)
except ValueError:
return False
try:
last_date_str = str(rows[0][date_idx])   # ej: “FEB 24, 2026”
from datetime import datetime, timedelta
last_date = datetime.strptime(last_date_str, “%b %d, %Y”).date()
yesterday = date.today() - timedelta(days=1)
return last_date == yesterday
except Exception:
return False

def build_advanced_analysis(pid: int, player_name: str, tipo: str, line: float,
side: str, opp_tricode: str, is_home: bool) -> str:
“””
Construye el bloque de análisis avanzado para un jugador/prop.
Incluye: tendencia, racha, splits H/A, matchup histórico, back-to-back.
“””
v20 = last_n_values(pid, tipo, 20)
v10 = last_n_values(pid, tipo, 10)
v5  = last_n_values(pid, tipo,  5)

```
if not v10:
    return "_Sin suficientes datos estadísticos_"

lines_out = []

# - Promedios y tendencia -
avg5  = round(sum(v5)  / len(v5),  1) if v5  else "-"
avg10 = round(sum(v10) / len(v10), 1) if v10 else "-"
avg20 = round(sum(v20) / len(v20), 1) if v20 else "-"
arrow = trend_arrow(v10)

lines_out.append(
    f"📊 *Promedios:* últ.5 `{avg5}` | últ.10 `{avg10}` | últ.20 `{avg20}` {arrow}"
)

# - Racha actual -
racha = streak_info(v10, line, side)
lines_out.append(f"🔁 *Racha actual:* {racha}  (línea `{line}` {side.upper()})")

# - Últimos 5 juegos detalle -
vals_str = "  ".join(f"`{v:.0f}`" for v in v5[:5])
lines_out.append(f"🕐 *Últ. 5 juegos:* {vals_str}")

# - Home/Away splits -
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
            f"({loc_n}G)  vs prom opuesto `{opp_avg or '-'}` → dif `{sign}{diff_ha}`"
        )

# - Matchup histórico vs rival -
mu = matchup_stats(pid, opp_tricode, tipo)
if mu and mu["games"] >= 1:
    lines_out.append(
        f"🆚 *vs {opp_tricode}:* `{mu['avg']}` prom en {mu['games']}G "
        f"(max `{mu['max']:.0f}` / min `{mu['min']:.0f}`)"
    )
else:
    lines_out.append(f"🆚 *vs {opp_tricode}:* sin historial esta temporada")

# - Back-to-back -
if is_back_to_b2b := is_back_to_back(pid):
    lines_out.append("⚠️ *BACK-TO-BACK:* jugó ayer → posible reducción de minutos/rendimiento")

# - Veredicto automático -
verdict = _auto_verdict(v10, line, side, avg10 if isinstance(avg10, float) else 0,
                         mu, splits, is_home, is_back_to_b2b)
lines_out.append(f"\n🧠 *Veredicto:* {verdict}")

return "\n".join(lines_out)
```

def loc_loc_str(is_home: bool) -> str:
return “local” if is_home else “visitante”

def _auto_verdict(v10, line, side, avg10, mu, splits, is_home, is_b2b) -> str:
“”“Genera un veredicto textual automático basado en todos los factores.”””
signals = []
warnings = []

```
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
    return f"✅ *FAVORABLE* - {'; '.join(signals)}"
elif len(signals) >= 1 and len(warnings) == 0:
    return f"🟡 *LIGERA VENTAJA* - {signals[0]}"
elif len(warnings) >= 2 and len(signals) == 0:
    return f"🔴 *EN CONTRA* - {'; '.join(warnings)}"
elif len(warnings) >= 1 and len(signals) == 0:
    return f"🟠 *PRECAUCIÓN* - {warnings[0]}"
elif signals and warnings:
    return f"⚖️ *MIXTO* - a favor: {signals[0]} | en contra: {warnings[0]}"
else:
    return "⚪ Sin señal clara - datos insuficientes"
```

async def cmd_analisis(update: Update, context: ContextTypes.DEFAULT_TYPE):
“””
/analisis Jugador | tipo | side | linea
Ej: /analisis Nikola Jokic | puntos | over | 27.5
“””
body = re.sub(r”^/analisis(@\w+)?\s*”, “”, (update.message.text or “”)).strip()
if “|” not in body:
await update.message.reply_text(
“Formato: `/analisis Nombre | tipo | side | linea`\n”
“Ej: `/analisis Nikola Jokic | puntos | over | 27.5`”,
parse_mode=ParseMode.MARKDOWN
)
return

```
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
    f"🔍 Analizando *{player_name}* - {tipo} {side} {line}...",
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
    full = full[:3900] + "\n..."
await msg_wait.edit_text(full, parse_mode=ParseMode.MARKDOWN)
```

# ================================================================

# BLOQUE 2 - ALERTAS PRE-PARTIDO INTELIGENTES

# ================================================================

SMART_ALERTS_FILE  = “smart_alerts_state.json”
SMART_ALERT_THRESH = 68   # PRE score mínimo para alerta pre-partido
SMART_ALERT_HOUR_START = 10  # hora local a partir de la cual enviar alertas pre-partido
SMART_ALERTS_SENT_TTL  = 20 * 60 * 60  # no reenviar la misma alerta en 20h

def load_smart_alerts_state() -> dict:
return load_json(SMART_ALERTS_FILE, {})

def save_smart_alerts_state(st: dict):
save_json(SMART_ALERTS_FILE, st)

def _smart_alert_key(player: str, tipo: str, side: str, line: float) -> str:
d = date.today().isoformat()
return f”{d}|{player.lower()}|{tipo}|{side}|{line}”

def _build_pre_game_alert(player: str, tipo: str, side: str, line: float,
pre: int, meta: dict, extra_flags: List[str]) -> str:
“”“Formatea el mensaje de alerta pre-partido.”””
tipo_icon = {“puntos”: “🏀”, “rebotes”: “💪”, “asistencias”: “🎯”}.get(tipo, “•”)
pre_emoji = _pre_rating_emoji(pre)
pre_bar   = _pre_bar(pre)
pre_lbl   = _pre_label(pre)

```
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
    f"_{pre_lbl}_ - `{h5}/{n5}` últ.5 | `{h10}/{n10}` últ.10"
    + (f" | prom `{avg:.1f}`" if avg else "") + "\n"
)
if flags_str:
    msg += f"\n{flags_str}\n"
return msg
```

async def background_smart_alerts(context: ContextTypes.DEFAULT_TYPE):
“””
Job periódico que analiza props pre-partido y envía alertas cuando
el PRE score supera el umbral, con señales de contexto adicionales.
“””
chat_id = context.job.chat_id
state   = load_smart_alerts_state()
now     = now_ts()

```
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
            flags.append("⚠️ Back-to-back - puede afectar minutos")
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
```

async def cmd_alertas(update: Update, context: ContextTypes.DEFAULT_TYPE):
“””
/alertas - muestra todas las props pre-partido con PRE ≥ 60, ordenadas.
“””
msg_wait = await update.message.reply_text(
“🔍 *Buscando mejores props pre-partido…*”,
parse_mode=ParseMode.MARKDOWN
)

```
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
    f"🏆 *MEJORES PROPS HOY - {today_str}*",
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
        f"*{i}.* {pre_e} `{pre}/100` - *{prop.player}*\n"
        f"   {icon} {prop.tipo.upper()} OVER `{prop.line}`  _{matchup}_\n"
        f"   {bar}  {avg_str}  `{h5}/{n5}` últ5 | `{h10}/{n10}` últ10"
    )

msg = "\n".join(lines)
if len(msg) > 3900:
    msg = msg[:3900] + "\n...(recortado)"
await msg_wait.edit_text(msg, parse_mode=ParseMode.MARKDOWN)
```

# ================================================================

# BLOQUE 3 - HISTORIAL Y TRACKING DE APUESTAS

# ================================================================

BETS_FILE = “bets.json”

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
result: Optional[str] = None   # “win” | “loss” | “push” | None (pendiente)
actual_stat: Optional[float] = None
resolved_at: Optional[int] = None
notes: str = “”

def load_bets() -> List[Bet]:
raw = load_json(BETS_FILE, {“bets”: []})
out = []
for b in raw.get(“bets”, []):
try:
out.append(Bet(**b))
except Exception:
pass
return out

def save_bets(bets: List[Bet]):
save_json(BETS_FILE, {“bets”: [asdict(b) for b in bets]})

def _new_bet_id() -> str:
import uuid
return str(uuid.uuid4())[:8].upper()

def _parse_bet_command(text: str) -> Optional[dict]:
“””
Parsea: /bet Jugador | tipo | side | linea | monto
Ej:     /bet Nikola Jokic | puntos | over | 27.5 | 50
“””
body = re.sub(r”^/bet(@\w+)?\s*”, “”, text).strip()
parts = [x.strip() for x in body.split(”|”)]
if len(parts) < 4:
return None
player, tipo, side, line_s = parts[0], parts[1], parts[2], parts[3]
amount_s = parts[4] if len(parts) >= 5 else “1”
tipo = tipo.lower(); side = side.lower()
if tipo not in (“puntos”,“rebotes”,“asistencias”): return None
if side not in (“over”,“under”): return None
try:
line   = float(line_s)
amount = float(amount_s)
except Exception:
return None
return {“player”: player, “tipo”: tipo, “side”: side, “line”: line, “amount”: amount}

async def cmd_bet(update: Update, context: ContextTypes.DEFAULT_TYPE):
“””
/bet Jugador | tipo | side | linea | monto
Registra una apuesta y calcula el PRE score.
“””
parsed = _parse_bet_command(update.message.text or “”)
if not parsed:
await update.message.reply_text(
“Formato: `/bet Jugador | tipo | side | linea | monto`\n”
“Ej: `/bet Nikola Jokic | puntos | over | 27.5 | 50`\n”
“*monto es opcional (default: 1 unidad)*”,
parse_mode=ParseMode.MARKDOWN
)
return

```
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
```

async def cmd_resultado(update: Update, context: ContextTypes.DEFAULT_TYPE):
“””
/resultado ID WIN|LOSS|PUSH stat_real
Ej: /resultado A3F2B1C0 WIN 29.0
“””
args = context.args or []
if len(args) < 2:
await update.message.reply_text(
“Formato: `/resultado ID WIN|LOSS|PUSH stat_real`\n”
“Ej: `/resultado A3F2B1C0 WIN 29`”,
parse_mode=ParseMode.MARKDOWN
)
return

```
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
    f"👤 {found.player} - {found.tipo.upper()} {found.side.upper()} `{found.line}`",
    parse_mode=ParseMode.MARKDOWN
)
```

async def cmd_historial(update: Update, context: ContextTypes.DEFAULT_TYPE):
“””
/historial - muestra estadísticas completas de tus apuestas.
/historial 30 - últimos 30 días (default: 30)
“””
args = context.args or []
days = int(args[0]) if args and args[0].isdigit() else 30
user_id = update.effective_user.id if update.effective_user else 0
cutoff  = now_ts() - days * 86400

```
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
    return f"`{w}/{len(sub)}`" if sub else "-"

# Stats por side
def side_stats(bets_list, side):
    sub = [b for b in bets_list if b.side == side and b.result in ("win","loss")]
    w   = sum(1 for b in sub if b.result == "win")
    return f"`{w}/{len(sub)}`" if sub else "-"

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
    f"📊 *MI HISTORIAL - últimos {days} días*\n"
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
        msg += f"  `#{b.id}` {b.player} {b.tipo.upper()} {b.side.upper()} `{b.line}` - `{b.amount}`u\n"
    if len(pending) > 5:
        msg += f"  _...y {len(pending)-5} más_\n"

msg += f"\n_Usa `/resultado ID WIN|LOSS stat` para resolver_"

if len(msg) > 3900:
    msg = msg[:3900] + "\n..."
await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
```

async def cmd_misapuestas(update: Update, context: ContextTypes.DEFAULT_TYPE):
“””
/misapuestas - lista las apuestas pendientes del usuario.
“””
user_id = update.effective_user.id if update.effective_user else 0
bets    = load_bets()
pending = [b for b in bets if b.user_id == user_id and not b.result]

```
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
    matchup  = _slug_to_matchup(b.game_slug) if b.game_slug else "-"
    ts_str   = time.strftime("%d/%m %H:%M", time.localtime(b.placed_at))
    lines.append(
        f"`#{b.id}` {icon} *{b.player}*\n"
        f"  {b.tipo.upper()} {b.side.upper()} `{b.line}` - `{b.amount}`u\n"
        f"  {pre_e} PRE `{b.pre_score}/100` | {matchup} | {ts_str}"
    )

msg = "\n\n".join(lines)
if len(msg) > 3900:
    msg = msg[:3900] + "\n..."
await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
```

# ================================================================

# BLOQUE A - CONTEXTO DEFENSIVO + PACE

# ================================================================

CONTEXT_CACHE: Dict[str, dict] = {}   # team_id → {ts, def_rating, pace, opp_pts_pos}
CONTEXT_TTL = 4 * 60 * 60             # 4 horas

# Columnas que usamos de leaguedashteamstats

_TEAM_STAT_COLS = [“TEAM_ID”,“TEAM_NAME”,“DEF_RATING”,“PACE”,“OPP_PTS_PAINT”,
“OPP_PTS_2ND_CHANCE”,“OPP_PTS_OFF_TOV”,“OPP_PTS_FB”]

def fetch_league_team_stats() -> Dict[int, dict]:
“””
Descarga leaguedashteamstats (defensive rating, pace, etc.) con cache.
Retorna dict team_id → {def_rating, pace, opp_pts_paint, …}
“””
cache_key = “league_team_stats”
now = now_ts()
cached = CONTEXT_CACHE.get(cache_key)
if cached and (now - cached.get(“ts”, 0)) < CONTEXT_TTL:
return cached.get(“data”, {})

```
time.sleep(0.5)
url = "https://stats.nba.com/stats/leaguedashteamstats"
params = {
    "Conference": "", "DateFrom": "", "DateTo": "",
    "Division": "", "GameScope": "", "GameSegment": "",
    "LastNGames": "0", "LeagueID": "00", "Location": "",
    "MeasureType": "Advanced", "Month": "0",
    "OpponentTeamID": "0", "Outcome": "", "PORound": "0",
    "PaceAdjust": "N", "PerMode": "PerGame", "Period": "0",
    "PlayerExperience": "", "PlayerPosition": "", "PlusMinus": "N",
    "Rank": "N", "Season": SEASON, "SeasonSegment": "",
    "SeasonType": "Regular Season", "ShotClockRange": "",
    "StarterBench": "", "TeamID": "0", "TwoWay": "0",
    "VsConference": "", "VsDivision": "",
}
try:
    resp = SESSION_NBA.get(url, params=params, timeout=(12, 60))
    if resp.status_code != 200:
        return {}
    data  = resp.json()
    rs    = (data.get("resultSets") or [{}])[0]
    hdrs  = rs.get("headers", [])
    rows  = rs.get("rowSet",  [])
    result: Dict[int, dict] = {}
    for row in rows:
        rd = dict(zip(hdrs, row))
        tid = int(rd.get("TEAM_ID", 0))
        result[tid] = {
            "team_name": rd.get("TEAM_NAME",""),
            "def_rating": float(rd.get("DEF_RATING") or 0),
            "pace":       float(rd.get("PACE")       or 0),
            "off_rating": float(rd.get("OFF_RATING") or 0),
        }
    CONTEXT_CACHE[cache_key] = {"ts": now, "data": result}
    log.info(f"League team stats cargados: {len(result)} equipos")
    return result
except Exception as e:
    log.warning(f"fetch_league_team_stats: {e}")
    return {}
```

def fetch_opp_position_stats() -> Dict[int, dict]:
“””
Descarga leaguedashteamstats MeasureType=Opponent para ver
cuántos puntos/reb/ast permite cada equipo por posición.
“””
cache_key = “opp_pos_stats”
now = now_ts()
cached = CONTEXT_CACHE.get(cache_key)
if cached and (now - cached.get(“ts”, 0)) < CONTEXT_TTL:
return cached.get(“data”, {})

```
time.sleep(0.4)
url = "https://stats.nba.com/stats/leaguedashteamstats"
params = {
    "Conference": "", "DateFrom": "", "DateTo": "",
    "Division": "", "GameScope": "", "GameSegment": "",
    "LastNGames": "0", "LeagueID": "00", "Location": "",
    "MeasureType": "Opponent", "Month": "0",
    "OpponentTeamID": "0", "Outcome": "", "PORound": "0",
    "PaceAdjust": "N", "PerMode": "PerGame", "Period": "0",
    "PlayerExperience": "", "PlayerPosition": "", "PlusMinus": "N",
    "Rank": "N", "Season": SEASON, "SeasonSegment": "",
    "SeasonType": "Regular Season", "ShotClockRange": "",
    "StarterBench": "", "TeamID": "0", "TwoWay": "0",
    "VsConference": "", "VsDivision": "",
}
try:
    resp = SESSION_NBA.get(url, params=params, timeout=(12, 60))
    if resp.status_code != 200:
        return {}
    data  = resp.json()
    rs    = (data.get("resultSets") or [{}])[0]
    hdrs  = rs.get("headers", [])
    rows  = rs.get("rowSet",  [])
    result: Dict[int, dict] = {}
    for row in rows:
        rd = dict(zip(hdrs, row))
        tid = int(rd.get("TEAM_ID", 0))
        result[tid] = {
            "opp_pts":  float(rd.get("OPP_PTS")  or 0),
            "opp_reb":  float(rd.get("OPP_REB")  or 0),
            "opp_ast":  float(rd.get("OPP_AST")  or 0),
        }
    CONTEXT_CACHE[cache_key] = {"ts": now, "data": result}
    return result
except Exception as e:
    log.warning(f"fetch_opp_position_stats: {e}")
    return {}
```

# Caché de team stats indexado por tricode

_TRICODE_TO_TEAM_ID_CACHE: Dict[str, int] = {}

def get_team_id_cached(tricode: str) -> Optional[int]:
if tricode in _TRICODE_TO_TEAM_ID_CACHE:
return _TRICODE_TO_TEAM_ID_CACHE[tricode]
tid = get_team_id_by_tricode(tricode)
if tid:
_TRICODE_TO_TEAM_ID_CACHE[tricode] = tid
return tid

def get_defensive_context(opp_tricode: str, tipo: str) -> dict:
“””
Retorna contexto defensivo del rival:
- def_rating, pace
- cuántos puntos/reb/ast permite vs liga
- percentil defensivo (0=mejor defensa, 100=peor)
“””
result = {“def_rating”: None, “pace”: None, “opp_stat”: None,
“def_rank”: None, “pace_rank”: None, “verdict”: “”}

```
team_stats = fetch_league_team_stats()
opp_stats  = fetch_opp_position_stats()
if not team_stats:
    return result

opp_tid = get_team_id_cached(opp_tricode)
if not opp_tid or opp_tid not in team_stats:
    return result

ts = team_stats[opp_tid]
result["def_rating"] = ts["def_rating"]
result["pace"]       = ts["pace"]

# Ranking defensivo (menor def_rating = mejor defensa)
all_def = sorted(team_stats.values(), key=lambda x: x["def_rating"])
rank_def = next((i+1 for i,t in enumerate(all_def)
                 if t.get("team_name") == ts["team_name"]), None)
result["def_rank"] = rank_def   # 1 = mejor defensa, 30 = peor

# Ranking de pace (mayor = más rápido)
all_pace = sorted(team_stats.values(), key=lambda x: x["pace"], reverse=True)
rank_pace = next((i+1 for i,t in enumerate(all_pace)
                  if t.get("team_name") == ts["team_name"]), None)
result["pace_rank"] = rank_pace  # 1 = más rápido

# Stats permitidos por el rival
if opp_tid in opp_stats:
    os = opp_stats[opp_tid]
    stat_key = {"puntos": "opp_pts", "rebotes": "opp_reb", "asistencias": "opp_ast"}.get(tipo)
    if stat_key:
        result["opp_stat"] = os.get(stat_key)
        # Ranking de lo que permite (más = peor defensa en esa stat)
        all_opp = sorted(opp_stats.values(), key=lambda x: x.get(stat_key,0), reverse=True)
        rank_opp = next((i+1 for i,s in enumerate(all_opp)
                         if s.get(stat_key) == os.get(stat_key)), None)
        result["opp_stat_rank"] = rank_opp  # 1 = permite más de esa stat

# Veredicto automático del contexto
verdicts = []
if rank_def:
    if rank_def >= 25:   verdicts.append("defensa débil (permite mucho) ✅")
    elif rank_def <= 5:  verdicts.append("defensa élite (difícil) ⚠️")
if rank_pace:
    if rank_pace <= 5:   verdicts.append("ritmo alto → más posesiones ✅")
    elif rank_pace >= 25: verdicts.append("ritmo lento → menos posesiones ⚠️")
opp_stat_rank = result.get("opp_stat_rank")
if opp_stat_rank:
    if opp_stat_rank <= 8:   verdicts.append(f"rival top-8 en {tipo} permitidos ✅")
    elif opp_stat_rank >= 23: verdicts.append(f"rival bottom-8 en {tipo} permitidos ⚠️")

result["verdict"] = " · ".join(verdicts) if verdicts else "contexto neutro"
return result
```

def format_defensive_context(ctx: dict, tipo: str, opp_tri: str) -> str:
“”“Formatea el contexto defensivo para incluir en mensajes.”””
if ctx.get(“def_rating”) is None:
return f”*Sin datos defensivos para {opp_tri}*”

```
dr   = ctx["def_rating"]
pace = ctx["pace"]
dr_r = ctx.get("def_rank","?")
pr_r = ctx.get("pace_rank","?")
os   = ctx.get("opp_stat")
osr  = ctx.get("opp_stat_rank","?")

dr_emoji   = "🟢" if (isinstance(dr_r, int) and dr_r >= 20) else ("🔴" if (isinstance(dr_r, int) and dr_r <= 8) else "🟡")
pace_emoji = "🟢" if (isinstance(pr_r, int) and pr_r <= 8) else ("🔴" if (isinstance(pr_r, int) and pr_r >= 23) else "🟡")

lines = [f"🛡️ *Contexto vs {opp_tri}:*"]
lines.append(f"  {dr_emoji} Def Rating: `{dr:.1f}` (rank #{dr_r}/30)")
lines.append(f"  {pace_emoji} Pace: `{pace:.1f}` (rank #{pr_r}/30)")
if os is not None:
    osr_emoji = "🟢" if (isinstance(osr, int) and osr <= 8) else ("🔴" if (isinstance(osr, int) and osr >= 23) else "🟡")
    lines.append(f"  {osr_emoji} {tipo.upper()} permitidos/j: `{os:.1f}` (rank #{osr}/30)")
lines.append(f"  💬 _{ctx['verdict']}_")
return "\n".join(lines)
```

# ================================================================

# BLOQUE B - MODELO MEJORADO (PRE v2 con contexto)

# ================================================================

def pre_score_v2(pid: int, tipo: str, line: float, side: str,
opp_tricode: str = “”, is_home: bool = True,
rest_days: int = 1) -> Tuple[int, dict]:
“””
PRE score mejorado que incorpora:
- Historial del jugador (igual que v1)
- Contexto defensivo del rival (def_rating, pace, opp_stat)
- Split home/away
- Rest days
“””
# Base: score v1
base_score, meta = pre_score(pid, tipo, line, side)

```
adjustments = []
adj_total = 0.0

# ── Ajuste 1: contexto defensivo ──
if opp_tricode:
    ctx = get_defensive_context(opp_tricode, tipo)
    dr_rank   = ctx.get("def_rank")
    pace_rank = ctx.get("pace_rank")
    osr       = ctx.get("opp_stat_rank")
    opp_stat  = ctx.get("opp_stat")

    # Defensive rating del rival
    if dr_rank:
        if side == "over":
            if dr_rank >= 25:   adj = +8; adjustments.append(f"rival def débil +8")
            elif dr_rank >= 20: adj = +4; adjustments.append(f"rival def floja +4")
            elif dr_rank <= 5:  adj = -8; adjustments.append(f"rival def élite -8")
            elif dr_rank <= 10: adj = -4; adjustments.append(f"rival def buena -4")
            else: adj = 0
        else:  # under
            if dr_rank <= 5:    adj = +8; adjustments.append(f"rival def élite +8")
            elif dr_rank <= 10: adj = +4; adjustments.append(f"rival def buena +4")
            elif dr_rank >= 25: adj = -8; adjustments.append(f"rival def débil -8")
            elif dr_rank >= 20: adj = -4; adjustments.append(f"rival def floja -4")
            else: adj = 0
        adj_total += adj

    # Pace
    if pace_rank:
        if side == "over":
            if pace_rank <= 5:   adj = +5; adjustments.append(f"ritmo alto +5")
            elif pace_rank >= 25: adj = -5; adjustments.append(f"ritmo lento -5")
            else: adj = 0
        else:
            if pace_rank >= 25:  adj = +5; adjustments.append(f"ritmo lento +5")
            elif pace_rank <= 5:  adj = -5; adjustments.append(f"ritmo alto -5")
            else: adj = 0
        adj_total += adj

    # Stat específica permitida
    if osr:
        if side == "over":
            if osr <= 8:    adj = +6; adjustments.append(f"rival permite muchos {tipo} +6")
            elif osr >= 23: adj = -6; adjustments.append(f"rival limita {tipo} -6")
            else: adj = 0
        else:
            if osr >= 23:   adj = +6; adjustments.append(f"rival limita {tipo} +6")
            elif osr <= 8:  adj = -6; adjustments.append(f"rival permite muchos {tipo} -6")
            else: adj = 0
        adj_total += adj

    meta["ctx_def_rank"]  = dr_rank
    meta["ctx_pace_rank"] = pace_rank
    meta["ctx_opp_stat"]  = opp_stat
    meta["ctx_osr"]       = osr
    meta["ctx_opp_tri"]   = opp_tricode

# ── Ajuste 2: split H/A ──
splits = home_away_splits(pid, tipo)
loc = "home" if is_home else "away"
loc_avg = splits.get(f"{loc}_avg")
if loc_avg is not None:
    gap = loc_avg - line if side == "over" else line - loc_avg
    if gap > 2.0:   adj = +5; adjustments.append(f"split {loc} favorable +5")
    elif gap < -2.0: adj = -5; adjustments.append(f"split {loc} desfavorable -5")
    else: adj = 0
    adj_total += adj
    meta["ha_split_avg"] = loc_avg
    meta["ha_loc"] = loc

# ── Ajuste 3: rest days ──
if rest_days == 0:    # back-to-back
    adj = -6 if side == "over" else +6
    adjustments.append(f"back-to-back {adj:+d}")
    adj_total += adj
elif rest_days >= 3:  # bien descansado
    adj = +4 if side == "over" else -4
    adjustments.append(f"bien descansado {adj:+d}")
    adj_total += adj

final = int(clamp(base_score + adj_total, 0, 100))
meta["v2_base"]        = base_score
meta["v2_adj"]         = round(adj_total, 1)
meta["v2_adjustments"] = adjustments
return final, meta
```

def _compute_pre_v2_for_player(player_name: str, tipo: str, line: float,
source: str, opp_tricode: str, is_home: bool) -> dict:
“”“Versión v2 de _compute_pre_for_player con contexto defensivo.”””
pid = get_pid_for_name(player_name)
if not pid:
return {“tipo”: tipo, “line”: line, “source”: source,
“pre_over”: 0, “pre_under”: 0, “meta_over”: {}, “pid”: None}

```
# Rest days
rest = 1
try:
    headers, rows = get_gamelog_table(pid)
    if headers and rows:
        from datetime import datetime, timedelta
        date_idx = headers.index("GAME_DATE") if "GAME_DATE" in headers else -1
        if date_idx >= 0 and rows:
            last_str = str(rows[0][date_idx])
            last_d   = datetime.strptime(last_str, "%b %d, %Y").date()
            rest = (date.today() - last_d).days
except Exception:
    pass

po, meta_o = pre_score_v2(pid, tipo, line, "over",  opp_tricode, is_home, rest)
pu, _      = pre_score_v2(pid, tipo, line, "under", opp_tricode, is_home, rest)

# Cache
cache_key = _pre_cache_key(pid, tipo, line)
PRE_SCORE_CACHE[cache_key] = (po, pu, meta_o)

return {"tipo": tipo, "line": line, "source": source,
        "pre_over": po, "pre_under": pu, "meta_over": meta_o, "pid": pid}
```

def _enrich_game_message(slug: str, players_data: Dict[str, List[dict]]) -> str:
“””
Igual que _build_game_message pero añade ajuste v2 y contexto defensivo
en cada línea del jugador.
“””
tipo_order = {“puntos”: 0, “rebotes”: 1, “asistencias”: 2}
tipo_icon  = {“puntos”: “🏀”, “rebotes”: “💪”, “asistencias”: “🎯”}

```
matchup = _slug_to_matchup(slug)
lines   = [f"🟣 *{matchup}*\n`{slug}`\n{'─'*28}"]

def best_score(entries):
    return max((max(e["pre_over"], e["pre_under"]) for e in entries), default=0)

for pl in sorted(players_data.keys(), key=lambda p: best_score(players_data[p]), reverse=True):
    entries = sorted(players_data[pl], key=lambda e: tipo_order.get(e["tipo"], 9))
    lines.append(f"\n👤 *{pl}*")

    for e in entries:
        tipo = e["tipo"]
        ln   = e["line"]
        po   = e["pre_over"]
        pu   = e["pre_under"]
        icon = tipo_icon.get(tipo, "•")
        meta = e.get("meta_over", {})

        h5    = meta.get("hits5",  "?")
        n5    = meta.get("n5",     "?")
        h10   = meta.get("hits10", "?")
        n10   = meta.get("n10",    "?")
        avg10 = meta.get("avg10",  None)
        avg_str = f"prom `{avg10:.1f}`" if avg10 is not None else ""

        # Ajustes v2
        base     = meta.get("v2_base", po)
        adj      = meta.get("v2_adj",  0)
        adj_sign = f"+{adj}" if adj >= 0 else str(adj)
        adj_str  = f"  _(base {base} {adj_sign} ctx)_\n" if adj != 0 else ""

        # Contexto defensivo resumido
        ctx_dr  = meta.get("ctx_def_rank")
        ctx_pr  = meta.get("ctx_pace_rank")
        ctx_osr = meta.get("ctx_osr")
        ctx_parts = []
        if ctx_dr:  ctx_parts.append(f"Def#{ctx_dr}")
        if ctx_pr:  ctx_parts.append(f"Pace#{ctx_pr}")
        if ctx_osr: ctx_parts.append(f"{tipo[:3].capitalize()}Allow#{ctx_osr}")
        ctx_line = f"  🛡️ `{' · '.join(ctx_parts)}`\n" if ctx_parts else ""

        lines.append(
            f"{icon} *{tipo.upper()}* - `{ln}`\n"
            f"  OVER  {_pre_rating_emoji(po)} `{po:>3}/100` {_pre_bar(po)} _{_pre_label(po)}_\n"
            f"  UNDER {_pre_rating_emoji(pu)} `{pu:>3}/100` {_pre_bar(pu)} _{_pre_label(pu)}_\n"
            f"{adj_str}"
            f"  📊 `{h5}/{n5}` últ5 | `{h10}/{n10}` últ10  {avg_str}\n"
            f"{ctx_line}"
        )

return "\n".join(lines)
```

# ================================================================

# BLOQUE C - RESUMEN MATUTINO AUTOMÁTICO

# ================================================================

MORNING_DIGEST_HOUR = int(os.environ.get(“MORNING_HOUR”, “10”))  # hora local
MORNING_DIGEST_FILE = “morning_digest_state.json”

def load_morning_state() -> dict:
return load_json(MORNING_DIGEST_FILE, {})

def save_morning_state(st: dict):
save_json(MORNING_DIGEST_FILE, st)

async def send_morning_digest(context: ContextTypes.DEFAULT_TYPE):
“””
Job: envía resumen matutino automático con
- Partidos del día
- Injury report rápido
- Top 5 props recomendadas (PRE v2)
“””
chat_id = context.job.chat_id
today   = date.today().isoformat()

```
# Verificar que no se haya enviado ya hoy
state = load_morning_state()
if state.get("last_date") == today:
    return
state["last_date"] = today
save_morning_state(state)

try:
    games = await asyncio.to_thread(
        lambda: scoreboard.ScoreBoard().get_dict()["scoreboard"]["games"]
    )
except Exception as e:
    log.warning(f"Morning digest: error scoreboard: {e}")
    return

if not games:
    return

today_fmt = date.today().strftime("%A %d/%m/%Y").capitalize()
header = (
    f"🌅 *RESUMEN MATUTINO NBA*\n"
    f"_{today_fmt}_\n"
    f"{'─'*32}"
)

# ── Partidos del día ──
game_lines = ["\n🏀 *PARTIDOS HOY:*"]
for g in games:
    away = g.get("awayTeam", {})
    home = g.get("homeTeam", {})
    at   = away.get("teamTricode","?")
    ht   = home.get("teamTricode","?")
    aw   = away.get("wins",0); al = away.get("losses",0)
    hw   = home.get("wins",0); hl = home.get("losses",0)
    st   = g.get("gameStatusText","")
    game_lines.append(f"  • *{at}* ({aw}-{al}) @ *{ht}* ({hw}-{hl}) - _{st}_")

# ── Injury report rápido (solo bajas confirmadas) ──
injury_lines = ["\n🏥 *INJURY REPORT:*"]
injury_found = False
for g in games[:4]:  # limitamos para no tardar mucho
    gid = g.get("gameId","")
    at  = (g.get("awayTeam") or {}).get("teamTricode","")
    ht  = (g.get("homeTeam") or {}).get("teamTricode","")
    if not gid:
        continue
    try:
        box_data = await asyncio.to_thread(fetch_boxscore_injury_data, gid)
        for tri in [at, ht]:
            pls = box_data.get(tri, [])
            out_pls = [p for p in pls
                       if p.get("status","").lower() in ("inactive","out")
                       and p.get("not_playing_reason","")]
            if out_pls:
                injury_found = True
                names = ", ".join(
                    f"*{p['name']}* _{p.get('not_playing_reason','')[:20]}_"
                    for p in out_pls[:3]
                )
                injury_lines.append(f"  🔴 {tri}: {names}")
    except Exception:
        pass

if not injury_found:
    injury_lines.append("  ✅ Sin bajas confirmadas (datos tempranos)")

# ── Top 5 props del día ──
props_lines = ["\n🏆 *TOP 5 PROPS RECOMENDADAS:*"]
try:
    props_pm = await asyncio.to_thread(polymarket_props_today_from_scoreboard)
    props_over = [p for p in props_pm if p.side == "over"][:40]  # limitar

    # Obtener rival para cada prop desde el slug
    def _get_opp(p: Prop) -> Tuple[str, bool]:
        parts = (p.game_slug or "").replace("nba-","").split("-")
        return (parts[1].upper() if len(parts) >= 2 else "", False)

    sem = asyncio.Semaphore(3)

    async def _score_prop(p: Prop):
        async with sem:
            opp, is_home = _get_opp(p)
            try:
                entry = await asyncio.wait_for(
                    asyncio.to_thread(_compute_pre_v2_for_player,
                                      p.player, p.tipo, p.line, p.source, opp, is_home),
                    timeout=20.0
                )
                return p, entry.get("pre_over", 0), entry.get("meta_over", {})
            except Exception:
                return p, 0, {}

    scored = await asyncio.gather(*[_score_prop(p) for p in props_over])
    scored = [(pr, sc, mt) for pr, sc, mt in scored if sc >= 60]
    scored.sort(key=lambda x: x[1], reverse=True)

    tipo_icon = {"puntos":"🏀","rebotes":"💪","asistencias":"🎯"}
    for i, (prop, score, meta) in enumerate(scored[:5], 1):
        matchup  = _slug_to_matchup(prop.game_slug or "")
        avg10    = meta.get("avg10")
        avg_str  = f"prom `{avg10:.1f}`" if avg10 else ""
        adjs     = meta.get("v2_adjustments", [])
        adj_str  = f" · _{', '.join(adjs[:2])}_" if adjs else ""
        icon     = tipo_icon.get(prop.tipo,"•")
        props_lines.append(
            f"  *{i}.* {_pre_rating_emoji(score)} `{score}/100` "
            f"- *{prop.player}*\n"
            f"     {icon} {prop.tipo.upper()} OVER `{prop.line}` _{matchup}_\n"
            f"     {avg_str}{adj_str}"
        )
except Exception as e:
    log.warning(f"Morning digest props: {e}")
    props_lines.append("  _Sin datos de props disponibles_")

footer = (
    f"\n{'─'*32}\n"
    f"_Usa /odds para ver todas · /lineup para alineaciones_"
)

full_msg = header + "\n".join(game_lines) + "\n".join(injury_lines) + "\n".join(props_lines) + footer
if len(full_msg) > 3900:
    full_msg = full_msg[:3900] + "\n..."

try:
    await context.bot.send_message(chat_id=chat_id, text=full_msg, parse_mode=ParseMode.MARKDOWN)
    log.info(f"Morning digest enviado a {chat_id}")
except Exception as e:
    log.warning(f"Error enviando morning digest: {e}")
```

async def background_check_morning(context: ContextTypes.DEFAULT_TYPE):
“””
Job que corre cada hora y dispara el digest matutino
cuando es la hora configurada.
“””
from datetime import datetime
current_hour = datetime.now().hour
if current_hour == MORNING_DIGEST_HOUR:
await send_morning_digest(context)

# ================================================================

# BLOQUE D - AUTO-RESOLUCIÓN DE APUESTAS

# ================================================================

async def background_autoresolve_bets(context: ContextTypes.DEFAULT_TYPE):
“””
Job periódico: busca apuestas pendientes y las resuelve automáticamente
cuando el partido ya terminó (gameStatus == 3).
“””
chat_id = context.job.chat_id
bets    = load_bets()
pending = [b for b in bets if not b.result]
if not pending:
return

```
try:
    games = await asyncio.to_thread(
        lambda: scoreboard.ScoreBoard().get_dict()["scoreboard"]["games"]
    )
except Exception:
    return

# Índice de partidos finalizados: slug → game_id
finished: Dict[str, str] = {}
for g in games:
    if g.get("gameStatus") == 3:
        slug = _slug_from_scoreboard_game(g)
        finished[slug] = g.get("gameId","")

if not finished:
    return

resolved_any = False
for bet in pending:
    if bet.game_slug not in finished:
        continue

    gid = finished[bet.game_slug]
    try:
        box = await asyncio.to_thread(
            lambda gid=gid: boxscore.BoxScore(gid).get_dict()["game"]
        )
    except Exception:
        continue

    # Buscar el jugador en el boxscore final
    pid = get_pid_for_name(bet.player)
    actual_stat: Optional[float] = None

    for team_key in ["homeTeam", "awayTeam"]:
        for pl in box.get(team_key, {}).get("players", []):
            if pl.get("personId") == pid:
                s = pl.get("statistics", {})
                stat_map = {
                    "puntos":     float(s.get("points",0) or 0),
                    "rebotes":    float(s.get("reboundsTotal",0) or 0),
                    "asistencias":float(s.get("assists",0) or 0),
                }
                actual_stat = stat_map.get(bet.tipo)
                break
        if actual_stat is not None:
            break

    if actual_stat is None:
        continue

    # Determinar resultado
    if bet.side == "over":
        result = "win" if actual_stat > bet.line else ("push" if actual_stat == bet.line else "loss")
    else:
        result = "win" if actual_stat < bet.line else ("push" if actual_stat == bet.line else "loss")

    bet.result       = result
    bet.actual_stat  = actual_stat
    bet.resolved_at  = now_ts()
    resolved_any     = True

    emoji = {"win":"✅","loss":"❌","push":"🔁"}.get(result,"❓")
    tipo_icon = {"puntos":"🏀","rebotes":"💪","asistencias":"🎯"}.get(bet.tipo,"•")
    msg = (
        f"🤖 *AUTO-RESULTADO* `#{bet.id}`\n"
        f"👤 *{bet.player}*  {tipo_icon} {bet.tipo.upper()} "
        f"{bet.side.upper()} `{bet.line}`\n"
        f"📊 Real: `{actual_stat:.0f}` → {emoji} *{result.upper()}*\n"
        f"💰 `{bet.amount}` unidades"
    )
    try:
        await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        log.warning(f"Auto-resolve send error: {e}")

if resolved_any:
    save_bets(bets)
```

# ================================================================

# COMANDO /contexto - ver contexto defensivo de un partido

# ================================================================

async def cmd_contexto(update: Update, context: ContextTypes.DEFAULT_TYPE):
“””
/contexto BOS DEN - contexto defensivo, pace y stats permitidas
de ambos equipos para el partido de hoy.
“””
args = context.args or []
if len(args) < 2:
await update.message.reply_text(
“Uso: `/contexto AWAY HOME`\nEj: `/contexto BOS DEN`”,
parse_mode=ParseMode.MARKDOWN
)
return

```
away_tri = args[0].upper()
home_tri = args[1].upper()

msg_wait = await update.message.reply_text(
    f"⏳ Cargando contexto *{away_tri} @ {home_tri}*...",
    parse_mode=ParseMode.MARKDOWN
)

def _fetch():
    ts   = fetch_league_team_stats()
    _    = fetch_opp_position_stats()
    away = {
        "ctx_pts":  get_defensive_context(home_tri, "puntos"),
        "ctx_reb":  get_defensive_context(home_tri, "rebotes"),
        "ctx_ast":  get_defensive_context(home_tri, "asistencias"),
    }
    home = {
        "ctx_pts":  get_defensive_context(away_tri, "puntos"),
        "ctx_reb":  get_defensive_context(away_tri, "rebotes"),
        "ctx_ast":  get_defensive_context(away_tri, "asistencias"),
    }
    return away, home

away_ctx, home_ctx = await asyncio.to_thread(_fetch)

def _fmt_team(tri: str, ctx_dict: dict, label: str) -> str:
    lines = [f"*{label} - {tri} (defensivamente)*"]
    for tipo, key in [("Puntos","ctx_pts"),("Rebotes","ctx_reb"),("Asistencias","ctx_ast")]:
        ctx = ctx_dict[key]
        dr  = ctx.get("def_rating"); dr_r = ctx.get("def_rank","?")
        pr  = ctx.get("pace");       pr_r = ctx.get("pace_rank","?")
        os  = ctx.get("opp_stat");   osr  = ctx.get("opp_stat_rank","?")
        os_str = f"`{os:.1f}` (rank #{osr})" if os else "-"
        lines.append(
            f"  🏷️ *{tipo}* permitidos: {os_str}\n"
            f"     Def Rating `{dr:.1f}` #{dr_r} · Pace `{pr:.1f}` #{pr_r}\n"
            f"     _{ctx.get('verdict','')}_"
        )
    return "\n".join(lines)

away_block = _fmt_team(home_tri, away_ctx, "Defensa del rival de los visitantes")
home_block = _fmt_team(away_tri, home_ctx, "Defensa del rival de los locales")

full = (
    f"🛡️ *CONTEXTO: {away_tri} @ {home_tri}*\n{'─'*30}\n\n"
    f"{away_block}\n\n{'─'*30}\n\n{home_block}\n\n"
    f"_Rank #1 = mejor defensa / permite menos_"
)

if len(full) > 3900:
    full = full[:3900] + "\n..."
await msg_wait.edit_text(full, parse_mode=ParseMode.MARKDOWN)
```

# =========================

# Main

# =========================

# ================================================================

# SISTEMA MULTI-USUARIO

# ================================================================

USERS_FILE = “users.json”

# Si defines ADMIN_ID en Railway, ese user_id es el admin automáticamente.

ADMIN_ID = int(os.environ.get(“ADMIN_ID”, “0”))

def load_users() -> dict:
“””
Estructura: {
“allowed”: [user_id, …],       # usuarios autorizados
“admins”:  [user_id, …],       # pueden /adduser /removeuser
“nicknames”: {user_id: name}     # nombres amigables
}
“””
return load_json(USERS_FILE, {“allowed”: [], “admins”: [], “nicknames”: {}})

def save_users(data: dict):
save_json(USERS_FILE, data)

def is_allowed(user_id: int) -> bool:
“””¿Está autorizado este usuario?”””
users = load_users()
# Si no hay nadie en la lista, el primero que arranque es libre
if not users[“allowed”] and not users[“admins”]:
return True
return user_id in users[“allowed”] or user_id in users[“admins”] or user_id == ADMIN_ID

def is_admin(user_id: int) -> bool:
users = load_users()
return user_id in users[“admins”] or user_id == ADMIN_ID

def add_user(user_id: int, nickname: str = “”, admin: bool = False) -> bool:
users = load_users()
if user_id not in users[“allowed”]:
users[“allowed”].append(user_id)
if admin and user_id not in users[“admins”]:
users[“admins”].append(user_id)
if nickname:
users[“nicknames”][str(user_id)] = nickname
save_users(users)
return True

def remove_user(user_id: int) -> bool:
users = load_users()
if user_id in users[“allowed”]:
users[“allowed”].remove(user_id)
if user_id in users[“admins”]:
users[“admins”].remove(user_id)
users[“nicknames”].pop(str(user_id), None)
save_users(users)
return True

def user_display(user_id: int) -> str:
users = load_users()
nick = users[“nicknames”].get(str(user_id))
return nick if nick else f”#{user_id}”

async def guard(update: Update) -> bool:
“””
Middleware de autorización. Retorna True si el usuario puede continuar.
Si no, le explica cómo solicitar acceso.
“””
uid = update.effective_user.id if update.effective_user else 0
if is_allowed(uid):
return True
uname = update.effective_user.username or update.effective_user.first_name or str(uid)
await update.message.reply_text(
f”🔒 *Acceso restringido*\n\n”
f”Tu ID es: `{uid}`\n”
f”Pide al admin que ejecute:\n”
f”`/adduser {uid} TuNombre`”,
parse_mode=ParseMode.MARKDOWN
)
log.info(f”Acceso denegado a @{uname} (id={uid})”)
return False

# Comandos de gestión de usuarios

async def cmd_adduser(update: Update, context: ContextTypes.DEFAULT_TYPE):
“”“Solo admins. /adduser USER_ID Nombre”””
uid   = update.effective_user.id if update.effective_user else 0
users = load_users()

```
# Permitir si: es admin, coincide con ADMIN_ID del env, o no hay ningún admin aún
no_admins_yet = not users["admins"] and (ADMIN_ID == 0 or ADMIN_ID not in users["allowed"])
if not is_admin(uid) and not no_admins_yet:
    await update.message.reply_text(
        f"⛔ Solo los admins pueden hacer esto.\n"
        f"Tu ID: `{uid}`\n\n"
        f"_Si eres el dueño del bot, agrega `ADMIN_ID={uid}` en las variables de entorno de Railway y reinicia._",
        parse_mode=ParseMode.MARKDOWN
    )
    return

# Si no había admins, este usuario se convierte en admin también
if no_admins_yet:
    add_user(uid, "", admin=True)

args = context.args or []
if not args:
    await update.message.reply_text(
        "Uso: `/adduser USER_ID Nombre`\n"
        "Ej: `/adduser 123456789 Carlos`\n\n"
        "_El user puede encontrar su ID arrancando el bot_",
        parse_mode=ParseMode.MARKDOWN
    )
    return

try:
    target_id = int(args[0])
except ValueError:
    await update.message.reply_text("El ID debe ser un número.")
    return

nickname = " ".join(args[1:]) if len(args) > 1 else ""
add_user(target_id, nickname)
nick_str = f" (*{nickname}*)" if nickname else ""
await update.message.reply_text(
    f"✅ Usuario `{target_id}`{nick_str} añadido.\n"
    f"Que ejecute `/start` en el bot para activar.",
    parse_mode=ParseMode.MARKDOWN
)
```

async def cmd_removeuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
“”“Solo admins. /removeuser USER_ID”””
uid = update.effective_user.id if update.effective_user else 0
if not is_admin(uid):
await update.message.reply_text(“⛔ Solo los admins pueden hacer esto.”)
return

```
args = context.args or []
if not args:
    await update.message.reply_text("Uso: `/removeuser USER_ID`", parse_mode=ParseMode.MARKDOWN)
    return

try:
    target_id = int(args[0])
except ValueError:
    await update.message.reply_text("El ID debe ser un número.")
    return

remove_user(target_id)
await update.message.reply_text(f"✅ Usuario `{target_id}` eliminado.", parse_mode=ParseMode.MARKDOWN)
```

async def cmd_usuarios(update: Update, context: ContextTypes.DEFAULT_TYPE):
“”“Solo admins. Lista todos los usuarios autorizados.”””
uid = update.effective_user.id if update.effective_user else 0
if not is_admin(uid):
await update.message.reply_text(“⛔ Solo los admins pueden hacer esto.”)
return

```
users = load_users()
lines = ["👥 *Usuarios autorizados:*\n"]

for u_id in users.get("allowed", []):
    nick   = users["nicknames"].get(str(u_id), "-")
    is_adm = "👑 " if u_id in users.get("admins", []) else "• "
    lines.append(f"{is_adm}`{u_id}` - {nick}")

if ADMIN_ID and ADMIN_ID not in users.get("allowed", []):
    lines.append(f"👑 `{ADMIN_ID}` - Admin (env)")

if len(lines) == 1:
    lines.append("_Sin usuarios registrados aún_")

lines.append(f"\n_Total: {len(users.get('allowed',[]))} usuarios_")
await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
```

async def cmd_miperfil(update: Update, context: ContextTypes.DEFAULT_TYPE):
“”“Muestra el ID y perfil del usuario actual.”””
if not await guard(update): return
uid   = update.effective_user.id if update.effective_user else 0
uname = update.effective_user.username or “-”
fname = update.effective_user.first_name or “-”
nick  = user_display(uid)
adm   = “👑 Admin” if is_admin(uid) else “👤 Usuario”

```
bets  = load_bets()
mine  = [b for b in bets if b.user_id == uid]
won   = sum(1 for b in mine if b.result == "win")
lost  = sum(1 for b in mine if b.result == "loss")
pend  = sum(1 for b in mine if not b.result)

await update.message.reply_text(
    f"👤 *Mi perfil*\n"
    f"{'─'*24}\n"
    f"ID: `{uid}`\n"
    f"Username: @{uname}\n"
    f"Nombre: {fname}\n"
    f"Alias en el bot: *{nick}*\n"
    f"Rol: {adm}\n\n"
    f"📊 *Apuestas:* {won}W / {lost}L / {pend} pendientes\n"
    f"_Usa `/historial` para estadísticas completas_",
    parse_mode=ParseMode.MARKDOWN
)
```

# ================================================================

# MENÚ DE COMANDOS (se muestra en Telegram como lista desplegable)

# ================================================================

BOT_COMMANDS = [
BotCommand(“start”,       “Activar el bot y todos los jobs”),
BotCommand(“games”,       “Partidos NBA de hoy”),
BotCommand(“signals”,     “🆕 Señales pregame con edge (roadmap)”),
BotCommand(“dashboard”,   “🆕 Métricas: win rate, edge, ROI”),
BotCommand(“status”,      “🆕 Health check del sistema”),
BotCommand(“odds”,        “Props con score PRE (todas)”),
BotCommand(“alertas”,     “Top props recomendadas hoy”),
BotCommand(“live”,        “Props en vivo con scoring”),
BotCommand(“lineup”,      “Alineaciones + injury report”),
BotCommand(“analisis”,    “Análisis profundo de un prop”),
BotCommand(“contexto”,    “Contexto defensivo de un partido”),
BotCommand(“bet”,         “Registrar apuesta”),
BotCommand(“misapuestas”, “Ver apuestas pendientes”),
BotCommand(“historial”,   “ROI y estadísticas de apuestas”),
BotCommand(“resultado”,   “Cerrar apuesta manualmente”),
BotCommand(“miperfil”,    “Ver mi ID y perfil”),
BotCommand(“help”,        “Lista de comandos”),
]

BOT_COMMANDS_ADMIN = BOT_COMMANDS + [
BotCommand(“adduser”,    “Añadir usuario autorizado”),
BotCommand(“removeuser”, “Eliminar usuario”),
BotCommand(“usuarios”,   “Ver todos los usuarios”),
BotCommand(“debug”,      “Estado técnico Polymarket”),
BotCommand(“add”,        “Añadir prop manual”),
]

async def on_startup(app: Application):
“”“Inicializa DB y registra comandos en Telegram al arrancar.”””
try:
db_init()
log.info(“SQLite inicializado ✅”)
except Exception as e:
log.error(f”Error inicializando DB: {e}”)

```
try:
    await app.bot.set_my_commands(BOT_COMMANDS)
    log.info("Comandos registrados en Telegram ✅")
except Exception as e:
    log.warning(f"Error registrando comandos: {e}")
log.info("Bot arrancado.")
```

async def register_job(update: Update, context: ContextTypes.DEFAULT_TYPE):
uid     = update.effective_user.id if update.effective_user else 0
uname   = update.effective_user.first_name or str(uid)
chat_id = update.effective_chat.id

```
users = load_users()

# Caso 1: el ID coincide con ADMIN_ID del entorno → siempre admin
if ADMIN_ID and uid == ADMIN_ID:
    add_user(uid, uname, admin=True)

# Caso 2: no hay absolutamente ningún usuario registrado → primer usuario = admin
elif not users["allowed"] and not users["admins"]:
    add_user(uid, uname, admin=True)
    await update.message.reply_text(
        f"👑 *Primer usuario - eres Admin automáticamente*\n"
        f"Tu ID: `{uid}`\n"
        f"Para invitar amigos: `/adduser ID Nombre`",
        parse_mode=ParseMode.MARKDOWN
    )

# Caso 3: no tiene acceso
elif not is_allowed(uid):
    await update.message.reply_text(
        f"🔒 *Acceso restringido*\n\n"
        f"Tu ID: `{uid}`\n"
        f"Comparte este ID con el admin para que ejecute:\n"
        f"`/adduser {uid} {uname}`",
        parse_mode=ParseMode.MARKDOWN
    )
    return

# Caso 4: ya tiene acceso → actualizar nickname
else:
    add_user(uid, uname)

# Job 1: scan en vivo
if not context.job_queue.get_jobs_by_name(f"scan:{chat_id}"):
    context.job_queue.run_repeating(
        background_scan, interval=POLL_SECONDS, first=5,
        chat_id=chat_id, name=f"scan:{chat_id}",
    )

# Job 2: alertas pre-partido
if not context.job_queue.get_jobs_by_name(f"smart:{chat_id}"):
    context.job_queue.run_repeating(
        background_smart_alerts, interval=30*60, first=20,
        chat_id=chat_id, name=f"smart:{chat_id}",
    )

# Job 3: resumen matutino
if not context.job_queue.get_jobs_by_name(f"morning:{chat_id}"):
    context.job_queue.run_repeating(
        background_check_morning, interval=60*60, first=60,
        chat_id=chat_id, name=f"morning:{chat_id}",
    )

# Job 4: auto-resolución de apuestas
if not context.job_queue.get_jobs_by_name(f"autoresolve:{chat_id}"):
    context.job_queue.run_repeating(
        background_autoresolve_bets, interval=20*60, first=120,
        chat_id=chat_id, name=f"autoresolve:{chat_id}",
    )

await update.message.reply_text(
    f"✅ *¡Bienvenido, {uname}!*\n"
    f"Todos los jobs activados.\n\n"
    f"Toca el 📎 menú o escribe `/` para ver los comandos disponibles.",
    parse_mode=ParseMode.MARKDOWN
)
await cmd_help(update, context)
```

def main():
app = Application.builder().token(TELEGRAM_TOKEN).build()

```
# ── Wrapper de autorización para todos los comandos ──
def guarded(fn):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await guard(update):
            return
        return await fn(update, context)
    wrapper.__name__ = fn.__name__
    return wrapper

# Principales
app.add_handler(CommandHandler("start",        register_job))
app.add_handler(CommandHandler("help",         guarded(cmd_help)))
app.add_handler(CommandHandler("games",        guarded(cmd_games)))
app.add_handler(CommandHandler("today",        guarded(cmd_games)))
app.add_handler(CommandHandler("odds",         guarded(cmd_odds)))
app.add_handler(CommandHandler("live",         guarded(cmd_live)))
app.add_handler(CommandHandler("lineup",       guarded(cmd_lineup)))

# Señales del roadmap
app.add_handler(CommandHandler("signals",      guarded(cmd_signals)))
app.add_handler(CommandHandler("dashboard",    guarded(cmd_dashboard)))
app.add_handler(CommandHandler("status",       guarded(cmd_status)))

# Análisis
app.add_handler(CommandHandler("analisis",     guarded(cmd_analisis)))
app.add_handler(CommandHandler("alertas",      guarded(cmd_alertas)))
app.add_handler(CommandHandler("contexto",     guarded(cmd_contexto)))

# Apuestas
app.add_handler(CommandHandler("bet",          guarded(cmd_bet)))
app.add_handler(CommandHandler("resultado",    guarded(cmd_resultado)))
app.add_handler(CommandHandler("historial",    guarded(cmd_historial)))
app.add_handler(CommandHandler("misapuestas",  guarded(cmd_misapuestas)))

# Perfil
app.add_handler(CommandHandler("miperfil",     guarded(cmd_miperfil)))

# Admin
app.add_handler(CommandHandler("adduser",      cmd_adduser))
app.add_handler(CommandHandler("removeuser",   cmd_removeuser))
app.add_handler(CommandHandler("usuarios",     cmd_usuarios))
app.add_handler(CommandHandler("debug",        guarded(cmd_debug)))
app.add_handler(CommandHandler("add",          guarded(cmd_add)))

app.post_init = on_startup
app.run_polling(allowed_updates=Update.ALL_TYPES)
```

if **name** == “**main**”:
main()

async def register_job(update: Update, context: ContextTypes.DEFAULT_TYPE):
chat_id = update.effective_chat.id

```
# Job 1: scan en vivo (cada POLL_SECONDS)
if not context.job_queue.get_jobs_by_name(f"scan:{chat_id}"):
    context.job_queue.run_repeating(
        background_scan, interval=POLL_SECONDS, first=5,
        chat_id=chat_id, name=f"scan:{chat_id}",
    )
    await update.message.reply_text(f"✅ Scan en vivo activado (cada {POLL_SECONDS}s).")

# Job 2: alertas pre-partido (cada 30 min)
if not context.job_queue.get_jobs_by_name(f"smart:{chat_id}"):
    context.job_queue.run_repeating(
        background_smart_alerts, interval=30*60, first=15,
        chat_id=chat_id, name=f"smart:{chat_id}",
    )
    await update.message.reply_text("✅ Alertas pre-partido activadas.")

# Job 3: resumen matutino (check cada hora)
if not context.job_queue.get_jobs_by_name(f"morning:{chat_id}"):
    context.job_queue.run_repeating(
        background_check_morning, interval=60*60, first=30,
        chat_id=chat_id, name=f"morning:{chat_id}",
    )
    await update.message.reply_text(f"✅ Resumen matutino activado (a las {MORNING_DIGEST_HOUR}:00h).")

# Job 4: auto-resolución de apuestas (cada 20 min)
if not context.job_queue.get_jobs_by_name(f"autoresolve:{chat_id}"):
    context.job_queue.run_repeating(
        background_autoresolve_bets, interval=20*60, first=60,
        chat_id=chat_id, name=f"autoresolve:{chat_id}",
    )
    await update.message.reply_text("✅ Auto-resolución de apuestas activada.")

await cmd_help(update, context)
```

def main():
app = Application.builder().token(TELEGRAM_TOKEN).build()

```
# Principales
app.add_handler(CommandHandler("start",        register_job))
app.add_handler(CommandHandler("help",         cmd_help))
app.add_handler(CommandHandler("games",        cmd_games))
app.add_handler(CommandHandler("today",        cmd_games))
app.add_handler(CommandHandler("odds",         cmd_odds))
app.add_handler(CommandHandler("live",         cmd_live))
app.add_handler(CommandHandler("lineup",       cmd_lineup))
app.add_handler(CommandHandler("debug",        cmd_debug))

# Análisis avanzado
app.add_handler(CommandHandler("analisis",     cmd_analisis))
app.add_handler(CommandHandler("alertas",      cmd_alertas))
app.add_handler(CommandHandler("contexto",     cmd_contexto))

# Tracking de apuestas
app.add_handler(CommandHandler("bet",          cmd_bet))
app.add_handler(CommandHandler("resultado",    cmd_resultado))
app.add_handler(CommandHandler("historial",    cmd_historial))
app.add_handler(CommandHandler("misapuestas",  cmd_misapuestas))

# Manual
app.add_handler(CommandHandler("add",          cmd_add))

app.post_init = on_startup
app.run_polling(allowed_updates=Update.ALL_TYPES)
```

if **name** == “**main**”:
main()
