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
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Tuple
from datetime import date, datetime

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from telegram import Update, BotCommand
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from nba_api.live.nba.endpoints import scoreboard, boxscore
from nba_api.stats.static import players, teams as nba_teams_static
from nba_api.stats.endpoints import commonteamroster

# ========================= CONFIG =========================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("nba-bot")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
if not TELEGRAM_TOKEN:
    raise RuntimeError("Missing TELEGRAM_TOKEN env var")

POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "120"))
SEASON = os.environ.get("NBA_SEASON", "2025-26")
GAMMA = "https://gamma-api.polymarket.com"

FINAL_ALERT_THRESHOLD = 75
EDGE_THRESH_PREGAME = float(os.environ.get("EDGE_THRESH_PREGAME", "7.0"))
MIN_LIQUIDITY_USD = 8000
MIN_VOLUME_24H = 5000

# ========================= DATACLASSES =========================
@dataclass
class Prop:
    player: str
    tipo: str
    line: float
    side: str
    source: str = "polymarket"
    game_slug: Optional[str] = None
    market_id: Optional[str] = None
    implied_prob: float = 0.5      # PRECIO REAL DE POLYMARKET
    liquidity: float = 0.0
    volume_24h: float = 0.0

@dataclass
class Signal:
    signal_id: str
    ts: int
    kind: str
    player: str
    player_id: Optional[int]
    market: str
    line: float
    side: str
    game_slug: str
    implied_prob: float
    model_prob: float
    edge: float
    confidence: int
    reason_codes: List[str]
    risk_flags: List[str]
    level: str
    result: Optional[str] = None
    actual_stat: Optional[float] = None
    resolved_at: Optional[int] = None
    market_id: str = ""
    source: str = "polymarket"

# ========================= NORMALIZACIÓN Y HELPERS (todo tu código original) =========================
_NAME_SUFFIXES = re.compile(r'\b(jr.?|sr.?|ii|iii|iv)\s*$', re.IGNORECASE)

def _strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')

def normalize_name(name: str) -> str:
    if not name: return ""
    n = _strip_accents(name.strip())
    n = _NAME_SUFFIXES.sub('', n).strip()
    n = re.sub(r'[^a-z0-9 ]', '', n.lower())
    return re.sub(r'\s+', ' ', n).strip()

_NAME_ALIASES = { ... }  # ← pega aquí tu diccionario completo de aliases

def resolve_player_name(raw_name: str) -> str:
    n = normalize_name(raw_name)
    return _NAME_ALIASES.get(n, n)

# (Todas tus funciones: fuzzy_match_player, db_connect, db_init, db_save_signal, etc. permanecen exactamente igual)

# ========================= POLYMARKET CON PRECIO REAL =========================
def polymarket_props_today_from_scoreboard() -> List[Prop]:
    props = []
    try:
        r = requests.get(f"{GAMMA}/events", params={"tag_slug": "nba", "closed": "false", "limit": 200}, timeout=25)
        events = r.json() if isinstance(r.json(), list) else r.json().get("events", [])

        for ev in events:
            markets = ev.get("markets", []) or requests.get(f"{GAMMA}/markets", params={"event_id": ev.get("id")}).json()
            for m in markets:
                q = (m.get("question") or m.get("title", "")).lower()
                if not any(s in q for s in ["points", "rebounds", "assists"]): continue

                match = re.search(r'(.+?)\s+(points|rebounds|assists)\s*(?:o/u|over/under)?\s*(\d+\.?\d*)', q, re.I)
                if not match: continue

                player = match.group(1).strip()
                tipo_raw = match.group(2).lower()
                line = float(match.group(3))

                tipo = {"points": "puntos", "rebounds": "rebotes", "assists": "asistencias"}.get(tipo_raw)
                if not tipo: continue

                prices = m.get("outcomePrices", ["0.5", "0.5"])
                over_price = float(prices[0]) if prices else 0.5

                liquidity = float(m.get("liquidity", 0) or 0)
                volume = float(m.get("volume_24hr", 0) or m.get("volume", 0) or 0)

                if liquidity < MIN_LIQUIDITY_USD or volume < MIN_VOLUME_24H:
                    continue

                props.append(Prop(
                    player=player, tipo=tipo, line=line, side="over",
                    implied_prob=over_price, liquidity=liquidity, volume_24h=volume,
                    game_slug=ev.get("slug"), market_id=str(m.get("id", ""))
                ))
    except Exception as e:
        log.warning(f"Polymarket error: {e}")

    if not props:
        log.warning("Usando FALLBACK_PROPS")
        props = FALLBACK_PROPS[:]   # tu lista original

    return props

