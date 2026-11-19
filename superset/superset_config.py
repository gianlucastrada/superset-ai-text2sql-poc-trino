"""Configurazione di Superset per la POC Text-to-SQL.

Valori sensibili e indirizzi arrivano da variabili d'ambiente (Secret e Deployment in k8s/).
"""

import os

from superset_ai import ai_bp, app_mutator

# Chiave per cifrare sessioni e password delle connessioni: deve restare stabile nel tempo.
SECRET_KEY = os.environ["SUPERSET_SECRET_KEY"]

# Metadati di Superset (utenti, dashboard, grafici...) nel database "superset" di Postgres
SQLALCHEMY_DATABASE_URI = os.environ["SUPERSET_DATABASE_URI"]

# Estensione AI: pagina /ai/, API e pannello per SQL Lab
BLUEPRINTS = [ai_bp]
FLASK_APP_MUTATOR = app_mutator

# SQL Lab: sincrono; asincrono sui database con "esecuzione asincrona" se gira il worker
# Celery (deploy.ps1 -Asincrono, vedi in fondo)
SQLLAB_TIMEOUT = 60
SUPERSET_WEBSERVER_TIMEOUT = 120

# Il validatore SQL di Postgres richiede il programma 'ecpg', assente nell'immagine:
# senza questa riga SQL Lab segnerebbe un errore (falso) su ogni query.
SQL_VALIDATORS_BY_ENGINE: dict = {}

# --- Cache e code -----------------------------------------------------------------------
# Con Redis (k8s/40-redis.yaml, REDIS_HOST impostata da deploy.ps1) la cache e' condivisa tra
# i processi e i pod di Superset e sopravvive ai riavvii di Superset. Senza, cache in memoria.
REDIS_HOST = os.environ.get("REDIS_HOST", "")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))

if REDIS_HOST:
    from cachelib.redis import RedisCache

    def _cache_redis(db: int, prefisso: str, durata: int) -> dict:
        return {
            "CACHE_TYPE": "RedisCache",
            "CACHE_DEFAULT_TIMEOUT": durata,
            "CACHE_KEY_PREFIX": prefisso,
            "CACHE_REDIS_HOST": REDIS_HOST,
            "CACHE_REDIS_PORT": REDIS_PORT,
            "CACHE_REDIS_DB": db,
        }

    # Metadati (elenco tabelle, schemi...), risultati dei grafici, filtri e stato di Explore
    CACHE_CONFIG = _cache_redis(1, "superset_meta_", 300)
    DATA_CACHE_CONFIG = _cache_redis(2, "superset_dati_", int(os.environ.get("SUPERSET_CACHE_DATI_SECONDI", "3600")))
    FILTER_STATE_CACHE_CONFIG = _cache_redis(3, "superset_filtri_", 86400)
    EXPLORE_FORM_DATA_CACHE_CONFIG = _cache_redis(3, "superset_explore_", 86400)

    # Risultati delle query asincrone di SQL Lab e coda del worker Celery
    RESULTS_BACKEND = RedisCache(host=REDIS_HOST, port=REDIS_PORT, db=4, key_prefix="superset_risultati_")

    class CeleryConfig:  # noqa: D101 - nomi richiesti da Celery
        broker_url = f"redis://{REDIS_HOST}:{REDIS_PORT}/0"
        result_backend = f"redis://{REDIS_HOST}:{REDIS_PORT}/0"
        imports = ("superset.sql_lab", "superset.tasks.cache")
        worker_prefetch_multiplier = 1
        task_acks_late = False

    CELERY_CONFIG = CeleryConfig
else:
    CACHE_CONFIG = {"CACHE_TYPE": "SimpleCache", "CACHE_DEFAULT_TIMEOUT": 300}
    DATA_CACHE_CONFIG = CACHE_CONFIG

# Accesso solo locale via port-forward (HTTP): cookie non "secure"
SESSION_COOKIE_SECURE = False

FEATURE_FLAGS = {
    "DASHBOARD_RBAC": False,
}
