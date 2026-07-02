"""
Local waste disposal database — SQLite-backed, thread-safe.
Provides lookup, auto-save, and stats tracking so OpenAI is only
called when the local cache misses.
"""

import sqlite3
import json
import re
import threading
import logging
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

DB_FILE = Path(__file__).parent / "waste.db"
_db_lock = threading.Lock()

COST_PER_OPENAI_CALL_USD = 0.01   # rough average across text + vision calls

# ---------------------------------------------------------------------------
# Seed data — pre-populated on first run
# ---------------------------------------------------------------------------

SEED_DATA = [
    {
        "object_name": "Lampadina LED",
        "synonyms": ["lampadina", "lampadina a basso consumo", "lampadina fluorescente",
                     "neon", "lampada", "lampadina alogena"],
        "category": "RAEE",
        "disposal_rules": (
            "💡 **Lampadina LED**\n\n"
            "✅ **Dove va:** Ecocentro comunale (RAEE)\n\n"
            "ℹ️ Nota: Accettata anche nei punti raccolta di negozi e supermercati.\n\n"
            "⚠️ Attenzione: Non buttare nel bidone normale — contiene materiali pericolosi."
        ),
        "notes": "Include lampadine fluorescenti, LED, alogene.",
    },
    {
        "object_name": "Polistirolo",
        "synonyms": ["foam", "imbottitura bianca", "polistirene", "schiuma bianca"],
        "category": "indifferenziato",
        "disposal_rules": (
            "🔍 Oggetto: Polistirolo\n\n"
            "✅ Dove si butta: **Bidone grigio (indifferenziato)**\n\n"
            "ℹ️ Nota: Schiaccialo per ridurre il volume prima di conferire.\n\n"
            "_Le regole possono cambiare in base al Comune._"
        ),
        "notes": "In alcuni comuni va nel bidone giallo o all'ecocentro.",
    },
    {
        "object_name": "Cartone della pizza",
        "synonyms": ["scatola pizza", "cartone pizza", "box pizza", "scatola della pizza"],
        "category": "carta",
        "disposal_rules": (
            "🔍 Oggetto: Cartone della pizza\n\n"
            "✅ Dove si butta: **Bidone blu (carta)** se pulito — "
            "**indifferenziato** se unto\n\n"
            "ℹ️ Nota: La parte untuosa non è riciclabile, separala se possibile.\n\n"
            "⚠️ Attenzione: Il grasso contamina il processo di riciclaggio della carta."
        ),
        "notes": "Solo la parte pulita va nella carta.",
    },
    {
        "object_name": "Padella",
        "synonyms": ["pentola", "tegame", "wok", "casseruola", "padellino", "teglia"],
        "category": "RAEE",
        "disposal_rules": (
            "🔍 Oggetto: Padella\n\n"
            "✅ Dove si butta: **Ecocentro comunale** (metalli/RAEE)\n\n"
            "ℹ️ Nota: Le padelle antiaderenti non vanno nel bidone giallo.\n\n"
            "⚠️ Attenzione: Il rivestimento antiaderente non è riciclabile normalmente."
        ),
        "notes": "Ferro puro → bidone giallo in alcuni comuni.",
    },
    {
        "object_name": "Batteria",
        "synonyms": ["pila", "pile", "batterie", "accumulatore", "batteria stilo",
                     "batteria a bottone", "batteria ricaricabile"],
        "category": "batterie",
        "disposal_rules": (
            "🔍 Oggetto: Batteria / Pila\n\n"
            "✅ Dove si butta: **Contenitore arancione** (negozi, supermercati, farmacie)\n\n"
            "ℹ️ Nota: Non buttare mai nel bidone normale.\n\n"
            "⚠️ Attenzione: Contengono metalli pesanti tossici — rifiuto pericoloso."
        ),
        "notes": "",
    },
    {
        "object_name": "Tetra Pak",
        "synonyms": ["tetrapak", "tetrapack", "brick latte", "cartone del latte",
                     "succo di frutta brick", "bevanda brick", "cartone succo"],
        "category": "carta",
        "disposal_rules": (
            "🔍 Oggetto: Tetra Pak\n\n"
            "✅ Dove si butta: **Bidone giallo** o **bidone blu** (varia per Comune)\n\n"
            "ℹ️ Nota: Svuota, sciacqua e schiaccia prima di conferire.\n\n"
            "_Le regole possono cambiare in base al Comune._"
        ),
        "notes": "In molti comuni va nel giallo (multi-materiale), in altri nel blu.",
    },
    {
        "object_name": "Olio esausto",
        "synonyms": ["olio fritto", "olio di cottura usato", "olio vegetale esausto", "olio cucina"],
        "category": "indifferenziato",
        "disposal_rules": (
            "🔍 Oggetto: Olio esausto\n\n"
            "✅ Dove si butta: **Ecocentro comunale** o contenitori appositi\n\n"
            "ℹ️ Nota: Versalo in una bottiglia chiusa, poi portala all'ecocentro.\n\n"
            "⚠️ Attenzione: Non versare mai nel lavandino — inquina le falde acquifere."
        ),
        "notes": "",
    },
    {
        "object_name": "Farmaci scaduti",
        "synonyms": ["medicinali", "farmaci", "medicine", "pillole", "sciroppo scaduto",
                     "farmaco scaduto", "antibiotico scaduto"],
        "category": "farmaci",
        "disposal_rules": (
            "🔍 Oggetto: Farmaci scaduti\n\n"
            "✅ Dove si butta: **Contenitore apposito in farmacia**\n\n"
            "ℹ️ Nota: Non buttarli nel water né nel bidone normale.\n\n"
            "⚠️ Attenzione: Sono rifiuti speciali — raccolta esclusivamente in farmacia."
        ),
        "notes": "",
    },
    {
        "object_name": "Scontrino",
        "synonyms": ["scontrino fiscale", "ricevuta", "carta termica", "biglietto autobus",
                     "biglietto treno", "scontrino cassa"],
        "category": "indifferenziato",
        "disposal_rules": (
            "🔍 Oggetto: Scontrino / Carta termica\n\n"
            "✅ Dove si butta: **Bidone grigio (indifferenziato)**\n\n"
            "ℹ️ Nota: La carta termica contiene BPA — non è riciclabile.\n\n"
            "⚠️ Attenzione: Non mettere nel bidone della carta."
        ),
        "notes": "",
    },
    {
        "object_name": "Pannolino",
        "synonyms": ["pannolini", "pannolone", "assorbente", "assorbenti igienici"],
        "category": "indifferenziato",
        "disposal_rules": (
            "🔍 Oggetto: Pannolino / Assorbente\n\n"
            "✅ Dove si butta: **Bidone grigio (indifferenziato)**\n\n"
            "ℹ️ Nota: Chiudilo in un sacchetto prima di gettarlo.\n\n"
            "_Le regole possono cambiare in base al Comune._"
        ),
        "notes": "In alcuni comuni esiste raccolta separata per pannolini.",
    },
    {
        "object_name": "Bottiglia di vetro",
        "synonyms": ["bottiglia", "bottiglie", "bottiglia di vino", "bottiglia di birra",
                     "vasetto di vetro", "barattolo di vetro"],
        "category": "vetro",
        "disposal_rules": (
            "🔍 Oggetto: Bottiglia di vetro\n\n"
            "✅ Dove si butta: **Campana verde del vetro**\n\n"
            "ℹ️ Nota: Svuota e risciacqua. Il tappo va nel bidone giallo.\n\n"
            "⚠️ Attenzione: Non buttare Pyrex, specchi o ceramiche nel vetro."
        ),
        "notes": "",
    },
    {
        "object_name": "Plastica generica",
        "synonyms": ["sacchetto di plastica", "busta di plastica", "imballaggio plastico",
                     "contenitore in plastica", "vaschetta plastica"],
        "category": "plastica",
        "disposal_rules": (
            "🔍 Oggetto: Plastica generica\n\n"
            "✅ Dove si butta: **Bidone giallo** (plastica e metalli)\n\n"
            "ℹ️ Nota: Risciacqua i contenitori con residui di cibo prima di conferire."
        ),
        "notes": "",
    },
    {
        "object_name": "Carta e cartone",
        "synonyms": ["giornale", "rivista", "libro", "cartone", "scatola di cartone",
                     "imballaggio di carta", "carta da giornale"],
        "category": "carta",
        "disposal_rules": (
            "🔍 Oggetto: Carta / Cartone\n\n"
            "✅ Dove si butta: **Bidone blu** (carta e cartone)\n\n"
            "ℹ️ Nota: Appiattisci le scatole. Non inserire carta sporca di cibo o plastificata."
        ),
        "notes": "",
    },
    {
        "object_name": "Organico",
        "synonyms": ["scarti alimentari", "avanzi di cibo", "bucce", "fondi di caffe",
                     "gusci", "umido", "scarti cucina", "rifiuto umido"],
        "category": "organico",
        "disposal_rules": (
            "🔍 Oggetto: Organico / Umido\n\n"
            "✅ Dove si butta: **Bidone marrone** (usa sacchetto compostabile)\n\n"
            "ℹ️ Nota: Include fondi caffè, gusci d'uovo, fiori, bucce di frutta e verdura."
        ),
        "notes": "",
    },
    {
        "object_name": "Smartphone",
        "synonyms": ["cellulare", "telefono", "iphone", "android", "tablet", "telefonino"],
        "category": "RAEE",
        "disposal_rules": (
            "🔍 Oggetto: Smartphone / Cellulare\n\n"
            "✅ Dove si butta: **Ecocentro comunale (RAEE)**\n\n"
            "ℹ️ Nota: I negozi > 400 m² sono obbligati a ritirarlo gratuitamente.\n\n"
            "⚠️ Attenzione: Contiene materiali preziosi e pericolosi — non nell'indifferenziato."
        ),
        "notes": "",
    },
    {
        "object_name": "Carta igienica",
        "synonyms": ["rotolo carta igienica", "fazzoletti", "tovaglioli carta", "carta assorbente"],
        "category": "indifferenziato",
        "disposal_rules": (
            "🔍 Oggetto: Carta igienica / Fazzoletti\n\n"
            "✅ Dove si butta: **Bidone grigio (indifferenziato)**\n\n"
            "ℹ️ Nota: Carta igienica usata e fazzoletti non si riciclano.\n\n"
            "⚠️ Attenzione: Non metterli nel bidone blu della carta."
        ),
        "notes": "Solo il cartone del rotolo (pulito) può andare nel bidone blu.",
    },
    {
        "object_name": "Lattina",
        "synonyms": ["lattine", "barattolo di alluminio", "lattina bibita", "lattina birra",
                     "alluminio", "latta"],
        "category": "plastica",
        "disposal_rules": (
            "🔍 Oggetto: Lattina / Alluminio\n\n"
            "✅ Dove si butta: **Bidone giallo** (plastica e metalli)\n\n"
            "ℹ️ Nota: Sciacqua prima di conferire. Puoi schiacciarla per risparmiare spazio."
        ),
        "notes": "",
    },
    {
        "object_name": "Pneumatico",
        "synonyms": ["gomma", "pneumatici", "ruota", "copertone", "gomme auto"],
        "category": "indifferenziato",
        "disposal_rules": (
            "🔍 Oggetto: Pneumatico / Gomma\n\n"
            "✅ Dove si butta: **Gommista** o **ecocentro comunale**\n\n"
            "ℹ️ Nota: È obbligatorio lo smaltimento tramite canali autorizzati.\n\n"
            "⚠️ Attenzione: Non buttare nell'indifferenziato — è reato."
        ),
        "notes": "",
    },
    {
        "object_name": "Olio motore",
        "synonyms": ["olio motore esausto", "olio auto", "olio motore usato"],
        "category": "indifferenziato",
        "disposal_rules": (
            "🔍 Oggetto: Olio motore esausto\n\n"
            "✅ Dove si butta: **Ecocentro comunale** o officina autorizzata\n\n"
            "ℹ️ Nota: Conservalo in contenitore chiuso, mai mischiare con altri rifiuti.\n\n"
            "⚠️ Attenzione: Altamente inquinante — smaltimento obbligatorio tramite ecocentro."
        ),
        "notes": "",
    },
]

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_FILE), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def normalize_key(text: str) -> str:
    """Lowercase, remove accents, keep only alphanumeric + spaces."""
    t = text.lower().strip()
    accents = {"à": "a", "è": "e", "é": "e", "ì": "i", "ò": "o", "ù": "u",
               "á": "a", "ê": "e", "ï": "i", "ô": "o", "û": "u"}
    for a, b in accents.items():
        t = t.replace(a, b)
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _infer_category(text: str) -> str:
    t = text.lower()
    if "campana verde" in t or "vetro" in t:
        return "vetro"
    if "bidone giallo" in t or "plastica" in t or "metalli" in t or "alluminio" in t:
        return "plastica"
    if "bidone blu" in t or "carta" in t or "cartone" in t:
        return "carta"
    if "bidone marrone" in t or "organico" in t or "umido" in t:
        return "organico"
    if "contenitore arancione" in t or "batterie" in t or "pile" in t:
        return "batterie"
    if "ecocentro" in t or "raee" in t or "elettronica" in t:
        return "RAEE"
    if "farmacia" in t:
        return "farmaci"
    return "indifferenziato"

