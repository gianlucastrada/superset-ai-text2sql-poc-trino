"""Impostazioni lette dalle variabili d'ambiente (vedi k8s/20-superset.yaml)."""

import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "si")


def _lista(name: str) -> list[str]:
    return [v.strip() for v in os.environ.get(name, "").split(",") if v.strip()]


# Ollama sulla DGX Spark (connessione diretta, porta 11434)
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://10.100.208.51:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3.6:35b")
OLLAMA_TIMEOUT = _int("OLLAMA_TIMEOUT", 240)
# 0 = usa il contesto predefinito del server. Cambiarlo puo' forzare Ollama a ricaricare
# il modello, con impatto sugli altri utenti della Spark: lasciare 0 se non serve.
OLLAMA_NUM_CTX = _int("OLLAMA_NUM_CTX", 0)
# qwen3.6 e' un modello "thinking": il ragionamento esteso rallenta molto le risposte.
OLLAMA_THINK = os.environ.get("OLLAMA_THINK", "false").lower() in ("1", "true", "yes")

# Database dei dati (utente in sola lettura) e nome con cui e' registrato in Superset
AI_DATA_URI = os.environ.get("AI_DATA_URI", "")
AI_DATABASE_NAME = os.environ.get("AI_DATABASE_NAME", "Vendite (POC)")
AI_SCHEMA = os.environ.get("AI_SCHEMA", "public")

# --- Adattamento al database ---------------------------------------------------------------
# Il codice non conosce nessuna tabella: cio' che riguarda un database sta nei commenti del
# database stesso (tabelle, viste, colonne) e in config/<database>/ (letto da deploy.ps1).
#
# Oggetti mostrati al modello. AI_SOLO_VISTE=true: solo le viste, se ce ne sono (utile se il
# database ha viste di analisi gia' pronte). AI_OGGETTI_INCLUSI / AI_OGGETTI_ESCLUSI: elenchi
# separati da virgola, con caratteri jolly (es. "v_*" oppure "tmp_*,log_*").
AI_SOLO_VISTE = _bool("AI_SOLO_VISTE", False)
AI_OGGETTI_INCLUSI = _lista("AI_OGGETTI_INCLUSI")
AI_OGGETTI_ESCLUSI = _lista("AI_OGGETTI_ESCLUSI")
# Colonna che fissa "oggi" per i periodi relativi (es. "ordini.data_ordine"), anche se nascosta
# al modello. Vuota = la data piu' recente tra le colonne data mostrate (sbagliata se ci sono
# date future, come scadenze o consegne previste).
AI_COLONNA_DATA = os.environ.get("AI_COLONNA_DATA", "").strip()
# Regole di business di QUESTO database, in linguaggio naturale, aggiunte al prompt
# (config/<database>/istruzioni.md). Vuoto = nessuna.
AI_ISTRUZIONI = os.environ.get("AI_ISTRUZIONI", "").strip()
# Controllo delle JOIN che moltiplicano le righe: se una colonna di JOIN non e' chiave primaria
# o unique (es. nelle viste) si verifica sui dati, con questo tempo massimo in millisecondi.
# 0 = solo le chiavi del catalogo, nessuna query sui dati (per database molto grandi).
AI_UNICITA_TIMEOUT_MS = _int("AI_UNICITA_TIMEOUT_MS", 3000)
# Esempi mostrati nella pagina AI (una riga per esempio; config/<database>/esempi-*.txt).
# Vuoti = nessun pulsante di esempio.
AI_ESEMPI_DOMANDE = [r.strip() for r in os.environ.get("AI_ESEMPI_DOMANDE", "").splitlines() if r.strip()]
AI_ESEMPI_DASHBOARD = [r.strip() for r in os.environ.get("AI_ESEMPI_DASHBOARD", "").splitlines() if r.strip()]

# Trino davanti allo stesso Postgres (facoltativo: se AI_TRINO_URI e' vuota il motore non compare).
# L'URI fissa catalogo e schema di sessione, es. trino://ai@trino:8080/postgresql/public
AI_TRINO_URI = os.environ.get("AI_TRINO_URI", "")
AI_TRINO_DATABASE_NAME = os.environ.get("AI_TRINO_DATABASE_NAME", "Vendite via Trino")
# Tempo massimo di una query su Trino (proprieta' di sessione query_max_run_time)
AI_TRINO_TIMEOUT = os.environ.get("AI_TRINO_TIMEOUT", "60s")

# Limiti
MAX_PREVIEW_ROWS = _int("AI_MAX_PREVIEW_ROWS", 200)
MAX_CHARTS = _int("AI_MAX_CHARTS", 6)
SCHEMA_CACHE_SECONDS = _int("AI_SCHEMA_CACHE_SECONDS", 600)
