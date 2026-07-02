import asyncio
import os
import json
import logging
import base64
import time
import re
import httpx
from pathlib import Path
from openai import OpenAI
import waste_db
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OpenAI client
# ---------------------------------------------------------------------------

openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

OPENAI_TIMEOUT_SECS = 30   # asyncio.wait_for timeout (event-loop level)
TIMEOUT_REPLY = "⚠️ Analisi troppo lunga. Riprova con una foto più chiara."
ERROR_REPLY   = "⚠️ Errore temporaneo. Riprova tra qualche secondo."

# ---------------------------------------------------------------------------
# Persistent user storage  (bot/users.json)
# ---------------------------------------------------------------------------

USERS_FILE = Path(__file__).parent / "users.json"


def _load_users() -> dict:
    if USERS_FILE.exists():
        try:
            return json.loads(USERS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_users(data: dict) -> None:
    USERS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_user_comune(user_id: int) -> str | None:
    return _load_users().get(str(user_id), {}).get("comune")


def set_user_comune(user_id: int, comune: str) -> None:
    data = _load_users()
    data.setdefault(str(user_id), {})["comune"] = comune
    _save_users(data)


# ---------------------------------------------------------------------------
# In-memory Comune rules cache  {comune_lower: (rules_text, timestamp)}
# ---------------------------------------------------------------------------

COMUNE_CACHE: dict[str, tuple[str, float]] = {}
CACHE_TTL_SECONDS = 3600  # 1 hour


def _get_cached_rules(comune: str) -> str | None:
    key = comune.lower().strip()
    if key in COMUNE_CACHE:
        rules, ts = COMUNE_CACHE[key]
        if time.time() - ts < CACHE_TTL_SECONDS:
            logger.info("[comune-cache] Hit: %s", comune)
            return rules
    return None


def _set_cached_rules(comune: str, rules: str) -> None:
    COMUNE_CACHE[comune.lower().strip()] = (rules, time.time())


def _fetch_comune_rules_sync(comune: str) -> str:
    """Blocking: fetch Comune-specific recycling rules from GPT-4.1."""
    cached = _get_cached_rules(comune)
    if cached:
        return cached

    logger.info("[comune] Fetching rules for: %s", comune)
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4.1",
            max_tokens=300,
            timeout=OPENAI_TIMEOUT_SECS,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Sei un esperto di raccolta differenziata in Italia. "
                        "Rispondi SOLO in italiano, in modo conciso."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Elenca le principali differenze nelle regole di raccolta differenziata "
                        f"del Comune di {comune} rispetto allo standard nazionale italiano. "
                        f"Includi solo le differenze reali e verificabili (es. raccolta multi-materiale, "
                        f"Tetra Pak, polistirolo, ecc.). "
                        f"Se non conosci le regole specifiche di {comune} con certezza, "
                        f"scrivi esattamente: 'Nessuna differenza nota: usa le linee guida nazionali.' "
                        f"Massimo 5 righe brevi."
                    ),
                },
            ],
        )
        rules = response.choices[0].message.content.strip()
        logger.info("[comune] Rules received for: %s", comune)
    except Exception:
        logger.exception("[comune] Error fetching rules for: %s", comune)
        rules = "Nessuna differenza nota: usa le linee guida nazionali."

    _set_cached_rules(comune, rules)
    return rules


# ---------------------------------------------------------------------------
# Category buttons (manual fallback via /start or button press)
# ---------------------------------------------------------------------------

WASTE_INFO = {
    "vetro": (
        "🟢 Oggetto: Vetro generico\n"
        "✅ Dove si butta: Campana verde del vetro.\n"
        "ℹ️ Nota: Svuota e risciacqua prima di conferire.\n"
        "🚫 Non va: Pyrex, specchi, ceramiche, lampadine."
    ),
    "plastica": (
        "🟡 Oggetto: Plastica / Metalli\n"
        "✅ Dove si butta: Bidone giallo (plastica e metalli).\n"
        "ℹ️ Nota: Include lattine, alluminio e pellicole.\n"
        "🧴 Prima: Risciacqua i contenitori con residui di cibo."
    ),
    "carta": (
        "🔵 Oggetto: Carta / Cartone\n"
        "✅ Dove si butta: Bidone blu (carta e cartone).\n"
        "ℹ️ Nota: Appiattisci le scatole per risparmiare spazio.\n"
        "🚫 Non va: Carta untuosa, plastificata o termica."
    ),
    "organico": (
        "🟤 Oggetto: Organico / Umido\n"
        "✅ Dove si butta: Bidone marrone (usa sacchetto compostabile).\n"
        "ℹ️ Nota: Include fondi caffè, gusci e fiori.\n"
        "🚫 Non va: Oli liquidi, sacchetti di plastica."
    ),
    "batterie": (
        "🔋 Oggetto: Batterie / Pile\n"
        "✅ Dove si butta: Contenitore arancione (supermercati, farmacie).\n"
        "⚠️ Nota: Non buttare mai nel bidone normale.\n"
        "ℹ️ Info: Cerca il punto più vicino sul sito del tuo Comune."
    ),
    "raee": (
        "⚡ Oggetto: RAEE (elettronica)\n"
        "✅ Dove si butta: Ecocentro comunale (isola ecologica).\n"
        "ℹ️ Nota: I negozi > 400 m² ritirano gratis con acquisto equivalente.\n"
        "🚫 Non va: Mai abbandonare per strada."
    ),
    "indifferenziato": (
        "⚫ Oggetto: Rifiuto indifferenziato\n"
        "✅ Dove si butta: Bidone grigio/nero (secco residuo).\n"
        "ℹ️ Nota: Solo ciò che non si può riciclare.\n"
        "♻️ Ricorda: Separa sempre il più possibile."
    ),
}