# ========================= BUILD SIGNAL CON EDGE REAL =========================
def build_pregame_signal(p: Prop, pid: int) -> Optional[Signal]:
    v10 = last_n_values(pid, p.tipo, 10)
    if len(v10) < 5: return None

    avg10 = sum(v10) / len(v10)
    std10 = stdev(v10)
    model_prob = model_probability(avg10, std10, p.line, "over")

    edge = (model_prob - p.implied_prob) * 100
    conf, _ = pre_score(pid, p.tipo, p.line, "over")

    if edge < EDGE_THRESH_PREGAME or conf < 60:
        return None

    return Signal(
        signal_id=_signal_id(p.player, p.tipo, p.line, "over", "pregame"),
        ts=int(time.time()),
        kind="pregame",
        player=normalize_name(p.player),
        player_id=pid,
        market=p.tipo,
        line=p.line,
        side="over",
        game_slug=p.game_slug or "",
        implied_prob=round(p.implied_prob, 3),
        model_prob=round(model_prob, 3),
        edge=round(edge, 1),
        confidence=conf,
        reason_codes=["edge_real", "liquidity_ok"],
        risk_flags=[],
        level="entry" if edge >= 10 else "watch",
        market_id=p.market_id
    )

# ========================= NUEVOS COMANDOS =========================
async def cmd_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔍 Buscando value bets en Polymarket...", parse_mode=ParseMode.MARKDOWN)
    props = await asyncio.to_thread(polymarket_props_today_from_scoreboard)
    signals = []
    for p in props:
        pid = get_pid_for_name(p.player)
        if pid:
            sig = build_pregame_signal(p, pid)
            if sig: signals.append(sig)
    signals.sort(key=lambda s: s.edge, reverse=True)
    lines = ["🏆 *TOP VALUE BETS POLYMARKET*\n"]
    for s in signals[:12]:
        lines.append(f"🔥 `{s.edge:+.1f}%` | `{s.confidence}/100` | {s.player} {s.market} O {s.line}")
    await msg.edit_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def cmd_mercados(update: Update, context: ContextTypes.DEFAULT_TYPE):
    props = await asyncio.to_thread(polymarket_props_today_from_scoreboard)
    lines = ["📋 *MERCADOS POLYMARKET HOY*\n"]
    for p in sorted(props, key=lambda x: x.liquidity, reverse=True)[:30]:
        lines.append(f"• {p.player} {p.tipo} O {p.line} → `{p.implied_prob*100:.1f}%` | Liq ${p.liquidity:,.0f}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

# ========================= MAIN (todo integrado) =========================
async def on_startup(app: Application):
    db_init()
    await app.bot.set_my_commands([
        BotCommand("start", "Activar bot"),
        BotCommand("value", "🔥 Mejores value bets"),
        BotCommand("mercados", "Ver todos los mercados"),
        BotCommand("signals", "Señales pregame"),
        BotCommand("odds", "Props con PRE"),
        BotCommand("live", "Live scoring"),
        BotCommand("dashboard", "Estadísticas"),
        # ... todos tus comandos originales ...
    ])

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Todos tus handlers originales + los nuevos
    app.add_handler(CommandHandler("value", cmd_value))
    app.add_handler(CommandHandler("mercados", cmd_mercados))
    # app.add_handler(CommandHandler("signals", cmd_signals))  # tu función original
    # ... resto de tus handlers ...

    app.post_init = on_startup
    app.run_polling()

if __name__ == "__main__":
    main()
