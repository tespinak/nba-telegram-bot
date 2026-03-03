"""
NBA Props Bot — refactorizado
Secciones: CONFIG · TIPOS · CACHE · NBA API · POLYMARKET · SCORING · SEÑALES · HANDLERS · JOBS · MAIN
"""
import asyncio, hashlib, json, logging, math, os, re, sqlite3, time, unicodedata
from dataclasses import dataclass, asdict, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from telegram import Update, BotCommand
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from nba_api.live.nba.endpoints import scoreboard, boxscore
from nba_api.stats.static import players as nba_players_static, teams as nba_teams_static
from nba_api.stats.endpoints import commonteamroster

# ═══════════════════════════════════════════════════════════════
# 1. CONFIG
# ═══════════════════════════════════════════════════════════════
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("nba-bot")

TOKEN        = os.environ.get("TELEGRAM_TOKEN", "").strip()
if not TOKEN: raise RuntimeError("Falta TELEGRAM_TOKEN")

POLL_SEC     = int(os.environ.get("POLL_SECONDS",   "120"))
SEASON       = os.environ.get("NBA_SEASON",         "2025-26")
DB_FILE      = os.environ.get("DB_FILE",            "nba_signals.db")
ADMIN_ID     = int(os.environ.get("ADMIN_ID",       "0"))
MORNING_HOUR = int(os.environ.get("MORNING_HOUR",   "10"))

EDGE_MIN_PRE  = float(os.environ.get("EDGE_THRESH_PREGAME", "4.0"))
EDGE_MIN_LIVE = float(os.environ.get("EDGE_THRESH_INGAME",  "6.0"))
CONF_MIN      = int(os.environ.get("CONF_THRESH_PREGAME",   "65"))
MAX_SIG_DAY   = int(os.environ.get("MAX_SIGNALS_DAY",       "20"))
MAX_SIG_PL    = int(os.environ.get("MAX_SIGNALS_PLAYER",     "2"))

COOLDOWN_SEC = 8 * 60
BLOWOUT_IS   = 20
BLOWOUT_STR  = 22
ALERT_THRESH = 75
ALERT_CLUTCH = 68

STAT_COL  = {"puntos": "PTS", "rebotes": "REB", "asistencias": "AST"}
STD_CAP   = {"puntos": 8.0, "rebotes": 4.0, "asistencias": 3.0}
MRG_CAP   = {"puntos": 8.0, "rebotes": 3.0, "asistencias": 3.0}
GAMMA_URL = "https://gamma-api.polymarket.com"
TIPO_ICON = {"puntos": "🏀", "rebotes": "💪", "asistencias": "🎯"}
TIPO_MAP  = {"points": "puntos", "rebounds": "rebotes", "assists": "asistencias"}

# Umbral de tiempo máximo que consideramos datos "frescos" (en segundos)
LIVE_STALE_SEC  = 90   # scoreboard
BOX_STALE_SEC   = 60   # boxscore
PM_TTL_SEC      = 8 * 60
GLOG_TTL_SEC    = 6 * 3600
CONTEXT_TTL_SEC = 4 * 3600

# ═══════════════════════════════════════════════════════════════
# 2. TIPOS / DATACLASSES
# ═══════════════════════════════════════════════════════════════
@dataclass
class Prop:
    player: str; tipo: str; line: float; side: str
    source: str = "manual"; game_slug: str = ""
    market_id: str = ""; added_by: int = 0; added_at: int = 0

@dataclass
class Signal:
    signal_id: str; ts: int; kind: str
    player: str; player_id: Optional[int]
    market: str; line: float; side: str; game_slug: str
    implied_prob: float; model_prob: float; edge: float; confidence: int
    reason_codes: List[str]; risk_flags: List[str]; level: str
    result: Optional[str] = None; actual_stat: Optional[float] = None
    resolved_at: Optional[int] = None; market_id: str = ""; source: str = "polymarket"

@dataclass
class Bet:
    id: str; user_id: int; player: str; tipo: str; side: str
    line: float; amount: float; pre_score: int; game_slug: str; placed_at: int
    result: Optional[str] = None; actual_stat: Optional[float] = None
    resolved_at: Optional[int] = None; notes: str = ""

@dataclass
class LiveSnapshot:
    """Estado del scoreboard en vivo con control de frescura."""
    ts: int
    games: List[dict]

    def is_fresh(self, max_age: int = LIVE_STALE_SEC) -> bool:
        return (time.time() - self.ts) < max_age

    def live_games(self) -> List[dict]:
        return [g for g in self.games if g.get("gameStatus") == 2]

    def pregame_games(self) -> List[dict]:
        return [g for g in self.games if g.get("gameStatus") == 1]

    def finished_games(self) -> List[dict]:
        return [g for g in self.games if g.get("gameStatus") == 3]

    def game_slug(self, g: dict) -> str:
        a = (g.get("awayTeam") or {}).get("teamTricode", "").lower()
        h = (g.get("homeTeam") or {}).get("teamTricode", "").lower()
        return f"nba-{a}-{h}-{date.today().isoformat()}"

@dataclass
class BoxSnapshot:
    """Boxscore de un partido con control de frescura."""
    ts: int; game_id: str; data: dict

    def is_fresh(self, max_age: int = BOX_STALE_SEC) -> bool:
        return (time.time() - self.ts) < max_age

    def players(self, team: str = "") -> List[dict]:
        rows = []
        for k in ("homeTeam", "awayTeam"):
            t = self.data.get(k, {})
            if not team or (t.get("teamTricode","") == team):
                rows += t.get("players", [])
        return rows

# ═══════════════════════════════════════════════════════════════
# 3. CACHE CENTRALIZADO
# ═══════════════════════════════════════════════════════════════
class Cache:
    """Cache genérico en memoria con TTL."""
    def __init__(self): self._store: Dict[str, dict] = {}

    def get(self, key: str, ttl: int):
        e = self._store.get(key)
        if e and (time.time() - e["ts"]) < ttl:
            return e["val"]
        return None

    def set(self, key: str, val):
        self._store[key] = {"ts": time.time(), "val": val}

    def clear(self, key: str):
        self._store.pop(key, None)

CACHE = Cache()

def _json_load(path: str, default):
    try:
        with open(path, encoding="utf-8") as f: return json.load(f)
    except: return default

