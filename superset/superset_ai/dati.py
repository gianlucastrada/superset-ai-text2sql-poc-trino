"""Accesso al database dei dati: esecuzione delle query e descrizione dello schema per il modello."""

from __future__ import annotations

import datetime as dt
import decimal
import fnmatch
import threading
import time
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from . import settings

_engines: dict[str, Engine] = {}
_lock = threading.Lock()
# Cache della descrizione dello schema, una per dialetto (cambiano solo i nomi dei tipi)
_schema_cache: dict[str, dict[str, Any]] = {}
_dove: dict[str, list[str]] = {}  # colonna -> tabelle/viste che la contengono

# OID dei tipi Postgres, per capire se una colonna e' numerica o temporale
_OID_NUMERICI = {20, 21, 23, 26, 700, 701, 790, 1700}
_OID_TEMPORALI = {1082, 1114, 1184}

# Motori con cui l'AI puo' interrogare i dati. La descrizione dello schema (commenti, valori,
# relazioni) si legge sempre dal catalogo Postgres: Trino qui sta davanti allo stesso database.
MOTORI: dict[str, dict[str, str]] = {
    "postgres": {
        "etichetta": "PostgreSQL (diretto)",
        "dialetto": "postgres",
        "database": settings.AI_DATABASE_NAME,
    },
    "trino": {
        "etichetta": "Trino (catalogo postgresql)",
        "dialetto": "trino",
        "database": settings.AI_TRINO_DATABASE_NAME,
    },
}


def _uri(motore: str) -> str:
    return settings.AI_TRINO_URI if motore == "trino" else settings.AI_DATA_URI


def motori_disponibili() -> list[str]:
    return [m for m in MOTORI if _uri(m)]


def motore_valido(motore: str | None) -> str:
    """Motore richiesto se configurato, altrimenti PostgreSQL."""
    return motore if motore in motori_disponibili() else "postgres"


def dialetto(motore: str) -> str:
    return MOTORI[motore_valido(motore)]["dialetto"]


def engine(motore: str = "postgres") -> Engine:
    motore = motore_valido(motore)
    if motore not in _engines:
        with _lock:
            if motore not in _engines:
                uri = _uri(motore)
                if not uri:
                    raise RuntimeError("Variabile AI_DATA_URI non impostata.")
                if motore == "trino":
                    _engines[motore] = create_engine(
                        uri,
                        pool_pre_ping=True,
                        connect_args={
                            "session_properties": {"query_max_run_time": settings.AI_TRINO_TIMEOUT}
                        },
                    )
                else:
                    _engines[motore] = create_engine(
                        uri, pool_pre_ping=True, pool_size=3, max_overflow=2
                    )
    return _engines[motore]


def _json(v: Any) -> Any:
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (dt.date, dt.datetime, dt.time)):
        return v.isoformat()
    if isinstance(v, dt.timedelta):
        return str(v)
    if isinstance(v, (bytes, memoryview)):
        return "<binario>"
    return v


_TRINO_NUMERICI = ("tinyint", "smallint", "integer", "bigint", "real", "double", "decimal")


def _tipo(type_code: Any) -> str:
    """Tipo semplificato di una colonna del risultato: numerico, data o testo.
    psycopg2 restituisce l'OID del tipo (intero), il client Trino il nome (es. 'decimal(12,2)')."""
    if isinstance(type_code, str):
        t = type_code.lower()
        if t.startswith(_TRINO_NUMERICI):
            return "numerico"
        if t.startswith(("date", "timestamp")):
            return "data"
        return "testo"
    if type_code in _OID_NUMERICI:
        return "numerico"
    if type_code in _OID_TEMPORALI:
        return "data"
    return "testo"