# ---------------------------------------------------------------------------
# Init & seed
# ---------------------------------------------------------------------------

def init_db() -> None:
    with _db_lock:
        conn = _get_conn()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS waste_objects (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    object_name   TEXT NOT NULL,
                    object_key    TEXT NOT NULL UNIQUE,
                    synonyms      TEXT NOT NULL DEFAULT '[]',
                    category      TEXT NOT NULL DEFAULT '',
                    disposal_rules TEXT NOT NULL DEFAULT '',
                    comune_overrides TEXT NOT NULL DEFAULT '{}',
                    notes         TEXT NOT NULL DEFAULT '',
                    updated_at    TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS stats (
                    id                    INTEGER PRIMARY KEY CHECK (id = 1),
                    cache_hits            INTEGER NOT NULL DEFAULT 0,
                    openai_requests_total INTEGER NOT NULL DEFAULT 0,
                    openai_today          INTEGER NOT NULL DEFAULT 0,
                    today_date            TEXT NOT NULL DEFAULT '',
                    total_response_ms     INTEGER NOT NULL DEFAULT 0,
                    response_count        INTEGER NOT NULL DEFAULT 0,
                    photos_processed      INTEGER NOT NULL DEFAULT 0,
                    text_searches         INTEGER NOT NULL DEFAULT 0
                )
            """)
            # Migrate existing stats table — add new columns if they don't exist
            existing_cols = {
                row[1] for row in conn.execute("PRAGMA table_info(stats)").fetchall()
            }
            for col, typedef in (
                ("photos_processed", "INTEGER NOT NULL DEFAULT 0"),
                ("text_searches",    "INTEGER NOT NULL DEFAULT 0"),
            ):
                if col not in existing_cols:
                    conn.execute(f"ALTER TABLE stats ADD COLUMN {col} {typedef}")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    telegram_user_id  INTEGER PRIMARY KEY,
                    username          TEXT,
                    first_name        TEXT,
                    language_code     TEXT,
                    first_seen        TEXT NOT NULL,
                    last_seen         TEXT NOT NULL
                )
            """)
            today = date.today().isoformat()
            conn.execute(
                "INSERT OR IGNORE INTO stats (id, today_date) VALUES (1, ?)", (today,)
            )
            conn.commit()
            _seed(conn)
        finally:
            conn.close()

    s = get_stats()
    logger.info(
        "[db] Ready — %d objects | %d users | %d cache hits | %d OpenAI calls total",
        s["objects"], s["total_users"], s["cache_hits"], s["openai_total"],
    )