WASTE_BUTTONS = [
    [
        InlineKeyboardButton("🟢 Vetro", callback_data="vetro"),
        InlineKeyboardButton("🟡 Plastica", callback_data="plastica"),
        InlineKeyboardButton("🔵 Carta", callback_data="carta"),
    ],
    [
        InlineKeyboardButton("🟤 Organico", callback_data="organico"),
        InlineKeyboardButton("🔋 Batterie", callback_data="batterie"),
    ],
    [
        InlineKeyboardButton("⚡ RAEE", callback_data="raee"),
        InlineKeyboardButton("⚫ Indifferenziato", callback_data="indifferenziato"),
    ],
]

# ---------------------------------------------------------------------------
# OpenAI Vision prompts
# ---------------------------------------------------------------------------

BASE_SYSTEM_PROMPT = (
    "Sei EcoGlass Bot, assistente per la raccolta differenziata in Italia. "
    "Rispondi SEMPRE in italiano.\n\n"

    "═══ ANALISI DELL'IMMAGINE ═══\n"
    "Prima di rispondere, identifica:\n"
    "1. Ogni oggetto presente nella foto (se sono più oggetti, trattali separatamente).\n"
    "2. Il materiale principale di ciascun oggetto.\n"
    "3. Tutte le parti rimovibili (tappo, coperchio, etichetta, batteria, cavo, ecc.).\n\n"

    "REGOLE QUALITÀ — ASSOLUTE, NESSUNA ECCEZIONE:\n"
    "- Se la confidenza è sotto il 90% per QUALSIASI motivo (foto sfocata, angolo sbagliato, "
    "oggetto parzialmente visibile, materiale ambiguo), NON classificare l'oggetto. "
    "Non indovinare. Non tentare una risposta parziale.\n"
    "- In quel caso fai UNA SOLA domanda brevissima chiedendo una foto migliore o un angolo diverso.\n"
    "- Esempio risposta sotto 90%:\n"
    "  📷 **Foto non sufficiente**\n"
    "\n"
    "  Puoi inviare una foto più ravvicinata o da un angolo diverso?\n"
    "- Non inventare mai informazioni.\n\n"

    "REGOLE DI SMALTIMENTO:\n"
    "- Se l'oggetto è composto da più materiali, indica come smaltire OGNI parte rimovibile "
    "separatamente, a meno che non debbano restare uniti (in quel caso dillo esplicitamente).\n"
    "- Se l'oggetto è sporco, contiene cibo o liquidi, indica se va svuotato, sciacquato "
    "o pulito prima del conferimento, secondo le regole del Comune dell'utente.\n"
    "- Se lo smaltimento è insolito o contro-intuitivo, spiega il motivo in UNA frase breve.\n"
    "- Metti in **grassetto** la destinazione finale di ogni oggetto o parte.\n\n"

    "REGOLE NAZIONALI:\n"
    "vetro → **campana verde** | plastica/metalli → **bidone giallo** | "
    "carta/cartone → **bidone blu** | organico/umido → **bidone marrone** (sacchetto compostabile) | "
    "batterie/pile → **contenitore arancione** (negozi/farmacie) | "
    "RAEE → **ecocentro comunale** | resto → **indifferenziato** (grigio/nero).\n\n"

    "═══ FORMATO DI RISPOSTA ═══\n"
    "REGOLE VISIVE OBBLIGATORIE:\n"
    "- La prima riga identifica SEMPRE l'oggetto.\n"
    "- Usa **grassetto** per titoli e destinazione finale.\n"
    "- Lascia UNA riga vuota tra sezioni diverse.\n"
    "- Mai muri di testo: massimo 1-2 righe per sezione.\n"
    "- Ogni risposta deve stare sotto 800 caratteri totali.\n"
    "- Lo stile deve sembrare una premium app mobile, non un testo AI.\n\n"

    "FORMATO oggetto singolo:\n"
    "[emoji] **[Nome oggetto]**\n"
    "\n"
    "✅ **Dove va:** [destinazione in grassetto]\n"
    "\n"
    "[emoji] **Parti:** [parti rimovibili e dove vanno — solo se presenti]\n"
    "\n"
    "[emoji] **Prima:** [cosa fare prima — solo se necessario]\n"
    "\n"
    "[emoji] **Perché:** [spiegazione breve — solo se insolito]\n\n"

    "FORMATO più oggetti (numerati + riepilogo finale):\n"
    "**1. [Nome oggetto 1]**\n"
    "✅ **Dove va:** [destinazione]\n"
    "\n"
    "**2. [Nome oggetto 2]**\n"
    "✅ **Dove va:** [destinazione]\n"
    "\n"
    "⚠️ **Perché:** [motivazione — solo se smaltimento insolito o pericoloso]\n"
    "\n"
    "Totale:\n"
    "[emoji categoria] [n] [categoria]\n"
    "[emoji categoria] [n] [categoria]\n\n"

    "REGOLA TOTALE: Il blocco 'Totale:' va aggiunto SOLO quando ci sono 2 o più oggetti distinti. "
    "Usa queste emoji per categoria: 🟢 vetro | 🟡 plastica | 🔵 carta | 🟤 organico | "
    "🔋 batterie | ⚡ RAEE | ⚫ indifferenziato.\n\n"

    "ESEMPIO risposta (3 oggetti, 2 con smaltimento insolito):\n"
    "**1. Bottiglia di vetro**\n"
    "✅ **Dove va:** Campana verde del vetro\n"
    "🧴 **Parti:** Tappo → bidone giallo.\n"
    "💧 **Prima:** Svuota e sciacqua.\n"
    "\n"
    "**2. Smartphone rotto**\n"
    "✅ **Dove va:** Ecocentro comunale (RAEE)\n"
    "\n"
    "⚠️ **Perché:** Contiene componenti elettronici.\n"
    "\n"
    "**3. Sacchetto di plastica**\n"
    "✅ **Dove va:** Bidone giallo\n"
    "\n"
    "⚠️ **Perché:** È un rifiuto pericoloso.\n"
    "\n"
    "Totale:\n"
    "🟢 1 vetro\n"
    "🟡 2 plastica\n"
    "⚫ 1 indifferenziato\n\n"

    "ESEMPIO (confidenza sotto 90%):\n"
    "📷 **Foto non sufficiente**\n"
    "\n"
    "Puoi inviare una foto più ravvicinata o da un angolo diverso?\n\n"

    "Non aggiungere righe extra. Non salutare. Non ringraziare."
)