def _json_save(path: str, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def now() -> int: return int(time.time())

# ═══════════════════════════════════════════════════════════════
# 4. HTTP SESSIONS
# ═══════════════════════════════════════════════════════════════
def _make_session(extra_headers: dict) -> requests.Session:
    base = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, */*", "Accept-Language": "es-ES,es;q=0.9",
    }
    s = requests.Session()
    retry = Retry(total=6, backoff_factor=1.2,
                  status_forcelist=(403,408,429,500,502,503,504),
                  allowed_methods=frozenset(["GET","POST"]))
    s.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20))
    s.headers.update({**base, **extra_headers})
    return s

NBA_SES = _make_session({
    "Referer": "https://www.nba.com/", "Origin": "https://www.nba.com",
    "x-nba-stats-origin": "stats", "x-nba-stats-token": "true",
})
PM_SES = _make_session({
    "Origin": "https://polymarket.com", "Referer": "https://polymarket.com/",
})

# ═══════════════════════════════════════════════════════════════
# 5. BASE DE DATOS SQLite
# ═══════════════════════════════════════════════════════════════
def db() -> sqlite3.Connection:
    c = sqlite3.connect(DB_FILE, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c

def db_init():
    conn = db(); conn.executescript("""
    CREATE TABLE IF NOT EXISTS signals (
        signal_id TEXT PRIMARY KEY, ts INT, kind TEXT, player TEXT, player_id INT,
        market TEXT, line REAL, side TEXT, game_slug TEXT, implied_prob REAL,
        model_prob REAL, edge REAL, confidence INT, reason_codes TEXT, risk_flags TEXT,
        level TEXT, result TEXT, actual_stat REAL, resolved_at INT, market_id TEXT,
        source TEXT, period INT, clock TEXT, score_diff INT);
    CREATE TABLE IF NOT EXISTS daily_risk (
        date_str TEXT PRIMARY KEY, signals_sent INT DEFAULT 0, player_counts TEXT DEFAULT '{}');
    CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);
    """); conn.commit(); conn.close()

def db_save_signal(sig: Signal, period=0, clock="", score_diff=0):
    conn = db()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO signals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sig.signal_id, sig.ts, sig.kind, sig.player, sig.player_id,
             sig.market, sig.line, sig.side, sig.game_slug,
             sig.implied_prob, sig.model_prob, sig.edge, sig.confidence,
             json.dumps(sig.reason_codes), json.dumps(sig.risk_flags),
             sig.level, sig.result, sig.actual_stat, sig.resolved_at,
             sig.market_id, sig.source, period, clock, score_diff))
        conn.commit()
    finally: conn.close()

def db_resolve(signal_id: str, result: str, actual: float):
    conn = db()
    conn.execute("UPDATE signals SET result=?,actual_stat=?,resolved_at=? WHERE signal_id=?",
                 (result, actual, now(), signal_id))
    conn.commit(); conn.close()

def db_get(days=30, player=None) -> List[dict]:
    conn = db(); cutoff = now() - days*86400
    q = "SELECT * FROM signals WHERE ts>=?"; p: list = [cutoff]
    if player: q += " AND player LIKE ?"; p.append(f"%{player.lower()}%")
    rows = [dict(r) for r in conn.execute(q+" ORDER BY ts DESC", p).fetchall()]
    conn.close(); return rows

def _daily_risk() -> dict:
    today = date.today().isoformat()
    conn = db()
    r = conn.execute("SELECT * FROM daily_risk WHERE date_str=?", (today,)).fetchone()
    conn.close()
    if not r: return {"date_str": today, "signals_sent": 0, "player_counts": {}}
    return {**dict(r), "player_counts": json.loads(r["player_counts"] or "{}")}

def _inc_risk(player: str):
    today = date.today().isoformat()
    risk  = _daily_risk(); risk["signals_sent"] += 1
    pc    = risk["player_counts"]; pc[player] = pc.get(player,0)+1
    conn  = db()
    conn.execute("""INSERT INTO daily_risk VALUES(?,?,?)
        ON CONFLICT(date_str) DO UPDATE SET signals_sent=excluded.signals_sent,player_counts=excluded.player_counts""",
        (today, risk["signals_sent"], json.dumps(pc)))
    conn.commit(); conn.close()

def risk_ok(player: str) -> Tuple[bool, str]:
    r = _daily_risk()
    if r["signals_sent"] >= MAX_SIG_DAY: return False, f"límite diario ({MAX_SIG_DAY})"
    if r["player_counts"].get(player, 0) >= MAX_SIG_PL: return False, f"máx por jugador ({MAX_SIG_PL})"
    return True, ""

# ═══════════════════════════════════════════════════════════════
# 6. HELPERS GENÉRICOS (subprogramas de apoyo)
# ═══════════════════════════════════════════════════════════════
def clamp(x, lo=0, hi=100): return max(lo, min(hi, x))

def stdev(vals: List[float]) -> float:
    if len(vals) < 2: return 0.0
    mu = sum(vals)/len(vals)
    return math.sqrt(sum((v-mu)**2 for v in vals)/(len(vals)-1))

def normal_cdf(x: float) -> float:
    return 0.5*(1.0+math.erf(x/math.sqrt(2.0)))

def model_prob(avg: float, std: float, line: float, side: str) -> float:
    std = max(std, avg*0.20, 1.0) if std <= 0 else std
    p   = 1.0 - normal_cdf((line-avg)/std)
    return p if side == "over" else 1.0-p

def parse_minutes(s) -> float:
    try: mm, ss = str(s).split(":"); return float(mm)+float(ss)/60
    except: return 0.0

def clock_to_sec(gc: str) -> Optional[int]:
    gc = str(gc or "")
    try:
        if "PT" in gc and "M" in gc:
            mm,ss = gc.split("PT")[1].split("M"); ss=ss.replace("S","").split(".")[0]
            return int(mm)*60+int(ss)
        if ":" in gc:
            mm,ss = gc.split(":"); return int(mm)*60+int(ss)
    except: pass
    return None

def elapsed_min(period: int, clock_sec: Optional[int]) -> Optional[float]:
    if clock_sec is None or period <= 0: return None
    if period <= 4: return ((period-1)*720 + (720-clock_sec))/60
    return (4*720 + (period-5)*300 + (300-min(clock_sec,300)))/60

def slug_matchup(slug: str) -> str:
    p = slug.replace("nba-","").split("-")
    return f"{p[0].upper()} @ {p[1].upper()}" if len(p) >= 2 else slug

def sig_id(player, market, line, side, kind) -> str:
    raw = f"{date.today().isoformat()}|{player.lower()}|{market}|{line}|{side}|{kind}"
    return hashlib.md5(raw.encode()).hexdigest()[:10].upper()

def pre_emoji(s: int) -> str:
    return "🔥" if s>=75 else ("⭐" if s>=60 else ("🟡" if s>=45 else ("🟠" if s>=30 else "🔻")))

def pre_bar(s: int, n=8) -> str:
    f = round(s/100*n); return "█"*f+"░"*(n-f)

def pre_label(s: int) -> str:
    return "FUERTE" if s>=75 else ("BUENA" if s>=60 else ("MEDIA" if s>=45 else ("DÉBIL" if s>=30 else "BAJA")))

def fmt_pre(s: int) -> str:
    return f"{pre_emoji(s)} `{s:>3}/100` {pre_bar(s)} _{pre_label(s)}_"

def stat_of(stats: dict, tipo: str) -> float:
    return float(stats.get({"puntos":"points","rebotes":"reboundsTotal","asistencias":"assists"}[tipo], 0) or 0)

async def send_msg(update: Update, text: str, max_len=3800):
    """Envía mensaje dividiéndolo si supera el límite."""
    if len(text) <= max_len:
        try: await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        except: await update.message.reply_text(text.replace("*","").replace("_","").replace("`",""))
        return
    # Partir en bloques
    parts, rem = [], text
    while len(rem) > max_len:
        cut = rem[:max_len].rfind("\n👤")
        if cut < 200: cut = rem[:max_len].rfind("\n")
        if cut < 0:   cut = max_len
        parts.append(rem[:cut]); rem = rem[cut:]
    if rem: parts.append(rem)
    for i, part in enumerate(parts):
        prefix = f"_(cont. {i+1}/{len(parts)})_\n" if i else ""
        try: await update.message.reply_text(prefix+part, parse_mode=ParseMode.MARKDOWN)
        except: await update.message.reply_text((prefix+part).replace("*","").replace("_","").replace("`",""))
        await asyncio.sleep(0.3)

# ═══════════════════════════════════════════════════════════════
# 7. NBA API — PLAYER IDS & GAMELOGS
# ═══════════════════════════════════════════════════════════════
_NAME_SFXS  = re.compile(r'\b(jr\.?|sr\.?|ii|iii|iv)\s*$', re.I)
_NAME_ALIAS = {
    "shai gilgeous alexander":"shai gilgeous-alexander","gg jackson":"gregory jackson ii",
    "gg jackson ii":"gregory jackson ii","tj mcconnell":"t.j. mcconnell",
    "cj mccollum":"c.j. mccollum","pj washington":"p.j. washington",
    "cam thomas":"cameron thomas","cam johnson":"cameron johnson",
    "mo bamba":"mohamed bamba","nic claxton":"nicolas claxton",
    "jaren jackson":"jaren jackson jr","wendell carter":"wendell carter jr",
}

def norm_name(n: str) -> str:
    n = ''.join(c for c in unicodedata.normalize('NFD',n) if unicodedata.category(c)!='Mn')
    n = _NAME_SFXS.sub('',n).strip()
    return re.sub(r'\s+',' ',re.sub(r'[^a-z0-9 ]','',n.lower())).strip()

def resolve_name(raw: str) -> str:
    n = norm_name(raw); return _NAME_ALIAS.get(n, n)

def _fuzzy_match(name: str) -> Optional[dict]:
    parts = norm_name(name).split()
    if not parts: return None
    last  = parts[-1]
    all_p = nba_players_static.get_players()
    cands = [p for p in all_p if last in norm_name(p.get("full_name","")).split()]
    if len(cands)==1: return cands[0]
    if len(parts)>=2:
        first=parts[0]
        for c in cands:
            if first in norm_name(c.get("full_name","")).split(): return c
    return None

def get_pid(name: str) -> Optional[int]:
    canonical = resolve_name(name)
    cache     = _json_load("player_ids.json", {})
    for k in (name, canonical):
        if k in cache: return int(cache[k])
    # NBA API lookup
    import random as _rand; time.sleep(0.25+_rand.random()*0.1)
    res   = nba_players_static.find_players_by_full_name(canonical)
    exact = [p for p in res if norm_name(p.get("full_name",""))==canonical]
    pick  = (exact or res or [None])[0]
    if not pick: pick = _fuzzy_match(canonical)
    if not pick:
        log.warning(f"PID no encontrado: '{name}' -> '{canonical}'"); return None
    pid = int(pick["id"])
    cache[name]=pid; cache[canonical]=pid
    _json_save("player_ids.json", cache)
    return pid

def get_gamelog(pid: int) -> Tuple[List[str], List[list]]:
    key = f"gl:{pid}"
    hit = CACHE.get(key, GLOG_TTL_SEC)
    if hit: return hit
    import random as _rand; time.sleep(0.5+_rand.random()*0.25)
    try:
        resp = NBA_SES.get("https://stats.nba.com/stats/playergamelog", timeout=(12,90), params={
            "DateFrom":"","DateTo":"","LeagueID":"00","PlayerID":str(pid),
            "Season":SEASON,"SeasonType":"Regular Season"})
        if resp.status_code!=200: return [],[]
        rs = resp.json().get("resultSets",[])
        if not rs: return [],[]
        h,r = rs[0].get("headers",[]), rs[0].get("rowSet",[])
        CACHE.set(key, (h,r)); return h,r
    except Exception as e:
        log.warning(f"Gamelog error pid={pid}: {e}"); return [],[]

def last_n(pid: int, tipo: str, n: int) -> List[float]:
    h, rows = get_gamelog(pid)
    if not h or not rows: return []
    try:
        i = h.index(STAT_COL[tipo])
        return [float(r[i]) for r in rows[:n] if i<len(r)]
    except: return []

def hits(vals: List[float], line: float, side: str) -> Tuple[int,int]:
    fn = (lambda v: v>line) if side=="over" else (lambda v: v<line)
    return sum(1 for v in vals if fn(v)), len(vals)

def trend(vals: List[float]) -> str:
    if len(vals)<6: return "→"
    d = sum(vals[:5])/5 - sum(vals[5:min(10,len(vals))])/min(5,len(vals[5:]))
    return "📈" if d>1.5 else ("📉" if d<-1.5 else "→")

def home_away_splits(pid: int, tipo: str) -> dict:
    h, rows = get_gamelog(pid)
    if not h or not rows: return {}
    try:
        si = h.index(STAT_COL[tipo]); mi = h.index("MATCHUP")
    except: return {}
    home_v, away_v = [], []
    for r in rows:
        try:
            v=float(r[si]); loc=str(r[mi])
            (home_v if " vs. " in loc else away_v).append(v)
        except: pass
    out={}
    if home_v: out["home_avg"]=round(sum(home_v)/len(home_v),1); out["home_n"]=len(home_v)
    if away_v: out["away_avg"]=round(sum(away_v)/len(away_v),1); out["away_n"]=len(away_v)
    return out

def matchup_hist(pid: int, opp: str, tipo: str) -> Optional[dict]:
    h, rows = get_gamelog(pid)
    if not h or not rows: return None
    try:
        si=h.index(STAT_COL[tipo]); mi=h.index("MATCHUP")
    except: return None
    vals = [float(r[si]) for r in rows if opp.upper() in str(r[mi]).upper()]
    if not vals: return None
    return {"games":len(vals),"avg":round(sum(vals)/len(vals),1),"max":max(vals),"min":min(vals)}

def is_b2b(pid: int) -> bool:
    h, rows = get_gamelog(pid)
    if not h or not rows: return False
    try:
        di=h.index("GAME_DATE")
        last=datetime.strptime(str(rows[0][di]),"%b %d, %Y").date()
        return last == date.today()-timedelta(days=1)
    except: return False

def get_team_id(tricode: str) -> Optional[int]:
    for t in nba_teams_static.get_teams():
        if t.get("abbreviation","").upper()==tricode.upper(): return int(t["id"])
    return None

# ═══════════════════════════════════════════════════════════════
# 8. PRE SCORE v2 (con contexto defensivo)
# ═══════════════════════════════════════════════════════════════
def _pre_base(pid: int, tipo: str, line: float, side: str) -> Tuple[int, dict]:
    v5=last_n(pid,tipo,5); v10=last_n(pid,tipo,10)
    if not v10: return 0,{}
    h5,n5=hits(v5,line,side); h10,n10=hits(v10,line,side)
    hit5=h5/n5 if n5 else 0; hit10=h10/n10 if n10 else 0
    avg10=sum(v10)/len(v10); avg5=sum(v5)/len(v5) if v5 else avg10
    m10=[(v-line if side=="over" else line-v) for v in v10]
    m5 =[(v-line if side=="over" else line-v) for v in v5]
    wm  = max(0, 0.65*(sum(m10)/len(m10) if m10 else 0) + 0.35*(sum(m5)/len(m5) if m5 else 0))
    cap = MRG_CAP.get(tipo,3.0); std=stdev(v10); scap=STD_CAP.get(tipo,4.0)
    HS  = 100*(0.65*hit10+0.35*hit5)
    MS  = clamp((wm/cap)*100)
    CS  = 100-clamp((std/scap)*60,0,60)
    pre = int(clamp(0.55*HS+0.25*MS+0.20*CS))
    return pre, {"hits5":h5,"n5":n5,"hits10":h10,"n10":n10,"avg5":round(avg5,1),"avg10":round(avg10,1),"std10":round(std,2)}

def _def_context(opp_tri: str, tipo: str) -> dict:
    """Obtiene ranking defensivo del rival (con cache)."""
    key = f"ctx:{opp_tri}:{tipo}"
    hit = CACHE.get(key, CONTEXT_TTL_SEC)
    if hit: return hit
    # Descarga leaguedashteamstats
    url = "https://stats.nba.com/stats/leaguedashteamstats"
    try:
        r = NBA_SES.get(url, timeout=(12,60), params={
            "MeasureType":"Advanced","PerMode":"PerGame","Season":SEASON,
            "SeasonType":"Regular Season","LeagueID":"00",
            **{k:"" for k in ("Conference","DateFrom","DateTo","Division","GameScope",
               "GameSegment","LastNGames","Location","Month","OpponentTeamID",
               "Outcome","PORound","PaceAdjust","Period","PlayerExperience",
               "PlayerPosition","PlusMinus","Rank","SeasonSegment","ShotClockRange",
               "StarterBench","TeamID","TwoWay","VsConference","VsDivision")},
            "LastNGames":"0","Month":"0","OpponentTeamID":"0","TeamID":"0",
            "PaceAdjust":"N","PlusMinus":"N","Rank":"N",
        })
        if r.status_code!=200: return {}
        rs=r.json().get("resultSets",[{}])[0]
        hdr=rs.get("headers",[]); rows=rs.get("rowSet",[])
        stats={int(dict(zip(hdr,row)).get("TEAM_ID",0)):dict(zip(hdr,row)) for row in rows}
        tid=get_team_id(opp_tri)
        if not tid or tid not in stats: return {}
        ts   = stats[tid]
        dr   = float(ts.get("DEF_RATING",0) or 0)
        pace = float(ts.get("PACE",0) or 0)
        all_dr = sorted(stats.values(), key=lambda x: float(x.get("DEF_RATING",0) or 0))
        dr_rank = next((i+1 for i,t in enumerate(all_dr) if t.get("TEAM_ID")==ts.get("TEAM_ID")), None)
        ctx = {"def_rating":dr,"pace":pace,"def_rank":dr_rank}
        CACHE.set(key, ctx); return ctx
    except Exception as e:
        log.warning(f"def_context error: {e}"); return {}

def pre_score(pid: int, tipo: str, line: float, side: str,
              opp_tri="", is_home=True, rest_days=1) -> Tuple[int,dict]:
    """PRE Score final con ajustes de contexto."""
    base, meta = _pre_base(pid, tipo, line, side)
    adj, adjs  = 0, []

    # Ajuste defensa rival
    ctx = _def_context(opp_tri, tipo) if opp_tri else {}
    dr  = ctx.get("def_rank")
    if dr:
        if side=="over":
            if dr>=25: adj+=8; adjs.append("def_débil+8")
            elif dr<=5: adj-=8; adjs.append("def_elite-8")
        else:
            if dr<=5: adj+=8; adjs.append("def_elite+8")
            elif dr>=25: adj-=8; adjs.append("def_débil-8")

    # Ajuste H/A
    splits=home_away_splits(pid,tipo)
    loc="home" if is_home else "away"
    la=splits.get(f"{loc}_avg")
    if la is not None:
        g=(la-line) if side=="over" else (line-la)
        if g>2: adj+=5; adjs.append(f"split_{loc}+5")
        elif g<-2: adj-=5; adjs.append(f"split_{loc}-5")

    # Ajuste descanso
    if rest_days==0: adj+=-6 if side=="over" else 6; adjs.append("b2b")
    elif rest_days>=3: adj+=4 if side=="over" else -4; adjs.append("descanso+4")

    final=int(clamp(base+adj)); meta.update({"v2_base":base,"v2_adj":adj,"adjs":adjs})
    return final, meta

# Pre-score cacheado en memoria por sesión
_PRE_CACHE: Dict[str,Tuple[int,int,dict]] = {}

def pre_cached(pid: int, tipo: str, line: float, opp="", is_home=True, rest=1):
    k=f"{pid}:{tipo}:{line}"
    if k not in _PRE_CACHE:
        po,m = pre_score(pid,tipo,line,"over",opp,is_home,rest)
        pu,_ = pre_score(pid,tipo,line,"under",opp,is_home,rest)
        _PRE_CACHE[k]=(po,pu,m)
    return _PRE_CACHE[k]

# ═══════════════════════════════════════════════════════════════
# 9. SCOREBOARD & BOXSCORE (con control de frescura)
# ═══════════════════════════════════════════════════════════════
async def get_live_snapshot() -> Optional[LiveSnapshot]:
    """Obtiene scoreboard fresco. Retorna None si falla."""
    key = "scoreboard"
    hit = CACHE.get(key, LIVE_STALE_SEC)
    if hit: return hit
    try:
        board = await asyncio.wait_for(
            asyncio.to_thread(lambda: scoreboard.ScoreBoard().get_dict()["scoreboard"]),
            timeout=20.0)
        snap = LiveSnapshot(ts=now(), games=board.get("games",[]))
        CACHE.set(key, snap); return snap
    except Exception as e:
        log.warning(f"Scoreboard error: {e}"); return None

async def get_boxscore(game_id: str) -> Optional[BoxSnapshot]:
    """Obtiene boxscore fresco. Retorna None si falla."""
    key = f"box:{game_id}"
    hit = CACHE.get(key, BOX_STALE_SEC)
    if hit: return hit
    try:
        data = await asyncio.wait_for(
            asyncio.to_thread(lambda gid=game_id: boxscore.BoxScore(gid).get_dict()["game"]),
            timeout=15.0)
        snap = BoxSnapshot(ts=now(), game_id=game_id, data=data)
        CACHE.set(key, snap); return snap
    except Exception as e:
        log.warning(f"Boxscore error {game_id}: {e}"); return None

def _stale_warning(snap: LiveSnapshot) -> str:
    """Genera advertencia si los datos son viejos."""
    age = now() - snap.ts
    if age > LIVE_STALE_SEC:
        return f"⚠️ _Datos actualizados hace {age}s (puede haber retraso)_\n"
    return ""

# ═══════════════════════════════════════════════════════════════
# 10. POLYMARKET
# ═══════════════════════════════════════════════════════════════
_PM_RE = re.compile(r"^(?P<player>.+?)(?::\s*|\s+)(?P<stat>Points|Rebounds|Assists)\s*O\/U\s*(?P<line>\d+(?:\.\d+)?)", re.I)
_TRI2SLUG = {
    "ATL":["atlanta","hawks"],"BOS":["boston","celtics"],"BKN":["brooklyn","nets"],
    "CHA":["charlotte","hornets"],"CHI":["chicago","bulls"],"CLE":["cleveland","cavaliers"],
    "DAL":["dallas","mavericks"],"DEN":["denver","nuggets"],"DET":["detroit","pistons"],
    "GSW":["golden-state","warriors"],"HOU":["houston","rockets"],"IND":["indiana","pacers"],
    "LAC":["la-clippers","clippers"],"LAL":["la-lakers","lakers"],"MEM":["memphis","grizzlies"],
    "MIA":["miami","heat"],"MIL":["milwaukee","bucks"],"MIN":["minnesota","timberwolves"],
    "NOP":["new-orleans","pelicans"],"NYK":["new-york","knicks"],"OKC":["oklahoma","thunder"],
    "ORL":["orlando","magic"],"PHI":["philadelphia","sixers"],"PHX":["phoenix","suns"],
    "POR":["portland","blazers"],"SAC":["sacramento","kings"],"SAS":["san-antonio","spurs"],
    "TOR":["toronto","raptors"],"UTA":["utah","jazz"],"WAS":["washington","wizards"],
}

def _pm_parse_market(m: dict) -> Tuple[Optional[str],Optional[str],Optional[float]]:
    """Extrae (player, stat, line) de un market de Polymarket."""
    smt = (m.get("sportsMarketType") or "").lower()
    q   = m.get("question") or m.get("title") or ""
    if not smt:
        ql = q.lower()
        smt = "points" if "point" in ql else ("rebounds" if "rebound" in ql else ("assists" if "assist" in ql else ""))
    if smt not in TIPO_MAP: return None,None,None
    line = m.get("line")
    try: line=float(line)
    except: line=None
    mm = _PM_RE.match(q)
    player = mm.group("player").strip() if mm else (m.get("groupItemTitle") or "")
    if not line and mm:
        try: line=float(mm.group("line"))
        except: pass
    return (player or None), smt, line

def _event_matches(slug: str, title: str, a_tri: str, h_tri: str) -> bool:
    combined = (slug+" "+title).lower()
    return (any(n in combined for n in _TRI2SLUG.get(a_tri,[])) and
            any(n in combined for n in _TRI2SLUG.get(h_tri,[])))

def _pm_fetch(url: str, params: dict) -> Optional[list]:
    try:
        r=PM_SES.get(url, params=params, timeout=20)
        if r.status_code!=200: return None
        d=r.json(); return d if isinstance(d,list) else d.get("events") or d.get("markets") or []
    except Exception as e:
        log.warning(f"PM fetch error {url}: {e}"); return None

def _props_from_event(ev: dict, slug: str) -> List[Prop]:
    markets = ev.get("markets",[]) or []
    if not markets and ev.get("id"):
        markets = _pm_fetch(f"{GAMMA_URL}/markets", {"event_id":str(ev["id"]),"limit":200}) or []
    out=[]
    for m in markets:
        player,smt,line = _pm_parse_market(m)
        if not player or not smt or line is None: continue
        tipo=TIPO_MAP[smt]; mid=str(m.get("id",""))
        for side in ("over","under"):
            out.append(Prop(player=player,tipo=tipo,line=line,side=side,
                           source="polymarket",game_slug=slug,market_id=mid))
    return out

def load_pm_props(snap: Optional[LiveSnapshot]=None) -> List[Prop]:
    """Carga props de Polymarket. Usa cache interno."""
    key="pm_props"; hit=CACHE.get(key, PM_TTL_SEC)
    if hit: return hit
    games = snap.games if snap else []
    pairs=[(  (g.get("awayTeam") or {}).get("teamTricode","").upper(),
               (g.get("homeTeam") or {}).get("teamTricode","").upper(),
               snap.game_slug(g) if snap else "") for g in games]
    props: List[Prop]=[]
    # Estrategia 1: slug exacto
    for a,h,local_slug in pairs:
        ev=_pm_fetch(f"{GAMMA_URL}/events/slug/{local_slug}",{})
        if ev and isinstance(ev,dict): props+=_props_from_event(ev,local_slug)
    # Estrategia 2: buscar por nombre
    if not props:
        evs=_pm_fetch(f"{GAMMA_URL}/events",{"tag_slug":"nba","closed":"false","limit":100}) or []
        for ev in evs:
            for a,h,local_slug in pairs:
                if _event_matches(ev.get("slug",""),ev.get("title",""),a,h):
                    props+=_props_from_event(ev,local_slug); break
    # Fallback hardcodeado
    if not props:
        log.warning("Sin props Polymarket — usando fallback"); props=list(_FALLBACK_PROPS)
    # Dedupe
    seen=set(); uniq=[]
    for p in props:
        k=(p.game_slug,p.player.lower(),p.tipo,p.side,p.line)
        if k not in seen: seen.add(k); uniq.append(p)
    CACHE.set(key,uniq)
    log.info(f"Props cargados: {len(uniq)}")
    return uniq

# Fallback mínimo (actualizar con las props del día)
_FALLBACK_PROPS: List[Prop] = [
Prop("Nikola Jokic","puntos",27.5,"over",source="fallback",game_slug=f"nba-den-placeholder-{date.today().isoformat()}"),
Prop("Nikola Jokic","puntos",27.5,"under",source="fallback",game_slug=f"nba-den-placeholder-{date.today().isoformat()}"),
Prop("Jaylen Brown","puntos",28.5,"over",source="fallback",game_slug=f"nba-bos-placeholder-{date.today().isoformat()}"),
Prop("Jaylen Brown","puntos",28.5,"under",source="fallback",game_slug=f"nba-bos-placeholder-{date.today().isoformat()}"),
]

# ═══════════════════════════════════════════════════════════════
# 11. SCORING LIVE
# ═══════════════════════════════════════════════════════════════
def live_over_score(tipo,delta,mins,pf,period,clk_sec,diff,is_clutch,is_blowout)->int:
    lo,hi = (0.5,4.0) if tipo=="puntos" else (0.5,1.5)
    mf    = 10.0 if tipo=="puntos" else 14.0
    if not (lo<=delta<=hi): return 0
    base  = 60*clamp((hi-delta)/(hi-0.5))+(10 if delta<=2 else 0)
    spot  = 12 if period>=4 else (7 if period==3 else 3)
    if clk_sec and period>=3: spot+=clamp((720-clk_sec)/720*9,0,9)
    if is_clutch: spot+=11
    fpen  = {5:18,4:10,3:4}.get(int(pf),0) if tipo!="rebotes" else {5:22,4:12}.get(int(pf),0)
    bpen  = (18 if diff>=25 else 12) if is_blowout else 0
    return int(clamp(base+spot+clamp((mins-mf)/18*12,0,12)-fpen-bpen))

def live_under_score(tipo,margin,mins,pf,period,clk_sec,diff,is_blowout)->int:
    min_m,good_m = (3.0,6.0) if tipo=="puntos" else (2.0,3.5)
    if margin<min_m: return 0
    el=elapsed_min(period,clk_sec) if clk_sec else None
    ts=clamp(((el or 0)-20)/28*28,0,28)
    cush=clamp((margin-min_m)/(good_m-min_m)*40,0,40)
    blow=(20+(6 if diff>=25 else 0)) if is_blowout else 0
    mb=12 if (el or 0)>=30 and mins<18 else (8 if (el or 0)>=30 and mins<24 else 0)
    fpen={5:6,4:3}.get(int(pf),0)
    return int(clamp(cush+ts+blow+mb-fpen))

def final_score(live: int, pre: int, side: str) -> int:
    w = (0.55,0.45) if side=="over" else (0.65,0.35)
    return int(clamp(w[0]*live+w[1]*pre))

# ═══════════════════════════════════════════════════════════════
# 12. SEÑALES PREGAME
# ═══════════════════════════════════════════════════════════════
def build_signal(pid:int, player:str, market:str, line:float, side:str,
                 game_slug:str, market_id:str="", opp:str="", is_home:bool=True) -> Optional[Signal]:
    v10=last_n(pid,market,10); v5=last_n(pid,market,5)
    if len(v10)<3: return None
    avg10=sum(v10)/len(v10); std10=stdev(v10); avg5=sum(v5)/len(v5) if v5 else avg10
    mp    = model_prob(avg10,std10,line,side)
    pre,m = pre_score(pid,market,line,side,opp,is_home)
    ip    = 0.25+(pre/100)*0.50 if side=="over" else 1-(0.25+(pre/100)*0.50)
    edge  = round((mp-ip)*100,2)
    conf  = pre
    reasons,risks=[],[]
    h5,n5=hits(v5,line,side); h10,n10=hits(v10,line,side)
    if n10 and h10/n10>=0.70: reasons.append("hit_rate_alto_10j")
    if n5  and h5/n5 >=0.80: reasons.append("racha_fuerte_5j")
    if (avg10-line if side=="over" else line-avg10)>2: reasons.append("promedio_supera_linea")
    t=trend(v10)
    if t=="📈": reasons.append("tendencia_alcista")
    elif t=="📉": risks.append("tendencia_bajista")
    if is_b2b(pid): risks.append("back_to_back"); conf-=8
    if opp:
        ctx=_def_context(opp,market)
        dr=ctx.get("def_rank")
        if dr:
            if side=="over" and dr>=25: reasons.append("rival_defensa_débil"); conf+=5
            elif side=="over" and dr<=5: risks.append("rival_defensa_elite"); conf-=5
    conf=int(clamp(conf))
    if edge>=EDGE_MIN_PRE and conf>=CONF_MIN: level="entry"
    elif edge>=EDGE_MIN_PRE*0.6 and conf>=CONF_MIN-10: level="watch"
    else: return None
    return Signal(signal_id=sig_id(player,market,line,side,"pregame"),ts=now(),kind="pregame",
                  player=norm_name(player),player_id=pid,market=market,line=line,side=side,
                  game_slug=game_slug,implied_prob=round(ip,3),model_prob=round(mp,3),
                  edge=edge,confidence=conf,reason_codes=reasons,risk_flags=risks,
                  level=level,market_id=market_id)

def fmt_signal(sig: Signal) -> str:
    level_str = {"entry":"🟢 ENTRY","watch":"🟡 WATCH","avoid":"🔴 AVOID"}.get(sig.level,"?")
    icon = TIPO_ICON.get(sig.market,sig.market.upper())
    side_str="Over" if sig.side=="over" else "Under"
    reasons="\n".join(f"  ✅ {r.replace('_',' ')}" for r in sig.reason_codes) or "  -"
    risks  ="\n".join(f"  ⚠️ {r.replace('_',' ')}" for r in sig.risk_flags)   or "  -"
    return (f"{level_str} | NBA Props\n{'─'*30}\n"
            f"👤 *{sig.player.title()}*\n"
            f"📌 {icon} {side_str} `{sig.line}` — _{slug_matchup(sig.game_slug)}_\n\n"
            f"📊 Impl: `{sig.implied_prob*100:.1f}%` · Modelo: `{sig.model_prob*100:.1f}%` · "
            f"Edge: `{sig.edge:+.1f}%` · Conf: `{sig.confidence}/100`\n\n"
            f"*Razones:*\n{reasons}\n*Riesgos:*\n{risks}\n{'─'*30}\n"
            f"`#{sig.signal_id}` · {sig.kind}")

# ═══════════════════════════════════════════════════════════════
# 13. ALERTAS DE ESTADO (live data)
# ═══════════════════════════════════════════════════════════════
def _no_live_msg(snap: Optional[LiveSnapshot]) -> str:
    """Mensaje claro cuando no hay partidos en vivo."""
    if snap is None:
        return "❌ No se pudo contactar la API de NBA. Intenta de nuevo en unos segundos."
    if not snap.live_games():
        pregame = snap.pregame_games()
        finished= snap.finished_games()
        lines   = ["⏸ *No hay partidos en vivo ahora.*\n"]
        if pregame:
            lines.append("🕐 *Próximos:*")
            for g in pregame:
                a=(g.get("awayTeam") or {}).get("teamTricode","?")
                h=(g.get("homeTeam") or {}).get("teamTricode","?")
                lines.append(f"  · {a} @ {h} — {g.get('gameStatusText','')}")
        if finished:
            lines.append("\n🏁 *Finalizados hoy:*")
            for g in finished[:4]:
                a=(g.get("awayTeam") or {})
                h=(g.get("homeTeam") or {})
                lines.append(f"  · {a.get('teamTricode','?')} {a.get('score','?')} — "
                             f"{h.get('teamTricode','?')} {h.get('score','?')}")
        lines.append("\n_Usa `/games` para ver la cartelera completa._")
        return "\n".join(lines)
    return ""   # hay juegos en vivo, no se necesita mensaje

_ALERT_STATE: dict = {}
def _can_alert(key: str) -> bool:
    n=now(); last=_ALERT_STATE.get(key,0)
    if n-last>=COOLDOWN_SEC: _ALERT_STATE[key]=n; return True
    return False

# ═══════════════════════════════════════════════════════════════
# 14. USUARIOS
# ═══════════════════════════════════════════════════════════════
USERS_F="users.json"
def _users()->dict: return _json_load(USERS_F,{"allowed":[],"admins":[],"nicknames":{}})
def _save_users(d): _json_save(USERS_F,d)
def _is_allowed(uid:int)->bool:
    u=_users(); return not u["allowed"] or uid in u["allowed"] or uid in u["admins"] or uid==ADMIN_ID
def _is_admin(uid:int)->bool: u=_users(); return uid in u["admins"] or uid==ADMIN_ID
def _add_user(uid:int,nick="",admin=False):
    u=_users()
    if uid not in u["allowed"]: u["allowed"].append(uid)
    if admin and uid not in u["admins"]: u["admins"].append(uid)
    if nick: u["nicknames"][str(uid)]=nick
    _save_users(u)
def _remove_user(uid:int):
    u=_users()
    for k in ("allowed","admins"): 
        if uid in u[k]: u[k].remove(uid)
    u["nicknames"].pop(str(uid),None); _save_users(u)

async def guard(update:Update)->bool:
    uid=update.effective_user.id if update.effective_user else 0
    if _is_allowed(uid): return True
    await update.message.reply_text(f"🔒 Sin acceso. Tu ID: `{uid}`\n"
        f"Pide al admin: `/adduser {uid} NombreAlias`",parse_mode=ParseMode.MARKDOWN)
    return False

def guarded(fn):
    async def wrapper(u,c):
        if await guard(u): await fn(u,c)
    wrapper.__name__=fn.__name__; return wrapper

# ═══════════════════════════════════════════════════════════════
# 15. APUESTAS
# ═══════════════════════════════════════════════════════════════
BETS_F="bets.json"
def _load_bets()->List[Bet]: return [Bet(**b) for b in _json_load(BETS_F,{"bets":[]}).get("bets",[])]
def _save_bets(bs): _json_save(BETS_F,{"bets":[asdict(b) for b in bs]})

def _parse_bet(text:str)->Optional[dict]:
    body=re.sub(r"^/bet(@\w+)?\s*","",text).strip()
    parts=[x.strip() for x in body.split("|")]
    if len(parts)<4: return None
    try: line=float(parts[3]); amt=float(parts[4]) if len(parts)>=5 else 1.0
    except: return None
    tipo=parts[1].lower(); side=parts[2].lower()
    if tipo not in STAT_COL or side not in ("over","under"): return None
    return {"player":parts[0],"tipo":tipo,"side":side,"line":line,"amount":amt}

# ═══════════════════════════════════════════════════════════════
# 16. HANDLERS — COMANDOS
# ═══════════════════════════════════════════════════════════════
HELP_TEXT=(
    "🧠 *NBA Props Bot*\n\n"
    "*Programación:* /games /lineup\n"
    "*Props:* /odds · /alertas · /signals\n"
    "*Análisis:* /analisis · /contexto\n"
    "*En vivo:* /live\n"
    "*Dashboard:* /dashboard · /status\n"
    "*Apuestas:* /bet · /misapuestas · /historial · /resultado\n"
    "*Admin:* /adduser · /removeuser · /usuarios\n"
)

async def cmd_start(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id if update.effective_user else 0
    uname=(update.effective_user.first_name or str(uid))
    u=_users()
    if uid==ADMIN_ID or (not u["allowed"] and not u["admins"]): _add_user(uid,uname,admin=True)
    elif not _is_allowed(uid):
        await update.message.reply_text(f"🔒 Acceso restringido. Tu ID: `{uid}`",parse_mode=ParseMode.MARKDOWN); return
    else: _add_user(uid,uname)
    cid=update.effective_chat.id
    for name,fn,intv in [("scan",bg_live_scan,POLL_SEC),("smart",bg_pregame_alerts,1800),
                          ("morning",bg_morning_check,3600),("autores",bg_autoresolve,1200)]:
        jname=f"{name}:{cid}"
        if not context.job_queue.get_jobs_by_name(jname):
            context.job_queue.run_repeating(fn,interval=intv,first=10,chat_id=cid,name=jname)
    await update.message.reply_text(f"✅ ¡Bienvenido *{uname}*! Jobs activados.\n\n"+HELP_TEXT,parse_mode=ParseMode.MARKDOWN)

@guarded
async def cmd_help(u,c): await u.message.reply_text(HELP_TEXT,parse_mode=ParseMode.MARKDOWN)

@guarded
async def cmd_games(update:Update, context:ContextTypes.DEFAULT_TYPE):
    snap=await get_live_snapshot()
    if not snap or not snap.games:
        await update.message.reply_text("❌ No se pudo obtener el scoreboard."); return
    lines=[f"📅 *NBA hoy* ({date.today().strftime('%d/%m/%Y')})\n{_stale_warning(snap)}"]
    status_icon={1:"🕐",2:"🔴",3:"🏁"}
    for g in snap.games:
        a=(g.get("awayTeam") or {}); h=(g.get("homeTeam") or {})
        at=a.get("teamTricode","?"); ht=h.get("teamTricode","?")
        sc=f"  `{a.get('score','?')} – {h.get('score','?')}`" if g.get("gameStatus")!=1 else ""
        icon=status_icon.get(g.get("gameStatus",1),"·")
        lines.append(f"{icon} *{at}* ({a.get('wins',0)}-{a.get('losses',0)}) @ *{ht}* ({h.get('wins',0)}-{h.get('losses',0)}){sc} — _{g.get('gameStatusText','')}_")
    await update.message.reply_text("\n".join(lines),parse_mode=ParseMode.MARKDOWN)

@guarded
async def cmd_live(update:Update, context:ContextTypes.DEFAULT_TYPE):
    """Muestra props en vivo, con manejo explícito de 'no hay juegos'."""
    w=await update.message.reply_text("🔄 Cargando datos en vivo...",parse_mode=ParseMode.MARKDOWN)
    snap=await get_live_snapshot()
    # ── Caso: no hay datos o no hay juegos en vivo ──
    no_live=_no_live_msg(snap)
    if no_live:
        await w.edit_text(no_live,parse_mode=ParseMode.MARKDOWN); return
    # ── Hay juegos en vivo ──
    live=snap.live_games()
    stale_warn=_stale_warning(snap)
    props_pm=load_pm_props(snap)
    props_all=_json_load("props.json",{"props":[]}).get("props",[])
    props_all=[Prop(**p) for p in props_all]+props_pm
    # índice nombre -> props
    by_name:Dict[str,List[Prop]]={}
    for p in props_all: by_name.setdefault(p.player.lower(),[]).append(p)
    rows=[]
    for g in live:
        gid=g.get("gameId","")
        if not gid: continue
        box=await get_boxscore(gid)
        if not box: continue
        period=int(g.get("period",0) or 0)
        clk=g.get("gameClock","") or ""
        clk_s=clock_to_sec(clk)
        ht=(g.get("homeTeam") or {}); at=(g.get("awayTeam") or {})
        diff=abs(int(ht.get("score",0))-int(at.get("score",0)))
        is_clutch=diff<=8; is_blow=diff>=BLOWOUT_IS
        for pl in box.players():
            fname=f"{pl.get('firstName','')} {pl.get('familyName','')}".strip().lower()
            pprops=by_name.get(fname,[])
            if not pprops: continue
            s=pl.get("statistics",{})
            mins=parse_minutes(s.get("minutes",""))
            pf=float(s.get("foulsPersonal",0) or 0)
            for pr in pprops:
                actual=stat_of(s,pr.tipo)
                po,pu,meta=pre_cached(pl.get("personId",0),pr.tipo,pr.line)
                pre=(po if pr.side=="over" else pu)
                if pr.side=="over":
                    delta=pr.line-actual
                    lsc=live_over_score(pr.tipo,delta,mins,pf,period,clk_s,diff,is_clutch,is_blow)
                else:
                    margin=pr.line-actual
                    lsc=live_under_score(pr.tipo,margin,mins,pf,period,clk_s,diff,is_blow)
                sc=final_score(lsc,pre,pr.side)
                if sc>0: rows.append((sc,lsc,pre,pr,actual,s,period,clk,mins,pf,diff,meta,g))
    await w.delete()
    if not rows:
        await update.message.reply_text(
            f"{stale_warn}📭 *Sin señal en vivo* — ninguna prop cerca de su línea ahora.\n"
            "_Usa /odds para cargar el caché de props._",parse_mode=ParseMode.MARKDOWN); return
    rows.sort(key=lambda x:-x[0])
    out=[f"🔥 *LIVE — {len(live)} partido(s)*\n{stale_warn}{'─'*28}"]
    for sc,lsc,pre,pr,actual,s,period,clk,mins,pf,diff,meta,g in rows[:12]:
        side_tag="OVER" if pr.side=="over" else "UNDER"
        delta=pr.line-actual; extra=f"faltan `{delta:.1f}`" if pr.side=="over" else f"margen `{delta:.1f}`"
        out.append(
            f"\n{pre_emoji(sc)} `{sc}/100` — *{pr.player}*\n"
            f"{TIPO_ICON.get(pr.tipo,'·')} {pr.tipo.upper()} {side_tag} `{pr.line}` | actual `{actual:.0f}` ({extra})\n"
            f"Q{period} {clk} · MIN`{mins:.0f}` PF`{pf:.0f}` Dif`{diff}`")
    await send_msg(update,"\n".join(out))

@guarded
async def cmd_odds(update:Update, context:ContextTypes.DEFAULT_TYPE):
	w=await update.message.reply_text("🔍 Cargando props...",parse_mode=ParseMode.MARKDOWN)
	# 1. Snapshot
	snap=await get_live_snapshot()
	# 2. Props con diagnóstico explícito
	try:
		props=await asyncio.wait_for(asyncio.to_thread(load_pm_props,snap),timeout=30.0)
	except asyncio.TimeoutError:
		await w.edit_text("⏱ Timeout cargando props de Polymarket. Intenta de nuevo."); return
	except Exception as e:
		await w.edit_text(f"❌ Error cargando props: {e}"); return
	args=" ".join(context.args or "").strip().lower()
	if args.startswith("nba-"): props=[p for p in props if (p.game_slug or "")==args]
	elif args: props=[p for p in props if args in p.player.lower()]
	# Solo OVER, dedupe
	seen=set(); candidates=[]
	for p in props:
		if p.side!="over": continue
		k=(p.game_slug,p.player,p.tipo,p.line)
		if k not in seen: seen.add(k); candidates.append(p)
	if not candidates:
		src="fallback hardcodeado" if props and all(p.source=="fallback" for p in props) else "Polymarket"
		await w.edit_text(
			f"😔 *Sin props disponibles* ({len(props)} props de {src}).
"
			f"_No hay partidos NBA hoy o Polymarket sin markets activos._",
			parse_mode=ParseMode.MARKDOWN); return
	await w.edit_text(f"⚙️ Calculando {len(candidates)} props...")
	sem=asyncio.Semaphore(4)
	errors=[]
	async def _calc(p:Prop):
		async with sem:
			def _inner():
				pid=get_pid(p.player)
				if not pid: return None
				slug_parts=(p.game_slug or "").replace("nba-","").split("-")
				opp=slug_parts[1].upper() if len(slug_parts)>=2 else ""
				po,pu,meta=pre_cached(pid,p.tipo,p.line,opp)
				return {"p":p,"po":po,"pu":pu,"meta":meta}
			try: return await asyncio.wait_for(asyncio.to_thread(_inner),timeout=30)
			except asyncio.TimeoutError:
				errors.append(f"timeout:{p.player}"); return None
			except Exception as e:
				errors.append(f"{p.player}:{e}"); return None
	results=[r for r in await asyncio.gather(*[_calc(p) for p in candidates]) if r]
	if not results:
		err_preview=", ".join(errors[:5]) if errors else "sin detalles"
		await w.edit_text(
			f"❌ *No se pudo calcular ninguna prop.*
"
			f"Errores ({len(errors)}): `{err_preview}`
"
			f"_Revisa los logs del bot._",
			parse_mode=ParseMode.MARKDOWN); return
    # Agrupar por slug
    by_slug:Dict[str,List[dict]]={}
    for r in results: by_slug.setdefault(r["p"].game_slug,[]).append(r)
    await w.delete()
    today_str=date.today().strftime("%d/%m/%Y")
    await update.message.reply_text(f"📋 *NBA Props — {today_str}*\n🔥≥75 ⭐≥60 🟡≥45",parse_mode=ParseMode.MARKDOWN)
    for slug,items in sorted(by_slug.items()):
        matchup=slug_matchup(slug)
        lines=[f"🟣 *{matchup}*\n{'─'*24}"]
        items.sort(key=lambda x:-max(x["po"],x["pu"]))
        for r in items:
            p=r["p"]; po=r["po"]; pu=r["pu"]; m=r["meta"]
            icon=TIPO_ICON.get(p.tipo,"·")
            avg=m.get("avg10"); avg_s=f"prom`{avg:.1f}`" if avg else ""
            lines.append(
                f"\n{icon} *{p.player}* — {p.tipo.upper()} `{p.line}`\n"
                f"  OVER  {fmt_pre(po)}\n"
                f"  UNDER {fmt_pre(pu)}\n"
                f"  📊`{m.get('hits5','?')}/{m.get('n5','?')}` ult5 · `{m.get('hits10','?')}/{m.get('n10','?')}` ult10 {avg_s}")
        await send_msg(update,"\n".join(lines))

@guarded
async def cmd_signals(update:Update, context:ContextTypes.DEFAULT_TYPE):
    w=await update.message.reply_text("🔍 Buscando señales pregame...",parse_mode=ParseMode.MARKDOWN)
    snap=await get_live_snapshot()
    props=load_pm_props(snap)
    seen=set(); candidates=[]
    for p in props:
        if p.side!="over": continue
        k=(p.player,p.tipo,p.line)
        if k not in seen: seen.add(k); candidates.append(p)
    sem=asyncio.Semaphore(4)
    async def _eval(p:Prop):
        async with sem:
            def _inner():
                pid=get_pid(p.player)
                if not pid: return None
                parts=(p.game_slug or "").replace("nba-","").split("-")
                opp=parts[1].upper() if len(parts)>=2 else ""
                return build_signal(pid,p.player,p.tipo,p.line,"over",p.game_slug,p.market_id,opp)
            try: return await asyncio.wait_for(asyncio.to_thread(_inner),timeout=25)
            except: return None
    sigs=[s for s in await asyncio.gather(*[_eval(p) for p in candidates[:50]]) if s]
    sigs.sort(key=lambda s:-s.edge)
    for s in sigs: db_save_signal(s)
    await w.delete()
    if not sigs:
        await update.message.reply_text(f"😔 Sin señales con edge ≥{EDGE_MIN_PRE}% hoy.",parse_mode=ParseMode.MARKDOWN); return
    e_cnt=sum(1 for s in sigs if s.level=="entry"); w_cnt=len(sigs)-e_cnt
    await update.message.reply_text(f"📈 *{len(sigs)} señales* — {e_cnt} ENTRY · {w_cnt} WATCH",parse_mode=ParseMode.MARKDOWN)
    for sig in sigs[:10]:
        await update.message.reply_text(fmt_signal(sig),parse_mode=ParseMode.MARKDOWN)
        await asyncio.sleep(0.4)

@guarded
async def cmd_dashboard(update:Update, context:ContextTypes.DEFAULT_TYPE):
    args=context.args or []; days=int(args[0]) if args and args[0].isdigit() else 30
    def _calc():
        sigs=db_get(days); res=[s for s in sigs if s.get("result") in ("win","loss","push")]
        wins=sum(1 for s in res if s["result"]=="win"); loss=sum(1 for s in res if s["result"]=="loss")
        tot=wins+loss; wr=round(wins/tot*100,1) if tot else 0
        roi=round((wins-loss)/tot*100,1) if tot else 0
        avg_edge=round(sum(s.get("edge",0) for s in sigs)/len(sigs),2) if sigs else 0
        by_mkt={}
        for m in ("puntos","rebotes","asistencias"):
            sub=[s for s in res if s["market"]==m]
            w=sum(1 for s in sub if s["result"]=="win"); l=len(sub)-w
            by_mkt[m]=(w,l,round((w-l)/len(sub)*100,1) if sub else 0)
        risk=_daily_risk()
        return {"total":len(sigs),"resolved":len(res),"pending":len(sigs)-len(res),
                "wins":wins,"losses":loss,"wr":wr,"roi":roi,"avg_edge":avg_edge,"by_mkt":by_mkt,"risk":risk}
    d=await asyncio.to_thread(_calc)
    roi_e="🟢" if d["roi"]>5 else ("🔴" if d["roi"]<-5 else "🟡")
    wr_e ="🟢" if d["wr"]>=55 else ("🔴" if d["wr"]<45 else "🟡")
    mkt_lines=[]
    for m,(w,l,roi) in d["by_mkt"].items():
        if w+l==0: continue
        mkt_lines.append(f"  {TIPO_ICON.get(m,'·')} {m}: `{w}W/{l}L` ROI `{roi:+.1f}%`")
    risk=d["risk"]
    msg=(f"📊 *DASHBOARD — {days} días* _{date.today().strftime('%d/%m/%Y')}_\n{'─'*30}\n"
         f"📝 `{d['total']}` señales (`{d['resolved']}` resueltas · `{d['pending']}` pend)\n"
         f"{wr_e} Win Rate: `{d['wr']}%`\n{roi_e} ROI: `{d['roi']:+.1f}%`\n"
         f"⚡ Edge prom: `{d['avg_edge']:+.1f}%`\n\n"
         f"*Por mercado:*\n"+"\n".join(mkt_lines)+
         f"\n\n🛡 Riesgo hoy: `{risk['signals_sent']}/{MAX_SIG_DAY}`")
    await update.message.reply_text(msg,parse_mode=ParseMode.MARKDOWN)

@guarded
async def cmd_alertas(update:Update, context:ContextTypes.DEFAULT_TYPE):
    w=await update.message.reply_text("🔍 Buscando mejores props...",parse_mode=ParseMode.MARKDOWN)
    snap=await get_live_snapshot(); props=load_pm_props(snap)
    sem=asyncio.Semaphore(4)
    async def _score(p:Prop):
        async with sem:
            def _inner():
                pid=get_pid(p.player);
                if not pid: return None
                po,_,meta=pre_cached(pid,p.tipo,p.line)
                return (po,p,meta)
            try: return await asyncio.wait_for(asyncio.to_thread(_inner),timeout=20)
            except: return None
    results=[r for r in await asyncio.gather(*[_score(p) for p in props if p.side=="over"]) if r and r[0]>=55]
    results.sort(key=lambda x:-x[0])
    await w.delete()
    if not results:
        await update.message.reply_text("😔 Sin props con PRE≥55 hoy."); return
    lines=[f"🏆 *MEJORES PROPS — {date.today().strftime('%d/%m/%Y')}*\n"]
    for i,(pre,p,m) in enumerate(results[:15],1):
        lines.append(f"*{i}.* {fmt_pre(pre)} — *{p.player}*\n"
                     f"   {TIPO_ICON.get(p.tipo,'·')} {p.tipo.upper()} OVER `{p.line}` _{slug_matchup(p.game_slug)}_\n"
                     f"   prom`{m.get('avg10','?')}` · `{m.get('hits5','?')}/{m.get('n5','?')}` ult5")
    await send_msg(update,"\n".join(lines))

@guarded
async def cmd_analisis(update:Update, context:ContextTypes.DEFAULT_TYPE):
    body=re.sub(r"^/analisis(@\w+)?\s*","",update.message.text or "").strip()
    parts=[x.strip() for x in body.split("|")]
    if len(parts)!=4:
        await update.message.reply_text("Uso: `/analisis Jugador | tipo | side | linea`",parse_mode=ParseMode.MARKDOWN); return
    player,tipo,side,line_s=parts
    tipo=tipo.lower(); side=side.lower()
    if tipo not in STAT_COL or side not in ("over","under"):
        await update.message.reply_text("tipo=puntos/rebotes/asistencias · side=over/under"); return
    try: line=float(line_s)
    except: await update.message.reply_text("Línea inválida"); return
    w=await update.message.reply_text(f"🔬 Analizando *{player}*...",parse_mode=ParseMode.MARKDOWN)
    def _run():
        pid=get_pid(player)
        if not pid: return None
        v10=last_n(pid,tipo,10); v5=last_n(pid,tipo,5)
        avg10=round(sum(v10)/len(v10),1) if v10 else 0
        avg5 =round(sum(v5)/len(v5),1)  if v5 else 0
        h5,n5=hits(v5,line,side); h10,n10=hits(v10,line,side)
        t=trend(v10)
        sp=home_away_splits(pid,tipo)
        opp=""; mu=None
        for p in (CACHE.get("pm_props",PM_TTL_SEC) or []):
            if p.player.lower()==player.lower() and p.game_slug:
                prt=(p.game_slug or "").replace("nba-","").split("-")
                if len(prt)>=2: opp=prt[1].upper()
                break
        if opp: mu=matchup_hist(pid,opp,tipo)
        b2b=is_b2b(pid)
        po,pu,meta=pre_cached(pid,tipo,line,opp)
        pre=(po if side=="over" else pu)
        return {"pid":pid,"v5":v5,"avg5":avg5,"avg10":avg10,"h5":h5,"n5":n5,"h10":h10,"n10":n10,
                "trend":t,"splits":sp,"mu":mu,"b2b":b2b,"pre":pre,"opp":opp}
    d=await asyncio.to_thread(_run)
    if not d: await w.edit_text(f"❌ Jugador no encontrado: {player}",parse_mode=ParseMode.MARKDOWN); return
    vals_str="  ".join(f"`{v:.0f}`" for v in (d["v5"] or [])[:5])
    sp=d["splits"]; sp_line=""
    la=sp.get("home_avg") if sp else None
    if la: sp_line=f"\n🏠 Split local: `{la}` | visitante: `{sp.get('away_avg','-')}`"
    mu=d["mu"]; mu_line=f"\n⚔️ vs {d['opp']}: `{mu['avg']}` en {mu['games']}G" if mu else ""
    b2b_line="\n⚠️ *Back-to-back*" if d["b2b"] else ""
    msg=(f"🔬 *ANÁLISIS — {player}*\n"
         f"📌 {tipo.upper()} {side.upper()} `{line}`\n"
         f"{fmt_pre(d['pre'])}\n{'─'*28}\n"
         f"📊 prom ult5:`{d['avg5']}` · ult10:`{d['avg10']}` {d['trend']}\n"
         f"🎯 ult5:`{d['h5']}/{d['n5']}` · ult10:`{d['h10']}/{d['n10']}`\n"
         f"🕐 {vals_str}{sp_line}{mu_line}{b2b_line}")
    await w.edit_text(msg,parse_mode=ParseMode.MARKDOWN)

@guarded
async def cmd_contexto(update:Update, context:ContextTypes.DEFAULT_TYPE):
    args=context.args or []
    if len(args)<2:
        await update.message.reply_text("Uso: `/contexto AWAY HOME`  Ej: `/contexto BOS DEN`",parse_mode=ParseMode.MARKDOWN); return
    away,home=args[0].upper(),args[1].upper()
    w=await update.message.reply_text(f"🛡 Cargando contexto {away}@{home}...",parse_mode=ParseMode.MARKDOWN)
    def _fetch():
        out={}
        for tri,label in [(home,f"Defensa del rival ({home})  ←  {away}"),(away,f"Defensa del rival ({away})  ←  {home}")]:
            ctx_lines=[f"*{label}:*"]
            for tipo in ("puntos","rebotes","asistencias"):
                c=_def_context(tri,tipo)
                dr=c.get("def_rating","?"); dr_r=c.get("def_rank","?"); pace=c.get("pace","?")
                icon="🟢" if isinstance(dr_r,int) and dr_r>=20 else ("🔴" if isinstance(dr_r,int) and dr_r<=8 else "🟡")
                ctx_lines.append(f"  {icon} {tipo.upper()}: DefRtg`{dr}` #{dr_r} · Pace`{pace}`")
            out[tri]="\n".join(ctx_lines)
        return out
    data=await asyncio.to_thread(_fetch)
    msg=f"🛡 *CONTEXTO — {away} @ {home}*\n{'─'*30}\n\n"+"\n\n".join(data.values())
    await w.edit_text(msg,parse_mode=ParseMode.MARKDOWN)

@guarded
async def cmd_status(update:Update, context:ContextTypes.DEFAULT_TYPE):
    def _check():
        checks={}
        try:
            conn=db(); n=conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]; conn.close()
            checks["db"]=f"✅ SQLite OK ({n} señales)"
        except Exception as e: checks["db"]=f"❌ DB: {e}"
        pm=CACHE.get("pm_props",PM_TTL_SEC) or []; checks["pm"]=f"{'✅' if pm else '⚠️'} {len(pm)} props cacheados"
        risk=_daily_risk(); checks["riesgo"]=f"{'✅' if risk['signals_sent']<MAX_SIG_DAY else '🔴'} {risk['signals_sent']}/{MAX_SIG_DAY} señales hoy"
        return checks
    data=await asyncio.to_thread(_check)
    lines=[f"🔧 *STATUS — {date.today().strftime('%d/%m/%Y')}*\n"]
    lines+=[f"*{k}:* {v}" for k,v in data.items()]
    lines.append(f"\n_Polling cada {POLL_SEC}s_")
    await update.message.reply_text("\n".join(lines),parse_mode=ParseMode.MARKDOWN)

# -- Apuestas --
@guarded
async def cmd_bet(update:Update, context:ContextTypes.DEFAULT_TYPE):
    p=_parse_bet(update.message.text or "")
    if not p:
        await update.message.reply_text("Uso: `/bet Jugador | tipo | side | linea | monto`",parse_mode=ParseMode.MARKDOWN); return
    w=await update.message.reply_text("💾 Registrando...",parse_mode=ParseMode.MARKDOWN)
    def _inner():
        pid=get_pid(p["player"])
        if not pid: return None,0,""
        po,pu,_=pre_cached(pid,p["tipo"],p["line"])
        pre=po if p["side"]=="over" else pu
        slug=next((pr.game_slug for pr in (CACHE.get("pm_props",PM_TTL_SEC) or []) if pr.player.lower()==p["player"].lower()),"")
        return pid,pre,slug
    import uuid as _uuid
    pid,pre,slug=await asyncio.to_thread(_inner)
    if not pid: await w.edit_text(f"❌ Jugador no encontrado: {p['player']}",parse_mode=ParseMode.MARKDOWN); return
    uid=update.effective_user.id if update.effective_user else 0
    bet=Bet(id=_uuid.uuid4().hex[:8].upper(),user_id=uid,player=p["player"],tipo=p["tipo"],
            side=p["side"],line=p["line"],amount=p["amount"],pre_score=pre,game_slug=slug,placed_at=now())
    bets=_load_bets(); bets.append(bet); _save_bets(bets)
    await w.edit_text(f"✅ *Apuesta `#{bet.id}`*\n👤 {bet.player} · {bet.tipo.upper()} {bet.side.upper()} `{bet.line}`\n"
                      f"💰 `{bet.amount}` u · {fmt_pre(pre)}\n_/resultado {bet.id} WIN|LOSS stat_",parse_mode=ParseMode.MARKDOWN)

@guarded
async def cmd_resultado(update:Update, context:ContextTypes.DEFAULT_TYPE):
    args=context.args or []
    if len(args)<2: await update.message.reply_text("Uso: `/resultado ID WIN|LOSS|PUSH stat`",parse_mode=ParseMode.MARKDOWN); return
    bid,res=args[0].upper(),args[1].upper()
    actual=float(args[2]) if len(args)>=3 else 0.0
    if res not in ("WIN","LOSS","PUSH"): await update.message.reply_text("Resultado: WIN, LOSS o PUSH"); return
    bets=_load_bets(); found=None
    for b in bets:
        if b.id==bid: b.result=res.lower(); b.actual_stat=actual; b.resolved_at=now(); found=b; break
    if not found: await update.message.reply_text(f"❌ No encontré `{bid}`",parse_mode=ParseMode.MARKDOWN); return
    _save_bets(bets)
    db_resolve(sig_id(found.player,found.tipo,found.line,found.side,"pregame"),res.lower(),actual)
    emoji={"WIN":"✅","LOSS":"❌","PUSH":"🔁"}[res]
    await update.message.reply_text(f"{emoji} `#{bid}` → *{res}* | real: `{actual}`",parse_mode=ParseMode.MARKDOWN)

@guarded
async def cmd_historial(update:Update, context:ContextTypes.DEFAULT_TYPE):
    args=context.args or []; days=int(args[0]) if args and args[0].isdigit() else 30
    uid=update.effective_user.id if update.effective_user else 0
    cutoff=now()-days*86400
    mine=[b for b in _load_bets() if b.user_id==uid and b.placed_at>=cutoff]
    res=[b for b in mine if b.result in ("win","loss")]; pend=[b for b in mine if not b.result]
    w=sum(1 for b in res if b.result=="win"); l=len(res)-w
    net=sum(b.amount for b in res if b.result=="win")-sum(b.amount for b in res if b.result=="loss")
    wr=round(w/len(res)*100,1) if res else 0; roi=round(net/sum(b.amount for b in res)*100,1) if res else 0
    roi_e="🟢" if roi>0 else "🔴"
    msg=(f"📊 *Historial — {days} días*\n{'─'*28}\n"
         f"📝 {len(mine)} apuestas · {len(res)} resueltas · {len(pend)} pend\n"
         f"🎯 WR: `{wr}%` · {roi_e} ROI: `{roi:+.1f}%` · Neto: `{net:+.1f}` u\n")
    for m in ("puntos","rebotes","asistencias"):
        sub=[b for b in res if b.tipo==m]; sw=sum(1 for b in sub if b.result=="win")
        if sub: msg+=f"{TIPO_ICON.get(m,'·')} {m}: `{sw}W/{len(sub)-sw}L`\n"
    if pend:
        msg+="\n*⏳ Pendientes:*\n"
        for b in pend[-5:]: msg+=f"  `#{b.id}` {b.player} {b.tipo.upper()} {b.side.upper()} `{b.line}`\n"
    await update.message.reply_text(msg,parse_mode=ParseMode.MARKDOWN)

@guarded
async def cmd_misapuestas(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id if update.effective_user else 0
    pend=[b for b in _load_bets() if b.user_id==uid and not b.result]
    if not pend: await update.message.reply_text("📭 Sin apuestas pendientes."); return
    lines=[f"⏳ *Pendientes ({len(pend)})*\n"]
    for b in pend:
        lines.append(f"`#{b.id}` {TIPO_ICON.get(b.tipo,'·')} *{b.player}*\n"
                     f"  {b.tipo.upper()} {b.side.upper()} `{b.line}` · `{b.amount}`u · {fmt_pre(b.pre_score)}")
    await send_msg(update,"\n\n".join(lines))

# -- Admin --
async def cmd_adduser(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id if update.effective_user else 0
    u=_users(); no_admins=not u["admins"] and (ADMIN_ID==0)
    if not _is_admin(uid) and not no_admins:
        await update.message.reply_text(f"❌ Solo admins. Tu ID: `{uid}`",parse_mode=ParseMode.MARKDOWN); return
    if no_admins: _add_user(uid,"",admin=True)
    args=context.args or []
    if not args: await update.message.reply_text("Uso: `/adduser ID Nombre`",parse_mode=ParseMode.MARKDOWN); return
    try: tid=int(args[0])
    except: await update.message.reply_text("ID debe ser número"); return
    nick=" ".join(args[1:]) if len(args)>1 else ""; _add_user(tid,nick)
    await update.message.reply_text(f"✅ `{tid}` (*{nick or '-'}*) añadido.",parse_mode=ParseMode.MARKDOWN)

async def cmd_removeuser(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id if update.effective_user else 0
    if not _is_admin(uid): await update.message.reply_text("❌ Solo admins"); return
    args=context.args or []
    if not args: await update.message.reply_text("Uso: `/removeuser ID`",parse_mode=ParseMode.MARKDOWN); return
    try: tid=int(args[0])
    except: await update.message.reply_text("ID debe ser número"); return
    _remove_user(tid); await update.message.reply_text(f"🗑 `{tid}` eliminado.",parse_mode=ParseMode.MARKDOWN)

async def cmd_usuarios(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id if update.effective_user else 0
    if not _is_admin(uid): await update.message.reply_text("❌ Solo admins"); return
    u=_users(); lines=["👥 *Usuarios:*\n"]
    for xid in u.get("allowed",[]):
        nick=u["nicknames"].get(str(xid),"-"); adm="👑 " if xid in u.get("admins",[]) else "· "
        lines.append(f"{adm}`{xid}` — {nick}")
    await update.message.reply_text("\n".join(lines),parse_mode=ParseMode.MARKDOWN)

@guarded
async def cmd_miperfil(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id if update.effective_user else 0
    u=_users(); nick=u["nicknames"].get(str(uid),"-")
    bets=_load_bets(); mine=[b for b in bets if b.user_id==uid]
    w=sum(1 for b in mine if b.result=="win"); l=sum(1 for b in mine if b.result=="loss")
    await update.message.reply_text(
        f"👤 *Perfil* — ID:`{uid}`  Alias:*{nick}*\n"
        f"{'👑 Admin' if _is_admin(uid) else '👤 Usuario'}\n"
        f"📊 `{w}W/{l}L` · {sum(1 for b in mine if not b.result)} pend",parse_mode=ParseMode.MARKDOWN)

@guarded
async def cmd_lineup(update:Update, context:ContextTypes.DEFAULT_TYPE):
    w=await update.message.reply_text("⏳ Cargando alineaciones...",parse_mode=ParseMode.MARKDOWN)
    snap=await get_live_snapshot()
    if not snap or not snap.games: await w.edit_text("❌ Sin datos de partidos."); return
    args=context.args or []; filt=args[0].upper() if args else ""
    for g in snap.games:
        at=(g.get("awayTeam") or {}).get("teamTricode","")
        ht=(g.get("homeTeam") or {}).get("teamTricode","")
        if filt and filt not in (at,ht): continue
        gid=g.get("gameId","")
        box=await get_boxscore(gid) if gid else None
        st_icon={1:"🕐",2:"🔴",3:"🏁"}.get(g.get("gameStatus",1),"·")
        lines=[f"{'─'*30}\n{st_icon} *{at} @ {ht}* — {g.get('gameStatusText','')}"]
        for tri,team_key in [(at,"awayTeam"),(ht,"homeTeam")]:
            pls=box.players(tri) if box else []
            starters=[p for p in pls if p.get("starter")=="1"]
            inactive=[p for p in pls if p.get("status","").lower() in ("inactive","out")]
            lines.append(f"\n*{tri}:*")
            if starters: lines+=["  5️⃣ "+", ".join(f"{p.get('firstName','')} {p.get('familyName','')}".strip() for p in starters[:5])]
            if inactive: lines+=["  🔴 "+", ".join(f"{p.get('firstName','')} {p.get('familyName','')}".strip() for p in inactive)]
            if not pls: lines.append("  _(datos no disponibles aún)_")
        await update.message.reply_text("\n".join(lines),parse_mode=ParseMode.MARKDOWN)
    await w.delete()

# ═══════════════════════════════════════════════════════════════
# 17. BACKGROUND JOBS
# ═══════════════════════════════════════════════════════════════
async def bg_live_scan(context:ContextTypes.DEFAULT_TYPE):
    """Escanea partidos en vivo y envía alertas cuando una prop alcanza el threshold."""
    cid=context.job.chat_id
    snap=await get_live_snapshot()
    if not snap or not snap.live_games(): return  # sin juegos en vivo: no hace nada
    if not snap.is_fresh(): log.debug("Scoreboard stale, omitiendo scan"); return

    props=load_pm_props(snap)
    by_name:Dict[str,List[Prop]]={};
    for p in props: by_name.setdefault(p.player.lower(),[]).append(p)

    for g in snap.live_games():
        gid=g.get("gameId","")
        if not gid: continue
        box=await get_boxscore(gid)
        if not box or not box.is_fresh(): continue  # datos viejos: omitir
        period=int(g.get("period",0) or 0)
        clk=g.get("gameClock","") or ""; clk_s=clock_to_sec(clk)
        diff=abs(int((g.get("homeTeam") or {}).get("score",0))-int((g.get("awayTeam") or {}).get("score",0)))
        is_clutch=diff<=8; is_blow=diff>=BLOWOUT_IS
        for pl in box.players():
            fname=f"{pl.get('firstName','')} {pl.get('familyName','')}".strip().lower()
            for pr in by_name.get(fname,[]):
                pid=pl.get("personId",0)
                s=pl.get("statistics",{})
                mins=parse_minutes(s.get("minutes",""))
                pf=float(s.get("foulsPersonal",0) or 0)
                actual=stat_of(s,pr.tipo)
                po,pu,meta=pre_cached(pid,pr.tipo,pr.line)
                pre=(po if pr.side=="over" else pu)
                if pr.side=="over":
                    lsc=live_over_score(pr.tipo,pr.line-actual,mins,pf,period,clk_s,diff,is_clutch,is_blow)
                else:
                    lsc=live_under_score(pr.tipo,pr.line-actual,mins,pf,period,clk_s,diff,is_blow)
                sc=final_score(lsc,pre,pr.side)
                if sc<ALERT_THRESH and not (is_clutch and sc>=ALERT_CLUTCH): continue
                key=f"{gid}|{pid}|{pr.tipo}|{pr.side}|{pr.line}"
                if not _can_alert(key): continue
                side_tag="OVER" if pr.side=="over" else "UNDER"
                msg=(f"{'🎯' if pr.side=='over' else '🧊'} *ALERTA {side_tag}* · `{sc}/100`\n"
                     f"👤 *{pr.player}* · {pr.tipo.upper()} {side_tag} `{pr.line}`\n"
                     f"📊 actual `{actual:.0f}` · Q{period} {clk} · Dif`{diff}`\n"
                     f"PRE`{pre}` LIVE`{lsc}`")
                await context.bot.send_message(cid,msg,parse_mode=ParseMode.MARKDOWN)
                # Guardar señal ingame
                sig=Signal(signal_id=sig_id(pr.player,pr.tipo,pr.line,pr.side,"ingame"),
                           ts=now(),kind="ingame",player=norm_name(pr.player),player_id=pid,
                           market=pr.tipo,line=pr.line,side=pr.side,game_slug=pr.game_slug,
                           implied_prob=round((0.25+pre/100*0.50),3),model_prob=round(sc/100,3),
                           edge=round((sc-pre)/10.0,1),confidence=sc,
                           reason_codes=[f"live_{lsc}"],risk_flags=[],level="entry",
                           market_id=pr.market_id,source=pr.source)
                db_save_signal(sig,period=period,clock=clk,score_diff=diff)
                _inc_risk(norm_name(pr.player))

async def bg_pregame_alerts(context:ContextTypes.DEFAULT_TYPE):
    """Alertas pre-partido cuando PRE score supera el umbral."""
    cid=context.job.chat_id
    snap=await get_live_snapshot()
    if not snap: return
    props=[p for p in load_pm_props(snap) if p.side=="over"
           and (p.game_slug or "") in {snap.game_slug(g) for g in snap.pregame_games()}]
    state=_json_load("pregame_alerts.json",{})
    today=date.today().isoformat()
    for p in props:
        key=f"{today}|{p.player.lower()}|{p.tipo}|{p.line}"
        if state.get(key,0) > now()-20*3600: continue
        def _calc(player=p.player,tipo=p.tipo,line=p.line):
            pid=get_pid(player)
            if not pid: return None,0,{}
            po,_,meta=pre_cached(pid,tipo,line)
            return pid,po,meta
        pid,pre,meta=await asyncio.to_thread(_calc)
        if not pid or pre<68: continue
        avg=meta.get("avg10"); h5=meta.get("hits5","?"); n5=meta.get("n5","?")
        msg=(f"🔔 *ALERTA PRE-PARTIDO* · `{pre}/100`\n"
             f"👤 *{p.player}* · {TIPO_ICON.get(p.tipo,'·')} {p.tipo.upper()} OVER `{p.line}`\n"
             f"📊 prom`{avg}` · `{h5}/{n5}` ult5\n_{slug_matchup(p.game_slug)}_")
        await context.bot.send_message(cid,msg,parse_mode=ParseMode.MARKDOWN)
        state[key]=now()
    _json_save("pregame_alerts.json",state)

async def bg_morning_check(context:ContextTypes.DEFAULT_TYPE):
    """Dispara el resumen matutino a la hora configurada."""
    if datetime.now().hour!=MORNING_HOUR: return
    state=_json_load("morning_state.json",{})
    today=date.today().isoformat()
    if state.get("date")==today: return
    state["date"]=today; _json_save("morning_state.json",state)
    cid=context.job.chat_id
    snap=await get_live_snapshot()
    if not snap or not snap.games: return
    lines=[f"🌅 *Resumen NBA — {date.today().strftime('%A %d/%m/%Y').capitalize()}*\n"]
    for g in snap.games:
        at=(g.get("awayTeam") or {}).get("teamTricode","?")
        ht=(g.get("homeTeam") or {}).get("teamTricode","?")
        lines.append(f"· *{at}* @ *{ht}* — {g.get('gameStatusText','')}")
    lines.append("\n_Usa /odds · /alertas · /lineup para más detalles_")
    await context.bot.send_message(cid,"\n".join(lines),parse_mode=ParseMode.MARKDOWN)

async def bg_autoresolve(context:ContextTypes.DEFAULT_TYPE):
    """Resuelve apuestas pendientes cuando el partido terminó."""
    cid=context.job.chat_id
    bets=_load_bets(); pend=[b for b in bets if not b.result]
    if not pend: return
    snap=await get_live_snapshot()
    if not snap: return
    finished={snap.game_slug(g):g.get("gameId","") for g in snap.finished_games()}
    changed=False
    for bet in pend:
        gid=finished.get(bet.game_slug,"")
        if not gid: continue
        box=await get_boxscore(gid)
        if not box: continue
        pid=get_pid(bet.player)
        actual=None
        for pl in box.players():
            if pl.get("personId")==pid:
                actual=stat_of(pl.get("statistics",{}),bet.tipo); break
        if actual is None: continue
        if bet.side=="over": res="win" if actual>bet.line else ("push" if actual==bet.line else "loss")
        else: res="win" if actual<bet.line else ("push" if actual==bet.line else "loss")
        bet.result=res; bet.actual_stat=actual; bet.resolved_at=now(); changed=True
        e={"win":"✅","loss":"❌","push":"🔁"}[res]
        await context.bot.send_message(cid,
            f"🤖 *Auto-resultado* `#{bet.id}`\n👤 {bet.player} {bet.tipo.upper()} {bet.side.upper()} `{bet.line}`\n"
            f"📊 Real:`{actual:.0f}` → {e} *{res.upper()}*",parse_mode=ParseMode.MARKDOWN)
    if changed: _save_bets(bets)

# ═══════════════════════════════════════════════════════════════
# 18. MAIN
# ═══════════════════════════════════════════════════════════════
async def on_startup(app:Application):
    db_init()
    await app.bot.set_my_commands([
        BotCommand("start","Activar el bot"), BotCommand("games","Partidos hoy"),
        BotCommand("live","Props en vivo"), BotCommand("odds","Props con PRE score"),
        BotCommand("alertas","Mejores props hoy"), BotCommand("signals","Señales con edge"),
        BotCommand("analisis","Análisis profundo"), BotCommand("contexto","Contexto defensivo"),
        BotCommand("dashboard","Métricas y ROI"), BotCommand("lineup","Alineaciones"),
        BotCommand("bet","Registrar apuesta"), BotCommand("misapuestas","Apuestas pendientes"),
        BotCommand("historial","ROI y estadísticas"), BotCommand("resultado","Cerrar apuesta"),
        BotCommand("miperfil","Ver mi perfil"), BotCommand("help","Ayuda"),
    ])
    log.info("Bot iniciado ✅")

def main():
    app=Application.builder().token(TOKEN).build()
    H=CommandHandler
    handlers=[
        ("start",cmd_start),("help",cmd_help),("games",cmd_games),("today",cmd_games),
        ("live",cmd_live),("odds",cmd_odds),("signals",cmd_signals),
        ("alertas",cmd_alertas),("analisis",cmd_analisis),("contexto",cmd_contexto),
        ("dashboard",cmd_dashboard),("status",cmd_status),("lineup",cmd_lineup),
        ("bet",cmd_bet),("resultado",cmd_resultado),("historial",cmd_historial),
        ("misapuestas",cmd_misapuestas),("miperfil",cmd_miperfil),
        ("adduser",cmd_adduser),("removeuser",cmd_removeuser),("usuarios",cmd_usuarios),
    ]
    for name,fn in handlers: app.add_handler(H(name,fn))
    app.post_init=on_startup
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__=="__main__": main()