def _seed(conn: sqlite3.Connection) -> None:
    today = date.today().isoformat()
    added = 0
    for item in SEED_DATA:
        key = normalize_key(item["object_name"])
        exists = conn.execute(
            "SELECT id FROM waste_objects WHERE object_key=?", (key,)
        ).fetchone()
        if not exists:
            conn.execute(
                """INSERT INTO waste_objects
                   (object_name, object_key, synonyms, category, disposal_rules, notes, updated_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    item["object_name"], key,
                    json.dumps(item.get("synonyms", []), ensure_ascii=False),
                    item.get("category", ""),
                    item.get("disposal_rules", ""),
                    item.get("notes", ""),
                    today,
                ),
            )
            added += 1
    conn.commit()
    if added:
        logger.info("[db] Seeded %d new objects", added)

# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------

def lookup(query: str, comune: str | None = None) -> str | None:
    """
    Search the local DB. Returns formatted disposal string or None (cache miss).
    Matching priority:
      1. Exact key match
      2. Synonym exact match
      3. Token overlap ≥ 70 %
    """
    key = normalize_key(query)
    tokens = set(key.split()) - {"di", "del", "della", "lo", "la", "le", "il", "un", "una"}

    with _db_lock:
        conn = _get_conn()
        try:
            rows = conn.execute(
                "SELECT object_name, object_key, synonyms, disposal_rules, comune_overrides "
                "FROM waste_objects"
            ).fetchall()
        finally:
            conn.close()

    best_row = None
    best_score = 0.0

    for row in rows:
        row_key = row["object_key"]
        synonyms: list[str] = json.loads(row["synonyms"])
        syn_keys = [normalize_key(s) for s in synonyms]

        # 1. Exact
        if key == row_key or key in syn_keys:
            best_row = row
            best_score = 1.0
            break

        # 2. Token overlap
        row_tokens = set(row_key.split())
        for sk in syn_keys:
            row_tokens |= set(sk.split())
        row_tokens -= {"di", "del", "della", "lo", "la", "le", "il", "un", "una"}

        if tokens and row_tokens:
            overlap = len(tokens & row_tokens) / max(len(tokens), len(row_tokens))
            if overlap >= 0.7 and overlap > best_score:
                best_row = row
                best_score = overlap

    if not best_row:
        return None

    # Comune override
    if comune:
        overrides: dict = json.loads(best_row["comune_overrides"] or "{}")
        override = overrides.get(comune.lower().strip())
        if override:
            return override

    return best_row["disposal_rules"]

# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------

def save_result(
    object_name: str,
    disposal_rules: str,
    category: str = "",
    synonyms: list[str] | None = None,
    notes: str = "",
    comune: str | None = None,
    comune_rules: str | None = None,
) -> None:
    """Upsert a waste object. Never raises — errors are logged only."""
    key = normalize_key(object_name)
    if not key or len(key) < 2:
        return
    if not category:
        category = _infer_category(disposal_rules)

    today = date.today().isoformat()

    with _db_lock:
        conn = _get_conn()
        try:
            existing = conn.execute(
                "SELECT id, synonyms, comune_overrides FROM waste_objects WHERE object_key=?",
                (key,),
            ).fetchone()

            if existing:
                ex_syns: set = set(json.loads(existing["synonyms"]))
                if synonyms:
                    ex_syns.update(synonyms)
                ex_overrides: dict = json.loads(existing["comune_overrides"])
                if comune and comune_rules:
                    ex_overrides[comune.lower().strip()] = comune_rules
                conn.execute(
                    """UPDATE waste_objects
                       SET synonyms=?, comune_overrides=?, updated_at=?
                       WHERE object_key=?""",
                    (json.dumps(list(ex_syns), ensure_ascii=False),
                     json.dumps(ex_overrides, ensure_ascii=False),
                     today, key),
                )
                logger.info("[db] Updated: %s", object_name)
            else:
                overrides = {}
                if comune and comune_rules:
                    overrides[comune.lower().strip()] = comune_rules
                conn.execute(
                    """INSERT INTO waste_objects
                       (object_name, object_key, synonyms, category, disposal_rules,
                        comune_overrides, notes, updated_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (object_name, key,
                     json.dumps(synonyms or [], ensure_ascii=False),
                     category, disposal_rules,
                     json.dumps(overrides, ensure_ascii=False),
                     notes, today),
                )
                logger.info("[db] Saved new object: %s", object_name)
            conn.commit()
        except Exception:
            logger.exception("[db] Error saving object: %s", object_name)
        finally:
            conn.close()

# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def upsert_user(
    telegram_user_id: int,
    username: str | None,
    first_name: str | None,
    language_code: str | None,
) -> None:
    """Insert new user or update last_seen. Never raises."""
    now = date.today().isoformat()
    with _db_lock:
        conn = _get_conn()
        try:
            conn.execute(
                """INSERT INTO users (telegram_user_id, username, first_name, language_code,
                                     first_seen, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(telegram_user_id) DO UPDATE SET
                       username=excluded.username,
                       first_name=excluded.first_name,
                       language_code=excluded.language_code,
                       last_seen=excluded.last_seen""",
                (telegram_user_id, username, first_name, language_code, now, now),
            )
            conn.commit()
        except Exception:
            logger.exception("[db] upsert_user failed for user_id=%s", telegram_user_id)
        finally:
            conn.close()