VISION_PROMPT = (
    "Analizza questa immagine e rispondi nel formato esatto. "
    "Se la confidenza è sotto il 90%, fai UNA sola domanda chiarificatrice e nient'altro."
)

CLARIFICATION_PROMPT_TEMPLATE = (
    "L'utente ha risposto: \"{answer}\". "
    "Dai la risposta finale nel formato esatto. Nient'altro."
)

# ---------------------------------------------------------------------------
# Food pre-classification
# ---------------------------------------------------------------------------

FOOD_DETECT_SYSTEM = (
    "Sei un analizzatore di immagini per un bot di raccolta differenziata italiano. "
    "Rispondi SOLO con JSON valido, nessun altro testo."
)

FOOD_DETECT_PROMPT = """\
Analizza questa immagine e rispondi con questo JSON esatto (nessun altro testo):
{
  "has_ambiguity": true,
  "food_label": "nome breve del cibo o liquido in italiano (es. Spaghetti, Pizza, Acqua)",
  "food_emoji": "emoji cibo più appropriata",
  "container_label": "nome breve del contenitore in italiano (es. Piatto, Scatola, Bottiglia)",
  "container_emoji": "emoji contenitore più appropriata"
}

Regole per has_ambiguity:
- true SOLO se nell'immagine sono presenti ENTRAMBI: (1) cibo o liquido smaltibile E (2) un contenitore smaltibile,
  E l'utente potrebbe ragionevolmente voler smaltire l'uno, l'altro, o entrambi.
- false se il contenitore è vuoto e pulito (intenzione ovvia: smaltire il contenitore).
- false se c'è solo cibo senza contenitore smaltibile visibile.
- false se l'intenzione è inequivocabile (es. lattina vuota schiacciata, bottiglia sigillata piena d'acqua acquistata).
- food_label e container_label devono essere brevissimi (1-3 parole).
"""