def _esegui(
    sql: str, params: tuple | None = None, motore: str = "postgres", timeout_ms: int = 0
) -> tuple[list, list]:
    """Esegue una query e restituisce (descrizione colonne, righe).

    PostgreSQL: transazione di sola lettura, sempre annullata alla fine.
    Trino: niente transazione; la sola lettura e' garantita dall'utente Postgres ai_reader
    usato dal catalogo, il tempo massimo da query_max_run_time."""
    motore = motore_valido(motore)
    raw = engine(motore).raw_connection()
    try:
        cur = raw.cursor()
        if motore == "postgres":
            cur.execute("SET TRANSACTION READ ONLY")
            if timeout_ms:
                cur.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
        if params is None:
            cur.execute(sql)  # niente parametri: psycopg2 non interpreta i '%'
        else:
            cur.execute(sql, params)
        if motore == "trino":
            # il client Trino compila description solo dopo aver letto i risultati
            righe = cur.fetchall()
        else:
            righe = cur.fetchall() if cur.description else []
        descr = list(cur.description or [])
        return descr, righe
    finally:
        try:
            if motore == "postgres":
                raw.rollback()
        finally:
            raw.close()


def esegui_query(sql: str, limite: int | None = None, motore: str = "postgres") -> dict[str, Any]:
    """Esegue una SELECT (gia' validata) e restituisce colonne, tipi e righe (max `limite`)."""
    limite = limite or settings.MAX_PREVIEW_ROWS
    avvolta = f"SELECT * FROM (\n{sql}\n) AS ai_q LIMIT {int(limite) + 1}"
    inizio = time.monotonic()
    descr, righe = _esegui(avvolta, motore=motore)
    ms = int((time.monotonic() - inizio) * 1000)
    troncato = len(righe) > limite
    righe = righe[:limite]
    return {
        "colonne": [d[0] for d in descr],
        "tipi": [_tipo(d[1]) for d in descr],
        "righe": [[_json(v) for v in r] for r in righe],
        "troncato": troncato,
        "ms": ms,
    }


_SQL_COLONNE = """
SELECT c.table_name, t.table_type, c.column_name, c.data_type,
       col_description((quote_ident(c.table_schema) || '.' || quote_ident(c.table_name))::regclass,
                       c.ordinal_position) AS commento_colonna,
       obj_description((quote_ident(c.table_schema) || '.' || quote_ident(c.table_name))::regclass,
                       'pg_class') AS commento_tabella
FROM information_schema.columns c
JOIN information_schema.tables t
  ON t.table_schema = c.table_schema AND t.table_name = c.table_name
WHERE c.table_schema = %s
ORDER BY t.table_type, c.table_name, c.ordinal_position
"""

_SQL_RELAZIONI = """
SELECT con.conrelid::regclass::text, a.attname, con.confrelid::regclass::text, af.attname
FROM pg_constraint con
JOIN pg_attribute a  ON a.attrelid = con.conrelid  AND a.attnum = con.conkey[1]
JOIN pg_attribute af ON af.attrelid = con.confrelid AND af.attnum = con.confkey[1]
WHERE con.contype = 'f' AND con.connamespace = %s::regnamespace
ORDER BY 1, 2
"""

_SQL_RIGHE = """
SELECT c.relname, c.reltuples::bigint
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = %s AND c.relkind = 'r'
"""