def record_photo() -> None:
    with _db_lock:
        conn = _get_conn()
        try:
            conn.execute("UPDATE stats SET photos_processed=photos_processed+1 WHERE id=1")
            conn.commit()
        finally:
            conn.close()


def record_text_search() -> None:
    with _db_lock:
        conn = _get_conn()
        try:
            conn.execute("UPDATE stats SET text_searches=text_searches+1 WHERE id=1")
            conn.commit()
        finally:
            conn.close()


def record_cache_hit() -> None:
    with _db_lock:
        conn = _get_conn()
        try:
            conn.execute(
                "UPDATE stats SET cache_hits=cache_hits+1 WHERE id=1"
            )
            conn.commit()
        finally:
            conn.close()


def record_openai_request(elapsed_ms: int) -> None:
    today = date.today().isoformat()
    with _db_lock:
        conn = _get_conn()
        try:
            row = conn.execute(
                "SELECT today_date FROM stats WHERE id=1"
            ).fetchone()
            reset_today = row and row["today_date"] != today
            if reset_today:
                conn.execute(
                    """UPDATE stats SET
                        today_date=?, openai_today=1,
                        openai_requests_total=openai_requests_total+1,
                        total_response_ms=total_response_ms+?,
                        response_count=response_count+1
                       WHERE id=1""",
                    (today, elapsed_ms),
                )
            else:
                conn.execute(
                    """UPDATE stats SET
                        openai_today=openai_today+1,
                        openai_requests_total=openai_requests_total+1,
                        total_response_ms=total_response_ms+?,
                        response_count=response_count+1
                       WHERE id=1""",
                    (elapsed_ms,),
                )
            conn.commit()
        finally:
            conn.close()