TEXT_SEARCH_SYSTEM = (
    "Sei EcoGlass Bot, assistente per la raccolta differenziata in Italia. "
    "Rispondi SEMPRE e SOLO in italiano.\n\n"

    "L'utente scrive il nome di un oggetto o materiale. "
    "Rispondi con le istruzioni di smaltimento nel formato esatto qui sotto. "
    "Usa le regole del Comune dell'utente se fornite, altrimenti usa le linee guida nazionali.\n\n"

    "FORMATO OBBLIGATORIO (rispetta esattamente spazi e righe vuote):\n"
    "🔍 Oggetto: [nome oggetto normalizzato]\n"
    "\n"
    "✅ Dove si butta: **[categoria destinazione]**\n"
    "\n"
    "ℹ️ Nota: [istruzione breve — max 1 riga]\n"
    "\n"
    "⚠️ Attenzione: [solo se smaltimento è pericoloso o insolito — ometti altrimenti]\n\n"

    "REGOLE:\n"
    "- Se l'oggetto è ambiguo o poco chiaro, rispondi con UNA sola domanda: "
    "'Puoi specificare meglio l'oggetto?'\n"
    "- Se mancano regole comunali, aggiungi SOLO questa riga dopo la Nota: "
    "'_Le regole possono cambiare in base al Comune._'\n"
    "- Non salutare. Non ringraziare. Nessun testo extra.\n"
    "- Massimo 300 caratteri totali.\n\n"

    "DESTINAZIONI NAZIONALI:\n"
    "vetro → **campana verde** | plastica/metalli → **bidone giallo** | "
    "carta/cartone → **bidone blu** | organico/umido → **bidone marrone** | "
    "batterie/pile → **contenitore arancione** (negozi/farmacie) | "
    "RAEE → **ecocentro comunale** | resto → **indifferenziato**"
)

FOOD_CHOICE_PROMPTS = {
    "food": (
        "L'utente vuole smaltire SOLO il cibo/liquido visibile nella foto (non il contenitore). "
        "Fornisci le istruzioni di smaltimento esclusivamente per il cibo o il liquido. "
        "Rispondi nel formato esatto."
    ),
    "container": (
        "L'utente vuole smaltire SOLO il contenitore visibile nella foto (non il cibo dentro). "
        "Fornisci le istruzioni di smaltimento esclusivamente per il contenitore, "
        "assumendo che verrà svuotato e sciacquato prima. "
        "Rispondi nel formato esatto."
    ),
    "both": (
        "L'utente vuole smaltire ENTRAMBI: il cibo/liquido E il contenitore. "
        "Fornisci due blocchi separati nel formato multi-oggetto: "
        "prima il blocco per il cibo/liquido, poi il blocco per il contenitore. "
        "Aggiungi il blocco Totale: alla fine. "
        "Rispondi nel formato esatto."
    ),
}

# ---------------------------------------------------------------------------
# Sync OpenAI helpers  (NEVER call these directly in async handlers —
# always wrap with: await asyncio.wait_for(asyncio.to_thread(fn, ...), timeout=OPENAI_TIMEOUT_SECS)
# ---------------------------------------------------------------------------

def _detect_food_intent_sync(b64_image: str) -> dict:
    """Blocking. Returns food-ambiguity JSON."""
    logger.info("[food-detect] OpenAI request started")
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4.1",
            max_tokens=150,
            timeout=OPENAI_TIMEOUT_SECS,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": FOOD_DETECT_SYSTEM},
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_image}"}},
                        {"type": "text", "text": FOOD_DETECT_PROMPT},
                    ],
                },
            ],
        )
        result = json.loads(response.choices[0].message.content)
        logger.info("[food-detect] OpenAI response received: %s", result)
        return result
    except Exception:
        logger.exception("[food-detect] OpenAI request failed — defaulting to no ambiguity")
        return {"has_ambiguity": False}


def _build_system_prompt(comune: str | None) -> str:
    """Blocking (may fetch Comune rules from OpenAI). Call inside a thread."""
    if not comune:
        return BASE_SYSTEM_PROMPT
    rules = _fetch_comune_rules_sync(comune)
    locale_block = (
        f"\nREGOLE SPECIFICHE DEL COMUNE DI {comune.upper()}:\n"
        f"{rules}\n"
        "Se le regole del Comune differiscono da quelle nazionali, segui SEMPRE quelle del Comune."
    )
    return BASE_SYSTEM_PROMPT + locale_block


def _run_vision_sync(b64_image: str, comune: str | None, prompt: str = VISION_PROMPT) -> str:
    """Blocking: builds system prompt (fetches Comune rules) + calls GPT-4.1 Vision."""
    system_prompt = _build_system_prompt(comune)
    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_image}"}},
                {"type": "text", "text": prompt},
            ],
        },
    ]
    logger.info("[vision] OpenAI request started")
    response = openai_client.chat.completions.create(
        model="gpt-4.1",
        max_tokens=1024,
        timeout=OPENAI_TIMEOUT_SECS,
        messages=messages,
    )
    text = response.choices[0].message.content.strip()
    logger.info("[vision] OpenAI response received (%d chars)", len(text))
    return text