def _inizio_mese(d: dt.date, mesi_indietro: int) -> dt.date:
    """Primo giorno del mese spostato di N mesi indietro (N negativo = in avanti)."""
    totale = d.year * 12 + (d.month - 1) - mesi_indietro
    return dt.date(totale // 12, totale % 12 + 1, 1)


def periodi_riferimento(ultima: dt.date, colonna: str) -> list[str]:
    """Periodi relativi gia' calcolati: il modello li copia come costanti invece di
    ricavarli con date_trunc (dove tende a scegliere interpretazioni diverse)."""
    inizio_trim = dt.date(ultima.year, 3 * ((ultima.month - 1) // 3) + 1, 1)
    fine_trim = _inizio_mese(inizio_trim, -3) - dt.timedelta(days=1)
    if ultima == fine_trim:
        # i dati arrivano all'ultimo giorno del trimestre: e' gia' completo
        inizio_trim_prec, fine_trim_prec = inizio_trim, fine_trim
    else:
        inizio_trim_prec = _inizio_mese(inizio_trim, 3)
        fine_trim_prec = inizio_trim - dt.timedelta(days=1)

    def p(nome: str, da: dt.date, a: dt.date) -> str:
        return f"  - {nome}: dal DATE '{da.isoformat()}' al DATE '{a.isoformat()}'"

    return [
        f"PERIODI DI RIFERIMENTO (ultima data presente nei dati: '{ultima.isoformat()}', colonna {colonna}).",
        "Per i periodi relativi usa ESATTAMENTE queste date come costanti di tipo data, es. BETWEEN DATE 'aaaa-mm-gg' AND DATE 'aaaa-mm-gg':",
        p("ultimo mese", _inizio_mese(ultima, 0), ultima),
        p("ultimo trimestre / ultimi 3 mesi", _inizio_mese(ultima, 2), ultima),
        p("ultimo semestre / ultimi 6 mesi", _inizio_mese(ultima, 5), ultima),
        p("ultimo anno / ultimi 12 mesi", _inizio_mese(ultima, 11), ultima),
        p("anno in corso (quest'anno)", dt.date(ultima.year, 1, 1), ultima),
        p("anno precedente", dt.date(ultima.year - 1, 1, 1), dt.date(ultima.year - 1, 12, 31)),
        p("trimestre solare in corso (solo se richiesto esplicitamente)", inizio_trim, ultima),
        p("ultimo trimestre solare completo (solo se richiesto esplicitamente)", inizio_trim_prec, fine_trim_prec),
    ]


def _qi(nome: str) -> str:
    return '"' + nome.replace('"', '""') + '"'


# Nomi dei tipi Postgres come li vede Trino (il modello deve ragionare con i tipi del dialetto)
_TIPI_TRINO = {
    "text": "varchar",
    "character varying": "varchar",
    "character": "char",
    "numeric": "decimal",
    "double precision": "double",
    "real": "real",
    "timestamp without time zone": "timestamp",
    "timestamp with time zone": "timestamp with time zone",
    "boolean": "boolean",
    "integer": "integer",
    "bigint": "bigint",
    "smallint": "smallint",
    "date": "date",
}


def _tipo_mostrato(tipo_c: str, dial: str) -> str:
    return _TIPI_TRINO.get(tipo_c, tipo_c) if dial == "trino" else tipo_c


# Colonne con valori unici per definizione: chiave primaria o indice unique su una sola colonna
_SQL_CHIAVI_UNICHE = """
SELECT t.relname, a.attname
FROM pg_index i
JOIN pg_class t      ON t.oid = i.indrelid
JOIN pg_namespace n  ON n.oid = t.relnamespace
JOIN pg_attribute a  ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0]
WHERE i.indisunique AND i.indnatts = 1 AND n.nspname = %s
"""

# Catalogo completo, anche degli oggetti nascosti al modello: (tabella, colonna) in minuscolo ->
# nomi reali. Serve a validare i nomi prima di usarli in una query di controllo.
_catalogo: dict[tuple[str, str], tuple[str, str]] = {}
_chiavi_uniche: set[tuple[str, str]] = set()
_unicita: dict[tuple[str, str], bool | None] = {}


def _oggetto_mostrato(nome: str) -> bool:
    """Filtro degli oggetti da descrivere al modello (AI_OGGETTI_INCLUSI / AI_OGGETTI_ESCLUSI)."""
    n = nome.lower()
    if settings.AI_OGGETTI_INCLUSI and not any(
        fnmatch.fnmatchcase(n, p.lower()) for p in settings.AI_OGGETTI_INCLUSI
    ):
        return False
    return not any(fnmatch.fnmatchcase(n, p.lower()) for p in settings.AI_OGGETTI_ESCLUSI)


def colonna_unica(tabella: str, colonna: str) -> bool | None:
    """True se la colonna ha un valore diverso per ogni riga della tabella/vista, False se no,
    None se non si sa. Vale per qualsiasi schema: prima le chiavi del catalogo; per viste e
    tabelle senza chiave si conta sui dati, entro AI_UNICITA_TIMEOUT_MS."""
    if not _catalogo:
        descrivi_schema()
    chiave = (tabella.lower(), colonna.lower())
    reale = _catalogo.get(chiave)
    if reale is None:
        return None
    if chiave in _chiavi_uniche:
        return True
    if chiave not in _unicita:
        esito = None
        if settings.AI_UNICITA_TIMEOUT_MS > 0:
            t, c = reale
            try:
                _, r = _esegui(
                    f"SELECT COUNT({_qi(c)}) - COUNT(DISTINCT {_qi(c)}) "
                    f"FROM {_qi(settings.AI_SCHEMA)}.{_qi(t)}",
                    timeout_ms=settings.AI_UNICITA_TIMEOUT_MS,
                )
                esito = r[0][0] == 0
            except Exception:  # noqa: BLE001 - tempo scaduto o errore: non si sa
                esito = None
        _unicita[chiave] = esito
    return _unicita[chiave]


def _ultima_data_configurata() -> tuple[dt.date, str] | None:
    """Data piu' recente della colonna indicata in AI_COLONNA_DATA ("tabella.colonna")."""
    if "." not in settings.AI_COLONNA_DATA:
        return None
    t, c = settings.AI_COLONNA_DATA.rsplit(".", 1)
    reale = _catalogo.get((t.split(".")[-1].lower(), c.lower()))
    if reale is None:
        return None
    try:
        _, mm = _esegui(f"SELECT max({_qi(reale[1])}) FROM {_qi(settings.AI_SCHEMA)}.{_qi(reale[0])}")
    except Exception:  # noqa: BLE001
        return None
    massimo = mm[0][0] if mm else None
    if isinstance(massimo, dt.datetime):
        massimo = massimo.date()
    return (massimo, f"{reale[0]}.{reale[1]}") if isinstance(massimo, dt.date) else None


def descrivi_schema(forza: bool = False, motore: str = "postgres") -> str:
    """Testo compatto che descrive tabelle, colonne, commenti, valori tipici e relazioni.
    Letto dal catalogo Postgres; per Trino cambiano solo i nomi dei tipi."""
    dial = dialetto(motore)
    adesso = time.time()
    cache = _schema_cache.get(dial)
    if not forza and cache and adesso - cache["quando"] < settings.SCHEMA_CACHE_SECONDS:
        return cache["testo"]

    schema = settings.AI_SCHEMA
    _, colonne = _esegui(_SQL_COLONNE, (schema,))
    _, relazioni = _esegui(_SQL_RELAZIONI, (schema,))
    _, chiavi = _esegui(_SQL_CHIAVI_UNICHE, (schema,))
    _catalogo.clear()
    _catalogo.update({(r[0].lower(), r[2].lower()): (r[0], r[2]) for r in colonne})
    _chiavi_uniche.clear()
    _chiavi_uniche.update((t.lower(), c.lower()) for t, c in chiavi)
    _unicita.clear()
    ultima_configurata = _ultima_data_configurata()

    # Oggetti descritti al modello: filtri per nome, poi eventualmente solo le viste
    # (con solo le viste, valori tipici e intervalli di date si leggono dalle viste)
    colonne = [r for r in colonne if _oggetto_mostrato(r[0])]
    solo_viste = settings.AI_SOLO_VISTE and any(r[1] == "VIEW" for r in colonne)
    if solo_viste:
        colonne = [r for r in colonne if r[1] == "VIEW"]
    _, conteggi = _esegui(_SQL_RIGHE, (schema,))
    righe_per_tabella = {nome: n for nome, n in conteggi}

    tabelle: dict[str, dict[str, Any]] = {}
    dove: dict[str, list[str]] = {}  # colonna -> tabelle/viste che la contengono
    for tabella, tipo_t, colonna, tipo_c, comm_col, comm_tab in colonne:
        t = tabelle.setdefault(
            tabella, {"vista": tipo_t == "VIEW", "commento": comm_tab, "colonne": []}
        )
        t["colonne"].append((colonna, tipo_c, comm_col))
        dove.setdefault(colonna.lower(), []).append(f"{schema}.{tabella}")

    righe_testo: list[str] = []
    ultima: tuple[dt.date, str] | None = None  # data piu' recente nei dati e colonna di provenienza
    for tabella, info in tabelle.items():
        intest = ("VISTA " if info["vista"] else "TABELLA ") + f"{schema}.{tabella}"
        if not info["vista"] and righe_per_tabella.get(tabella, -1) >= 0:
            intest += f" (circa {righe_per_tabella[tabella]} righe)"
        if info["commento"]:
            intest += f" -- {info['commento']}"
        righe_testo.append(intest)
        for colonna, tipo_c, comm_col in info["colonne"]:
            riga = f"  - {colonna} {_tipo_mostrato(tipo_c, dial)}"
            if comm_col:
                riga += f" -- {comm_col}"
            # Valori tipici per colonne di testo con pochi valori distinti (solo tabelle)
            if (solo_viste or not info["vista"]) and tipo_c in ("text", "character varying", "character"):
                try:
                    _, valori = _esegui(
                        f"SELECT DISTINCT {_qi(colonna)} FROM {_qi(schema)}.{_qi(tabella)} "
                        f"WHERE {_qi(colonna)} IS NOT NULL LIMIT 21"
                    )
                    if 0 < len(valori) <= 20:
                        riga += " [valori: " + ", ".join(sorted(str(v[0]) for v in valori)) + "]"
                except Exception:  # noqa: BLE001
                    pass
            # Intervallo per le date (solo tabelle)
            if (solo_viste or not info["vista"]) and tipo_c in ("date", "timestamp without time zone", "timestamp with time zone"):
                try:
                    _, mm = _esegui(
                        f"SELECT min({_qi(colonna)}), max({_qi(colonna)}) FROM {_qi(schema)}.{_qi(tabella)}"
                    )
                    if mm and mm[0][0] is not None:
                        riga += f" [dal {_json(mm[0][0])} al {_json(mm[0][1])}]"
                        massimo = mm[0][1]
                        if isinstance(massimo, dt.datetime):
                            massimo = massimo.date()
                        if ultima is None or massimo > ultima[0]:
                            ultima = (massimo, f"{tabella}.{colonna}")
                except Exception:  # noqa: BLE001
                    pass
            righe_testo.append(riga)
        righe_testo.append("")

    # Chiavi esterne solo tra tabelle mostrate (con solo_viste nessuna: le viste non ne hanno)
    mostrate = set(tabelle)
    relazioni = [r for r in relazioni if r[0].split(".")[-1] in mostrate and r[2].split(".")[-1] in mostrate]
    if relazioni:
        righe_testo.append("RELAZIONI (chiavi esterne):")
        for da_t, da_c, a_t, a_c in relazioni:
            righe_testo.append(f"  - {da_t}.{da_c} -> {a_t}.{a_c}")

    if ultima_configurata:
        ultima = ultima_configurata
    if ultima:
        righe_testo.append("")
        righe_testo.extend(periodi_riferimento(*ultima))

    testo = "\n".join(righe_testo).strip()
    _schema_cache[dial] = {"testo": testo, "quando": adesso}
    _dove.clear()
    _dove.update(dove)
    return testo


def verifica_trino() -> str:
    """Controlla che Trino veda le stesse tabelle/viste e colonne del catalogo Postgres.
    Una colonna che manca di solito e' un tipo che il catalogo di Trino non sa mappare
    (es. numeric senza precisione, se manca decimal-mapping)."""
    if "trino" not in motori_disponibili():
        return "Trino non configurato."
    if not _catalogo:
        descrivi_schema()
    _, col = _esegui(
        "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = "
        + "'" + settings.AI_SCHEMA.replace("'", "''") + "'",
        motore="trino",
    )
    visti = {(t.lower(), c.lower()) for t, c in col}
    oggetti = sorted({t for t, _ in visti})
    testo = f"Trino raggiungibile: {len(oggetti)} tabelle/viste ({', '.join(oggetti)})."
    assenti = sorted({t for t, _ in _catalogo} - set(oggetti))
    if assenti:
        testo += f" ATTENZIONE: non visibili da Trino: {', '.join(assenti)}."
    mancanti = sorted(f"{t}.{c}" for t, c in _catalogo if t in oggetti and (t, c) not in visti)
    if mancanti:
        altre = f" e altre {len(mancanti) - 10}" if len(mancanti) > 10 else ""
        testo += (
            f" ATTENZIONE: colonne non visibili da Trino: {', '.join(mancanti[:10])}{altre} "
            "(tipo non mappato: controlla decimal-mapping nel catalogo postgresql di Trino)."
        )
    return testo


def dove_si_trova(colonna: str) -> list[str]:
    """Tabelle/viste dello schema che contengono la colonna (per aiutare il modello a correggersi)."""
    if not _dove:
        descrivi_schema()
    return list(_dove.get(colonna.lower(), []))