def get_stats() -> dict:
    today = date.today().isoformat()
    with _db_lock:
        conn = _get_conn()
        try:
            s = conn.execute("SELECT * FROM stats WHERE id=1").fetchone()
            n = conn.execute("SELECT COUNT(*) AS n FROM waste_objects").fetchone()["n"]
            total_users = conn.execute(
                "SELECT COUNT(*) AS n FROM users"
            ).fetchone()["n"]
            new_today = conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE first_seen=?", (today,)
            ).fetchone()["n"]
            active_24h = conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE last_seen=?", (today,)
            ).fetchone()["n"]
        finally:
            conn.close()

    if not s:
        return {
            "objects": n, "total_users": total_users, "new_today": new_today,
            "active_24h": active_24h, "cache_hits": 0, "openai_today": 0,
            "openai_total": 0, "money_saved_usd": 0.0, "avg_response_ms": 0,
            "photos_processed": 0, "text_searches": 0,
        }

    hits        = s["cache_hits"]
    total_ms    = s["total_response_ms"]
    resp_count  = s["response_count"]
    avg_ms      = round(total_ms / resp_count) if resp_count else 0
    money_saved = round(hits * COST_PER_OPENAI_CALL_USD, 2)

    return {
        "objects":          n,
        "total_users":      total_users,
        "new_today":        new_today,
        "active_24h":       active_24h,
        "photos_processed": s["photos_processed"],
        "text_searches":    s["text_searches"],
        "cache_hits":       hits,
        "openai_today":     s["openai_today"],
        "openai_total":     s["openai_requests_total"],
        "money_saved_usd":  money_saved,
        "avg_response_ms":  avg_ms,
    }