def _run_vision_with_history_sync(messages: list) -> str:
    """Blocking: calls GPT-4.1 with a pre-built message list (for clarification follow-ups)."""
    logger.info("[vision] OpenAI request started (with history)")
    response = openai_client.chat.completions.create(
        model="gpt-4.1",
        max_tokens=1024,
        timeout=OPENAI_TIMEOUT_SECS,
        messages=messages,
    )
    text = response.choices[0].message.content.strip()
    logger.info("[vision] OpenAI response received (%d chars)", len(text))
    return text


# ---------------------------------------------------------------------------
# Async wrappers with event-loop timeout
# ---------------------------------------------------------------------------

async def detect_food_intent(b64_image: str) -> dict:
    return await asyncio.wait_for(
        asyncio.to_thread(_detect_food_intent_sync, b64_image),
        timeout=OPENAI_TIMEOUT_SECS,
    )


async def run_vision(b64_image: str, comune: str | None, prompt: str = VISION_PROMPT) -> str:
    return await asyncio.wait_for(
        asyncio.to_thread(_run_vision_sync, b64_image, comune, prompt),
        timeout=OPENAI_TIMEOUT_SECS,
    )


async def run_vision_with_history(messages: list) -> str:
    return await asyncio.wait_for(
        asyncio.to_thread(_run_vision_with_history_sync, messages),
        timeout=OPENAI_TIMEOUT_SECS,
    )


def _run_text_search_sync(item: str, comune: str | None) -> str:
    """Blocking: text-only waste disposal lookup (no image)."""
    locale_block = ""
    if comune:
        rules = _fetch_comune_rules_sync(comune)
        locale_block = (
            f"\nREGOLE SPECIFICHE DEL COMUNE DI {comune.upper()}:\n"
            f"{rules}\n"
            "Se le regole del Comune differiscono da quelle nazionali, usa quelle del Comune."
        )
    system = TEXT_SEARCH_SYSTEM + locale_block

    logger.info("[text-search] OpenAI request started (item=%r, comune=%s)", item, comune)
    response = openai_client.chat.completions.create(
        model="gpt-4.1",
        max_tokens=300,
        timeout=OPENAI_TIMEOUT_SECS,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": item},
        ],
    )
    text = response.choices[0].message.content.strip()
    logger.info("[text-search] OpenAI response received (%d chars)", len(text))
    return text


async def run_text_search(item: str, comune: str | None) -> str:
    t0 = time.monotonic()
    result = await asyncio.wait_for(
        asyncio.to_thread(_run_text_search_sync, item, comune),
        timeout=OPENAI_TIMEOUT_SECS,
    )
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    waste_db.record_openai_request(elapsed_ms)
    # Auto-save in background — don't block the reply
    asyncio.get_event_loop().run_in_executor(None, _autosave_text_result, item, result)
    return result


def _autosave_text_result(query: str, response: str) -> None:
    """Extract object name from text-search response and persist to DB."""
    if response.strip().endswith("?"):
        return  # clarification question — nothing to save
    match = re.search(r"Oggetto:\s*(.+)", response)
    if not match:
        return
    object_name = match.group(1).strip().rstrip("*").strip()
    if not object_name:
        return
    # Add the raw query as synonym if it differs from the extracted name
    synonyms = []
    if waste_db.normalize_key(query) != waste_db.normalize_key(object_name):
        synonyms = [query]
    waste_db.save_result(
        object_name=object_name,
        disposal_rules=response,
        synonyms=synonyms,
    )


def _autosave_vision_result(response: str, comune: str | None) -> None:
    """Extract single-object name from vision response and persist to DB."""
    if response.strip().endswith("?"):
        return
    # Skip multi-object responses
    if re.search(r"\*\*\d+\.", response):
        return
    # First bold text on the first non-empty line = object name
    match = re.search(r"\*\*([^*\n]+)\*\*", response)
    if not match:
        return
    candidate = match.group(1).strip()
    # Reject if it looks like a field label
    if any(candidate.lower().startswith(k) for k in ("dove va", "parti", "prima", "perché", "foto")):
        return
    waste_db.save_result(
        object_name=candidate,
        disposal_rules=response,
        comune=comune,
    )


# ---------------------------------------------------------------------------
# Photo download
# ---------------------------------------------------------------------------

async def download_photo_as_base64(file_id: str, bot) -> str:
    logger.info("[download] Download started (file_id=%s)", file_id)
    tg_file = await bot.get_file(file_id)
    async with httpx.AsyncClient() as client:
        resp = await client.get(tg_file.file_path)
        resp.raise_for_status()
    size_kb = len(resp.content) / 1024
    logger.info("[download] Download completed (%.1f KB)", size_kb)
    return base64.b64encode(resp.content).decode("utf-8")


# ---------------------------------------------------------------------------
# Comune setup helpers
# ---------------------------------------------------------------------------

ASK_COMUNE_TEXT = (
    "📍 *In quale Comune vivi?*\n\n"
    "Scrivi il nome del tuo Comune per ricevere istruzioni precise "
    "sulla raccolta differenziata locale.\n"
    "_Esempio: Milano, Roma, Napoli, Torino…_"
)


async def ask_for_comune(update: Update) -> None:
    await update.message.reply_text(ASK_COMUNE_TEXT, parse_mode="Markdown")


async def confirm_comune(update: Update, comune: str) -> None:
    await update.message.reply_text(
        f"✅ *Comune salvato: {comune}*\n\n"
        f"Userò le regole di raccolta differenziata di *{comune}* per ogni risposta.\n"
        f"Puoi cambiarlo in qualsiasi momento con /comune.",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# Shared photo-analysis flow  (food check → vision → reply)
# ---------------------------------------------------------------------------

async def _process_photo(
    b64: str,
    comune: str | None,
    user_id: int,
    reply_msg,           # telegram Message to edit
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Run food detection then vision analysis, editing reply_msg with the result.
    Always guarantees a Telegram reply even on errors.
    """
    try:
        # ── Step 1: food ambiguity check ──────────────────────────────────
        logger.info("[photo] Food detection started (user=%s)", user_id)
        food_info = await detect_food_intent(b64)
        logger.info("[photo] Food detection done: has_ambiguity=%s", food_info.get("has_ambiguity"))

        if food_info.get("has_ambiguity"):
            food_label     = food_info.get("food_label", "Avanzi di cibo")
            food_emoji     = food_info.get("food_emoji", "🍽")
            container_label = food_info.get("container_label", "Contenitore")
            container_emoji = food_info.get("container_emoji", "📦")

            context.user_data["pending_food"] = {
                "b64": b64, "comune": comune,
                "food_label": food_label, "food_emoji": food_emoji,
                "container_label": container_label, "container_emoji": container_emoji,
            }

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{food_emoji} {food_label}",        callback_data="food:food")],
                [InlineKeyboardButton(f"{container_emoji} {container_label}", callback_data="food:container")],
                [InlineKeyboardButton("✅ Entrambi",                         callback_data="food:both")],
            ])
            await reply_msg.edit_text(
                "🗑 *Cosa vuoi smaltire?*",
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
            logger.info("[photo] Food ambiguity buttons sent to user=%s", user_id)
            return

        # ── Step 2: standard vision analysis ─────────────────────────────
        t0 = time.monotonic()
        reply_text = await run_vision(b64, comune)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        waste_db.record_openai_request(elapsed_ms)
        logger.info("[photo] Reply ready (user=%s, chars=%d, %dms)", user_id, len(reply_text), elapsed_ms)

        is_question = "?" in reply_text[-120:]
        if is_question:
            context.user_data["pending_clarification"] = {
                "b64": b64,
                "comune": comune,
                "question": reply_text,
            }
        else:
            # Auto-save successful recognition in background
            asyncio.get_event_loop().run_in_executor(
                None, _autosave_vision_result, reply_text, comune
            )

        await reply_msg.edit_text(reply_text, parse_mode="Markdown")
        logger.info("[photo] Reply sent to user=%s", user_id)

    except asyncio.TimeoutError:
        logger.error("[photo] OpenAI timeout (user=%s)", user_id)
        await reply_msg.edit_text(TIMEOUT_REPLY, parse_mode="Markdown")
    except Exception:
        logger.exception("[photo] Unhandled exception (user=%s, comune=%s)", user_id, comune)
        await reply_msg.edit_text(ERROR_REPLY, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    user_id = update.effective_user.id
    comune = get_user_comune(user_id)

    welcome = (
        "♻️ *EcoGlass Bot*\n\n"
        "Sono il tuo assistente per la raccolta differenziata in Italia.\n\n"
        "📸 Inviami la foto di un rifiuto e ti dico dove smaltirlo.\n"
        "📂 Oppure scegli una categoria con i pulsanti qui sotto."
    )
    await update.message.reply_text(
        welcome,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(WASTE_BUTTONS),
    )

    if not comune:
        await ask_for_comune(update)
        context.user_data["awaiting_comune"] = True


async def cmd_comune(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["awaiting_comune"] = True
    context.user_data.pop("pending_clarification", None)
    context.user_data.pop("pending_food", None)
    await ask_for_comune(update)


def _track_user(user: object) -> None:
    """Fire-and-forget user upsert — called via run_in_executor."""
    try:
        waste_db.upsert_user(
            telegram_user_id=user.id,
            username=getattr(user, "username", None),
            first_name=getattr(user, "first_name", None),
            language_code=getattr(user, "language_code", None),
        )
    except Exception:
        logger.exception("[db] _track_user failed")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("pending_clarification", None)
    context.user_data.pop("pending_food", None)

    tg_user = update.effective_user
    user_id = tg_user.id
    asyncio.get_event_loop().run_in_executor(None, _track_user, tg_user)

    comune  = get_user_comune(user_id)
    logger.info("[photo] Photo received (user=%s, comune=%s)", user_id, comune)

    # No Comune yet — save photo and ask first
    if not comune:
        context.user_data["pending_photo_file_id"] = update.message.photo[-1].file_id
        await update.message.reply_text(
            "📍 Prima dimmi: *in quale Comune vivi?*\n"
            "_Esempio: Milano, Roma, Napoli…_",
            parse_mode="Markdown",
        )
        context.user_data["awaiting_comune"] = True
        return

    # Immediate ACK — user sees feedback right away
    reply_msg = await update.message.reply_text(
        "📸 *Foto ricevuta. Analizzo…*",
        parse_mode="Markdown",
    )
    asyncio.get_event_loop().run_in_executor(None, waste_db.record_photo)

    try:
        b64 = await download_photo_as_base64(update.message.photo[-1].file_id, context.bot)
    except Exception:
        logger.exception("[photo] Download failed (user=%s)", user_id)
        await reply_msg.edit_text(ERROR_REPLY, parse_mode="Markdown")
        return

    await _process_photo(b64, comune, user_id, reply_msg, context)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    user_id = tg_user.id
    asyncio.get_event_loop().run_in_executor(None, _track_user, tg_user)
    text    = update.message.text.strip()

    # ── Comune setup flow ─────────────────────────────────────────────────
    if context.user_data.get("awaiting_comune"):
        context.user_data.pop("awaiting_comune", None)
        comune = text
        set_user_comune(user_id, comune)
        await confirm_comune(update, comune)

        pending_file_id = context.user_data.pop("pending_photo_file_id", None)
        if pending_file_id:
            reply_msg = await update.message.reply_text(
                "📸 *Foto ricevuta. Analizzo…*",
                parse_mode="Markdown",
            )
            logger.info("[post-comune] Processing held photo (user=%s, comune=%s)", user_id, comune)
            try:
                b64 = await download_photo_as_base64(pending_file_id, context.bot)
            except Exception:
                logger.exception("[post-comune] Download failed (user=%s)", user_id)
                await reply_msg.edit_text(ERROR_REPLY, parse_mode="Markdown")
                return
            await _process_photo(b64, comune, user_id, reply_msg, context)
        return

    # ── Clarification follow-up flow ──────────────────────────────────────
    pending = context.user_data.get("pending_clarification")
    if pending:
        context.user_data.pop("pending_clarification", None)
        reply_msg = await update.message.reply_text(
            "🔍 *Elaboro la risposta…*",
            parse_mode="Markdown",
        )
        logger.info("[clarify] Follow-up started (user=%s)", user_id)
        try:
            # Rebuild full message list: system + original image + bot question + user answer
            comune = pending.get("comune")
            b64    = pending.get("b64")
            system_prompt = await asyncio.wait_for(
                asyncio.to_thread(_build_system_prompt, comune),
                timeout=OPENAI_TIMEOUT_SECS,
            )
            messages = [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                        {"type": "text", "text": VISION_PROMPT},
                    ],
                },
                {"role": "assistant", "content": pending["question"]},
                {"role": "user", "content": CLARIFICATION_PROMPT_TEMPLATE.format(answer=text)},
            ]
            reply_text = await run_vision_with_history(messages)
            logger.info("[clarify] Reply ready (user=%s, chars=%d)", user_id, len(reply_text))
            await reply_msg.edit_text(reply_text, parse_mode="Markdown")
            logger.info("[clarify] Reply sent to user=%s", user_id)
        except asyncio.TimeoutError:
            logger.error("[clarify] OpenAI timeout (user=%s)", user_id)
            await reply_msg.edit_text(TIMEOUT_REPLY, parse_mode="Markdown")
        except Exception:
            logger.exception("[clarify] Unhandled exception (user=%s)", user_id)
            await reply_msg.edit_text(ERROR_REPLY, parse_mode="Markdown")
        return

    # ── Default: treat as waste item text search ──────────────────────────
    comune = get_user_comune(user_id)
    logger.info("[text-search] Query=%r (user=%s, comune=%s)", text, user_id, comune)
    asyncio.get_event_loop().run_in_executor(None, waste_db.record_text_search)

    # 1. Check local DB first
    cached = await asyncio.to_thread(waste_db.lookup, text, comune)
    if cached:
        waste_db.record_cache_hit()
        logger.info("[text-search] Cache hit for %r", text)
        await update.message.reply_text(cached, parse_mode="Markdown")
        return

    # 2. Cache miss — ask OpenAI
    logger.info("[text-search] Cache miss — calling OpenAI for %r", text)
    thinking_msg = await update.message.reply_text(
        "🔍 *Cerco informazioni…*",
        parse_mode="Markdown",
    )
    try:
        reply_text = await run_text_search(text, comune)
        await thinking_msg.edit_text(reply_text, parse_mode="Markdown")
        logger.info("[text-search] OpenAI reply sent to user=%s", user_id)
    except asyncio.TimeoutError:
        logger.error("[text-search] OpenAI timeout (user=%s)", user_id)
        await thinking_msg.edit_text(TIMEOUT_REPLY, parse_mode="Markdown")
    except Exception:
        logger.exception("[text-search] Unhandled exception (user=%s)", user_id)
        await thinking_msg.edit_text(ERROR_REPLY, parse_mode="Markdown")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Hidden admin command — /stats"""
    s = await asyncio.to_thread(waste_db.get_stats)
    avg_s = s["avg_response_ms"] / 1000 if s["avg_response_ms"] else 0
    text = (
        "📊 *Statistiche EcoGlass Bot*\n\n"
        f"👥 Utenti totali: *{s['total_users']}*\n"
        f"🆕 Nuovi utenti oggi: *{s['new_today']}*\n"
        f"🟢 Utenti attivi (24h): *{s['active_24h']}*\n"
        f"📷 Foto elaborate: *{s['photos_processed']}*\n"
        f"💬 Ricerche testo: *{s['text_searches']}*\n"
        f"⚡ Cache hits: *{s['cache_hits']}*\n"
        f"🤖 Richieste OpenAI oggi: *{s['openai_today']}*\n"
        f"📅 Richieste OpenAI totali: *{s['openai_total']}*\n"
        f"💰 Risparmiato stimato: *${s['money_saved_usd']:.2f}*\n"
        f"⏱ Tempo medio risposta: *{avg_s:.1f}s*"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data    = query.data
    user_id = query.from_user.id

    # ── Food choice buttons ───────────────────────────────────────────────
    if data.startswith("food:"):
        choice  = data.split(":", 1)[1]
        pending = context.user_data.get("pending_food")
        if not pending:
            logger.warning("[food-cb] No pending_food (user=%s)", user_id)
            await query.edit_message_text("⚠️ Sessione scaduta. Invia di nuovo la foto.")
            return

        b64    = pending["b64"]
        comune = pending["comune"]
        logger.info("[food-cb] Choice='%s' (user=%s, comune=%s)", choice, user_id, comune)
        await query.edit_message_text("🔍 *Elaboro…*", parse_mode="Markdown")

        try:
            logger.info("[food-cb] OpenAI request started (choice=%s)", choice)
            reply_text = await run_vision(b64, comune, FOOD_CHOICE_PROMPTS[choice])
            logger.info("[food-cb] OpenAI response received (user=%s, chars=%d)", user_id, len(reply_text))
            context.user_data.pop("pending_food", None)
            await query.edit_message_text(reply_text, parse_mode="Markdown")
            logger.info("[food-cb] Reply sent to user=%s", user_id)
        except asyncio.TimeoutError:
            logger.error("[food-cb] OpenAI timeout (user=%s)", user_id)
            await query.edit_message_text(TIMEOUT_REPLY, parse_mode="Markdown")
        except Exception:
            logger.exception("[food-cb] Unhandled exception (user=%s)", user_id)
            await query.edit_message_text(ERROR_REPLY, parse_mode="Markdown")
        return

    # ── Standard waste category buttons ──────────────────────────────────
    info = WASTE_INFO.get(data)
    if info:
        await query.edit_message_text(
            info,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("↩️ Altra categoria", callback_data="menu")]]
            ),
        )
    elif data == "menu":
        await query.edit_message_text(
            "Seleziona la categoria del rifiuto:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(WASTE_BUTTONS),
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not tg_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN non trovato nelle variabili d'ambiente.")
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY non trovato nelle variabili d'ambiente.")

    # Init local waste DB and print startup stats
    waste_db.init_db()
    s = waste_db.get_stats()
    logger.info(
        "[db] Startup — oggetti: %d | cache hits: %d | OpenAI totali: %d | risparmiato: $%.2f",
        s["objects"], s["cache_hits"], s["openai_total"], s["money_saved_usd"],
    )

    app = Application.builder().token(tg_token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("comune", cmd_comune))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("EcoGlass Bot avviato (asyncio-safe, GPT-4.1 Vision, food pre-classification).")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
