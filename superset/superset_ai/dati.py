"""Accesso al database dei dati: esecuzione delle query e descrizione dello schema per il modello.

Funziona con qualsiasi motore che SQLAlchemy (e quindi Superset) sa collegare:
- lo schema (tabelle, viste, colonne, commenti, chiavi, relazioni) si legge con l'Inspector
  di SQLAlchemy, lo stesso meccanismo che usa Superset;
- il dialetto SQL si ricava dall'indirizzo di connessione (vedi dialetti.py);
- le query di servizio (valori tipici, date, unicita') si scrivono una volta e sqlglot le
  traduce nel dialetto del motore; leggono al massimo AI_CAMPIONE_RIGHE righe, cosi' il costo
  resta limitato anche su tabelle molto grandi.

Motori:
- "diretto": la connessione AI_DATA_URI (PostgreSQL, Oracle, SQL Server, MySQL, Hive...);
- "trino": facoltativo, AI_TRINO_URI. Con AI_METADATI_DA=diretto lo schema di Trino si legge
  dalla connessione diretta (utile quando Trino non espone commenti, viste o chiavi della sorgente).
"""

from __future__ import annotations

import datetime as dt
import decimal
import fnmatch
import logging
import threading
import time
from typing import Any

import sqlglot
from sqlalchemy import create_engine, inspect, types as satypes
from sqlalchemy.engine import Engine, make_url

from . import dialetti, settings

logger = logging.getLogger(__name__)

_engines: dict[str, Engine] = {}
_lock = threading.Lock()
_metadati_cache: dict[str, dict[str, Any]] = {}  # motore -> catalogo letto con l'Inspector
_schema_cache: dict[str, dict[str, Any]] = {}  # motore -> testo dello schema per il modello
_unicita: dict[tuple[str, str, str], bool | None] = {}
_dove: dict[str, list[str]] = {}  # colonna -> tabelle/viste mostrate che la contengono

# Il motore principale si chiamava "postgres": il vecchio nome resta valido
_ALIAS = {"postgres": "diretto"}

# OID dei tipi PostgreSQL (psycopg2): ripiego quando una colonna del risultato e' tutta NULL
_OID_NUMERICI = {20, 21, 23, 26, 700, 701, 790, 1700}
_OID_TEMPORALI = {1082, 1114, 1184}
_NOMI_NUMERICI = ("tinyint", "smallint", "integer", "int", "bigint", "real", "double", "decimal", "number", "numeric", "float")


# --------------------------------------------------------------------------- motori

def _uri(motore: str) -> str:
    return settings.AI_TRINO_URI if motore == "trino" else settings.AI_DATA_URI


def motori_disponibili() -> list[str]:
    return [m for m in ("diretto", "trino") if _uri(m)]


def motore_valido(motore: str | None) -> str:
    """Motore richiesto se configurato, altrimenti la connessione diretta."""
    motore = _ALIAS.get(motore or "", motore)
    return motore if motore in motori_disponibili() else "diretto"


def dialetto(motore: str) -> str:
    """Dialetto sqlglot del motore ("postgres", "trino", "tsql", "oracle", "hive"...)."""
    motore = motore_valido(motore)
    if motore == "diretto" and settings.AI_DIALETTO:
        return settings.AI_DIALETTO
    try:
        return dialetti.da_backend(make_url(_uri(motore)).get_backend_name())
    except Exception:  # noqa: BLE001 - indirizzo mancante o non valido
        return ""


def etichetta(motore: str) -> str:
    motore = motore_valido(motore)
    if motore == "trino":
        return "Trino"
    return f"{dialetti.nome(dialetto(motore))} (diretto)"


def nome_database(motore: str) -> str:
    """Nome con cui il database e' registrato in Superset (dataset e SQL Lab)."""
    return settings.AI_TRINO_DATABASE_NAME if motore_valido(motore) == "trino" else settings.AI_DATABASE_NAME


def motore_metadati(motore: str) -> str:
    """Motore da cui si legge lo schema per `motore` (AI_METADATI_DA)."""
    motore = motore_valido(motore)
    if motore != "diretto" and settings.AI_METADATI_DA == "diretto":
        return "diretto"
    return motore


def engine(motore: str = "diretto") -> Engine:
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
                    try:
                        _engines[motore] = create_engine(uri, pool_pre_ping=True, pool_size=3, max_overflow=2)
                    except TypeError:  # pool senza dimensione (es. SQLite)
                        _engines[motore] = create_engine(uri, pool_pre_ping=True)
    return _engines[motore]


def verifica_connessione(motore: str = "diretto") -> None:
    """Solleva un'eccezione se il motore non risponde."""
    with engine(motore).connect():
        pass


def schema_effettivo(motore: str = "diretto") -> str:
    """Schema descritto al modello: AI_SCHEMA oppure lo schema predefinito della connessione."""
    return _metadati(motore_metadati(motore)).get("schema") or ""


# --------------------------------------------------------------------------- esecuzione

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


def _tipo(valori: list, type_code: Any) -> str:
    """Tipo semplificato di una colonna del risultato: numerico, data o testo.
    Si guarda il valore restituito (vale per ogni driver); se e' sempre NULL, il codice del tipo."""
    for v in valori:
        if v is None:
            continue
        if isinstance(v, bool):
            return "testo"
        if isinstance(v, (int, float, decimal.Decimal)):
            return "numerico"
        if isinstance(v, (dt.date, dt.datetime)):
            return "data"
        return "testo"
    if isinstance(type_code, str):
        t = type_code.lower()
        if t.startswith(_NOMI_NUMERICI):
            return "numerico"
        if t.startswith(("date", "timestamp")):
            return "data"
    if type_code in _OID_NUMERICI:
        return "numerico"
    if type_code in _OID_TEMPORALI:
        return "data"
    return "testo"


def _esegui(sql: str, motore: str = "diretto", timeout_ms: int = 0, max_righe: int = 0) -> tuple[list, list]:
    """Esegue una query e restituisce (descrizione colonne, righe).

    PostgreSQL: transazione di sola lettura (annullata alla fine) e tempo massimo.
    MySQL: tempo massimo per la sessione. Gli altri motori: la sola lettura e' garantita
    dall'utente del database (solo permessi di lettura) e dai controlli di sqlguard."""
    motore = motore_valido(motore)
    dial = dialetto(motore)
    raw = engine(motore).raw_connection()
    try:
        cur = raw.cursor()
        if dial == "postgres":
            cur.execute("SET TRANSACTION READ ONLY")
            if timeout_ms:
                cur.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
        elif dial == "mysql" and timeout_ms:
            cur.execute(f"SET SESSION MAX_EXECUTION_TIME = {int(timeout_ms)}")
        cur.execute(sql)  # niente parametri: alcuni driver interpreterebbero i '%'
        if cur.description is None and motore != "trino":
            righe = []
        else:
            righe = cur.fetchmany(max_righe) if max_righe else cur.fetchall()
        descr = list(cur.description or [])  # Trino la compila solo dopo la lettura
        if dial == "mysql" and timeout_ms:
            cur.execute("SET SESSION MAX_EXECUTION_TIME = 0")
        return descr, righe
    finally:
        try:
            raw.rollback()
        except Exception:  # noqa: BLE001 - motori senza transazioni
            pass
        raw.close()


_SEGNAPOSTO = "SELECT ai_segnaposto FROM ai_segnaposto"


def _traduci(sql_postgres: str, dial: str) -> str:
    """Query di servizio scritta in PostgreSQL, tradotta nel dialetto del motore."""
    if not dial or dial == "postgres":
        return sql_postgres
    return sqlglot.transpile(sql_postgres, read="postgres", write=dial)[0]


def _avvolgi(sql: str, limite: int, dial: str) -> str:
    """SELECT * FROM (<sql>) LIMIT n, nella sintassi del motore (LIMIT, TOP, FETCH FIRST...)."""
    try:
        modello = _traduci(f"SELECT * FROM ({_SEGNAPOSTO}) AS ai_q LIMIT {int(limite)}", dial)
        if f"({_SEGNAPOSTO})" in modello:
            return modello.replace(f"({_SEGNAPOSTO})", "(\n" + sql + "\n)", 1)
    except Exception:  # noqa: BLE001
        pass
    return sql  # senza involucro: il limite lo applica comunque la lettura (fetchmany)


def esegui_query(sql: str, limite: int | None = None, motore: str = "diretto") -> dict[str, Any]:
    """Esegue una SELECT (gia' validata) e restituisce colonne, tipi e righe (max `limite`)."""
    limite = limite or settings.MAX_PREVIEW_ROWS
    avvolta = _avvolgi(sql, int(limite) + 1, dialetto(motore))
    inizio = time.monotonic()
    descr, righe = _esegui(avvolta, motore=motore, max_righe=int(limite) + 1)
    ms = int((time.monotonic() - inizio) * 1000)
    troncato = len(righe) > limite
    righe = [list(r) for r in righe[:limite]]
    return {
        "colonne": [d[0] for d in descr],
        "tipi": [_tipo([r[i] for r in righe], d[1]) for i, d in enumerate(descr)],
        "righe": [[_json(v) for v in r] for r in righe],
        "troncato": troncato,
        "ms": ms,
    }


# --------------------------------------------------------------------------- catalogo

def _qi(nome: str) -> str:
    """Identificatore tra doppi apici (sintassi PostgreSQL, poi tradotta da sqlglot)."""
    return '"' + nome.replace('"', '""') + '"'


def _qualificato(schema: str, nome: str) -> str:
    return f"{_qi(schema)}.{_qi(nome)}" if schema else _qi(nome)


def _prova(funzione, *args, predefinito=None, **kwargs):
    """Chiama una funzione dell'Inspector: non tutti i motori le implementano tutte."""
    try:
        return funzione(*args, **kwargs)
    except Exception:  # noqa: BLE001 - NotImplementedError, permessi, sintassi del motore...
        return predefinito


def _tipo_testo(tipo: Any, dialect: Any) -> str:
    try:
        return str(tipo.compile(dialect=dialect))
    except Exception:  # noqa: BLE001
        try:
            return str(tipo)
        except Exception:  # noqa: BLE001
            return type(tipo).__name__


def _categoria(tipo: Any) -> str:
    """testo, data o altro: decide quali controlli sui dati hanno senso per la colonna."""
    if isinstance(tipo, (satypes.Date, satypes.DateTime)):
        return "data"
    if isinstance(tipo, (satypes.String, satypes.Enum)):
        return "testo"
    nome = type(tipo).__name__.lower()
    if "date" in nome or "timestamp" in nome:
        return "data"
    if "char" in nome or "text" in nome or "string" in nome:
        return "testo"
    return "altro"


def _metadati(motore: str, forza: bool = False) -> dict[str, Any]:
    """Catalogo del motore letto con l'Inspector di SQLAlchemy (in cache come lo schema)."""
    adesso = time.time()
    cache = _metadati_cache.get(motore)
    if not forza and cache and adesso - cache["quando"] < settings.SCHEMA_CACHE_SECONDS:
        return cache
    eng = engine(motore)
    insp = inspect(eng)
    schema = settings.AI_SCHEMA or _prova(lambda: insp.default_schema_name) or ""
    sch = schema or None
    tabelle = _prova(insp.get_table_names, schema=sch, predefinito=[]) or []
    viste = set(_prova(insp.get_view_names, schema=sch, predefinito=[]) or [])
    oggetti: dict[str, dict[str, Any]] = {}
    for nome in list(tabelle) + sorted(v for v in viste if v not in tabelle):
        colonne = _prova(insp.get_columns, nome, schema=sch)
        if not colonne:
            continue
        commento = (_prova(insp.get_table_comment, nome, schema=sch) or {}).get("text")
        uniche = set()
        pk = (_prova(insp.get_pk_constraint, nome, schema=sch) or {}).get("constrained_columns") or []
        if len(pk) == 1:
            uniche.add(pk[0].lower())
        for u in _prova(insp.get_unique_constraints, nome, schema=sch, predefinito=[]) or []:
            if len(u.get("column_names") or []) == 1:
                uniche.add(u["column_names"][0].lower())
        for i in _prova(insp.get_indexes, nome, schema=sch, predefinito=[]) or []:
            if i.get("unique") and len(i.get("column_names") or []) == 1 and i["column_names"][0]:
                uniche.add(i["column_names"][0].lower())
        relazioni = []
        for fk in _prova(insp.get_foreign_keys, nome, schema=sch, predefinito=[]) or []:
            if len(fk.get("constrained_columns") or []) == 1 and fk.get("referred_table"):
                relazioni.append((fk["constrained_columns"][0], fk["referred_table"], fk["referred_columns"][0]))
        oggetti[nome] = {
            "vista": nome in viste,
            "commento": commento,
            "colonne": [
                {
                    "nome": c["name"],
                    "tipo": _tipo_testo(c["type"], eng.dialect),
                    "categoria": _categoria(c["type"]),
                    "commento": c.get("comment"),
                }
                for c in colonne
            ],
            "uniche": uniche,
            "relazioni": relazioni,
        }
    meta = {"schema": schema, "dialetto": dialetto(motore), "oggetti": oggetti, "quando": adesso}
    _metadati_cache[motore] = meta
    for chiave in [k for k in _unicita if k[0] == motore]:
        del _unicita[chiave]
    return meta


def _trova(meta: dict[str, Any], tabella: str, colonna: str | None = None):
    """(nome reale dell'oggetto, nome reale della colonna) senza badare a maiuscole/minuscole."""
    for nome, obj in meta["oggetti"].items():
        if nome.lower() == tabella.lower():
            if colonna is None:
                return nome, None
            for c in obj["colonne"]:
                if c["nome"].lower() == colonna.lower():
                    return nome, c["nome"]
            return None
    return None


def _oggetto_mostrato(nome: str) -> bool:
    """Filtro degli oggetti da descrivere al modello (AI_OGGETTI_INCLUSI / AI_OGGETTI_ESCLUSI)."""
    n = nome.lower()
    if settings.AI_OGGETTI_INCLUSI and not any(
        fnmatch.fnmatchcase(n, p.lower()) for p in settings.AI_OGGETTI_INCLUSI
    ):
        return False
    return not any(fnmatch.fnmatchcase(n, p.lower()) for p in settings.AI_OGGETTI_ESCLUSI)


def _timeout_ms() -> int:
    return settings.AI_UNICITA_TIMEOUT_MS if settings.AI_UNICITA_TIMEOUT_MS > 0 else 5000


def colonna_unica(tabella: str, colonna: str, motore: str = "diretto") -> bool | None:
    """True se la colonna ha un valore diverso per ogni riga della tabella/vista, False se no,
    None se non si sa. Vale per ogni motore: prima chiavi primarie, vincoli e indici unique
    del catalogo; poi, per viste e tabelle senza chiave, un controllo su un campione di
    AI_CAMPIONE_RIGHE righe (un doppione trovato basta a dire "non unica")."""
    mm = motore_metadati(motore)
    meta = _metadati(mm)
    trovata = _trova(meta, tabella, colonna)
    if not trovata:
        return None
    nome, col = trovata
    if col.lower() in meta["oggetti"][nome]["uniche"]:
        return True
    chiave = (mm, nome.lower(), col.lower())
    if chiave not in _unicita:
        esito = None
        if settings.AI_UNICITA_TIMEOUT_MS > 0:
            n_max = settings.AI_CAMPIONE_RIGHE
            sql = _traduci(
                f"SELECT COUNT(*) AS n, COUNT(DISTINCT {_qi(col)}) AS d FROM (SELECT {_qi(col)} "
                f"FROM {_qualificato(meta['schema'], nome)} WHERE {_qi(col)} IS NOT NULL "
                f"LIMIT {n_max}) AS ai_u",
                meta["dialetto"],
            )
            try:
                _, r = _esegui(sql, motore=mm, timeout_ms=settings.AI_UNICITA_TIMEOUT_MS)
                n, d = int(r[0][0] or 0), int(r[0][1] or 0)
                if d < n:
                    esito = False  # doppioni nel campione
                elif n < n_max:
                    esito = True  # campione = tabella intera, nessun doppione
            except Exception:  # noqa: BLE001 - tempo scaduto o errore: non si sa
                esito = None
        _unicita[chiave] = esito
    return _unicita[chiave]


def _valori_tipici(meta: dict[str, Any], motore: str, nome: str, col: str) -> list[str] | None:
    """Fino a 20 valori distinti di una colonna di testo, letti da un campione di righe."""
    sql = _traduci(
        f"SELECT DISTINCT {_qi(col)} FROM (SELECT {_qi(col)} FROM {_qualificato(meta['schema'], nome)} "
        f"WHERE {_qi(col)} IS NOT NULL LIMIT {settings.AI_CAMPIONE_RIGHE}) AS ai_v LIMIT 21",
        meta["dialetto"],
    )
    try:
        _, valori = _esegui(sql, motore=motore, timeout_ms=_timeout_ms())
    except Exception:  # noqa: BLE001
        return None
    if 0 < len(valori) <= 20:
        return sorted(str(v[0]) for v in valori)
    return None


def _intervallo_date(meta: dict[str, Any], motore: str, nome: str, col: str):
    sql = _traduci(
        f"SELECT MIN({_qi(col)}), MAX({_qi(col)}) FROM {_qualificato(meta['schema'], nome)}",
        meta["dialetto"],
    )
    try:
        _, mm = _esegui(sql, motore=motore, timeout_ms=_timeout_ms())
    except Exception:  # noqa: BLE001
        return None
    if not mm or mm[0][0] is None:
        return None
    return mm[0][0], mm[0][1]


def _come_data(v: Any) -> dt.date | None:
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    if isinstance(v, str):  # motori che restituiscono le date come testo (es. SQLite)
        try:
            return dt.date.fromisoformat(v[:10])
        except ValueError:
            return None
    return None


def _ultima_data_configurata(meta: dict[str, Any], motore: str) -> tuple[dt.date, str] | None:
    """Data piu' recente della colonna indicata in AI_COLONNA_DATA ("tabella.colonna")."""
    if "." not in settings.AI_COLONNA_DATA:
        return None
    t, c = settings.AI_COLONNA_DATA.rsplit(".", 1)
    trovata = _trova(meta, t.split(".")[-1], c)
    if not trovata:
        return None
    intervallo = _intervallo_date(meta, motore, *trovata)
    massimo = _come_data(intervallo[1]) if intervallo else None
    return (massimo, f"{trovata[0]}.{trovata[1]}") if massimo else None


# --------------------------------------------------------------------------- schema per il modello

def _inizio_mese(d: dt.date, mesi_indietro: int) -> dt.date:
    """Primo giorno del mese spostato di N mesi indietro (N negativo = in avanti)."""
    totale = d.year * 12 + (d.month - 1) - mesi_indietro
    return dt.date(totale // 12, totale % 12 + 1, 1)


def periodi_riferimento(ultima: dt.date, colonna: str, dial: str = "postgres") -> list[str]:
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

    def lett(d: dt.date | str) -> str:
        return dialetti.data_letterale(d if isinstance(d, str) else d.isoformat(), dial)

    def p(nome: str, da: dt.date, a: dt.date) -> str:
        return f"  - {nome}: dal {lett(da)} al {lett(a)}"

    return [
        f"PERIODI DI RIFERIMENTO (ultima data presente nei dati: '{ultima.isoformat()}', colonna {colonna}).",
        "Per i periodi relativi usa ESATTAMENTE queste date come costanti di tipo data, es. "
        f"BETWEEN {lett('aaaa-mm-gg')} AND {lett('aaaa-mm-gg')}:",
        p("ultimo mese", _inizio_mese(ultima, 0), ultima),
        p("ultimo trimestre / ultimi 3 mesi", _inizio_mese(ultima, 2), ultima),
        p("ultimo semestre / ultimi 6 mesi", _inizio_mese(ultima, 5), ultima),
        p("ultimo anno / ultimi 12 mesi", _inizio_mese(ultima, 11), ultima),
        p("anno in corso (quest'anno)", dt.date(ultima.year, 1, 1), ultima),
        p("anno precedente", dt.date(ultima.year - 1, 1, 1), dt.date(ultima.year - 1, 12, 31)),
        p("trimestre solare in corso (solo se richiesto esplicitamente)", inizio_trim, ultima),
        p("ultimo trimestre solare completo (solo se richiesto esplicitamente)", inizio_trim_prec, fine_trim_prec),
    ]


def _tipo_nel_dialetto(tipo: str, da: str, a: str) -> str:
    """Nome del tipo come lo scriverebbe il motore che esegue le query (es. TEXT -> VARCHAR)."""
    if da == a or not a:
        return tipo
    try:
        return sqlglot.exp.DataType.build(tipo, dialect=da or None).sql(dialect=a)
    except Exception:  # noqa: BLE001
        return tipo


def descrivi_schema(forza: bool = False, motore: str = "diretto") -> str:
    """Testo compatto che descrive tabelle, viste, colonne, commenti, valori tipici e relazioni."""
    motore = motore_valido(motore)
    adesso = time.time()
    cache = _schema_cache.get(motore)
    if not forza and cache and adesso - cache["quando"] < settings.SCHEMA_CACHE_SECONDS:
        return cache["testo"]

    mm = motore_metadati(motore)
    meta = _metadati(mm, forza=forza)
    dial = dialetto(motore)
    schema = meta["schema"]
    oggetti = {n: o for n, o in meta["oggetti"].items() if _oggetto_mostrato(n)}
    if settings.AI_SOLO_VISTE and any(o["vista"] for o in oggetti.values()):
        oggetti = {n: o for n, o in oggetti.items() if o["vista"]}

    righe_testo: list[str] = []
    dove: dict[str, list[str]] = {}
    ultima: tuple[dt.date, str] | None = None
    for nome, obj in oggetti.items():
        qualificato = f"{schema}.{nome}" if schema else nome
        intest = ("VISTA " if obj["vista"] else "TABELLA ") + qualificato
        if obj["commento"]:
            intest += f" -- {obj['commento']}"
        righe_testo.append(intest)
        for c in obj["colonne"]:
            dove.setdefault(c["nome"].lower(), []).append(qualificato)
            riga = f"  - {c['nome']} {_tipo_nel_dialetto(c['tipo'], meta['dialetto'], dial)}"
            if c["commento"]:
                riga += f" -- {c['commento']}"
            if settings.AI_PROFILO_DATI and c["categoria"] == "testo":
                valori = _valori_tipici(meta, mm, nome, c["nome"])
                if valori:
                    riga += " [valori: " + ", ".join(valori) + "]"
            if settings.AI_PROFILO_DATI and c["categoria"] == "data":
                intervallo = _intervallo_date(meta, mm, nome, c["nome"])
                if intervallo:
                    riga += f" [dal {_json(intervallo[0])} al {_json(intervallo[1])}]"
                    massimo = _come_data(intervallo[1])
                    if massimo and (ultima is None or massimo > ultima[0]):
                        ultima = (massimo, f"{nome}.{c['nome']}")
            righe_testo.append(riga)
        righe_testo.append("")

    relazioni = [
        (nome, col, rif_t, rif_c)
        for nome, obj in oggetti.items()
        for col, rif_t, rif_c in obj["relazioni"]
        if rif_t in oggetti
    ]
    if relazioni:
        righe_testo.append("RELAZIONI (chiavi esterne):")
        for da_t, da_c, a_t, a_c in relazioni:
            righe_testo.append(f"  - {da_t}.{da_c} -> {a_t}.{a_c}")

    ultima = _ultima_data_configurata(meta, mm) or ultima
    if ultima:
        righe_testo.append("")
        righe_testo.extend(periodi_riferimento(*ultima, dial))

    testo = "\n".join(righe_testo).strip()
    _schema_cache[motore] = {"testo": testo, "quando": adesso}
    _dove.clear()
    _dove.update(dove)
    return testo


def verifica_trino() -> str:
    """Controlla che Trino veda le stesse tabelle/viste e colonne del catalogo da cui si legge
    lo schema. Una colonna che manca di solito e' un tipo che il catalogo di Trino non sa
    mappare (es. numeric senza precisione, se manca decimal-mapping)."""
    if "trino" not in motori_disponibili():
        return "Trino non configurato."
    insp = inspect(engine("trino"))
    sch = settings.AI_SCHEMA or _prova(lambda: insp.default_schema_name) or None
    visti = {
        (t.lower(), c["name"].lower())
        for t in (_prova(insp.get_table_names, schema=sch, predefinito=[]) or [])
        for c in (_prova(insp.get_columns, t, schema=sch, predefinito=[]) or [])
    }
    oggetti = sorted({t for t, _ in visti})
    testo = f"Trino raggiungibile: {len(oggetti)} tabelle/viste ({', '.join(oggetti)})."
    if motore_metadati("trino") == "trino":
        return testo
    catalogo = {
        (n.lower(), c["nome"].lower()) for n, o in _metadati("diretto")["oggetti"].items() for c in o["colonne"]
    }
    assenti = sorted({t for t, _ in catalogo} - set(oggetti))
    if assenti:
        testo += f" ATTENZIONE: non visibili da Trino: {', '.join(assenti)}."
    mancanti = sorted(f"{t}.{c}" for t, c in catalogo if t in oggetti and (t, c) not in visti)
    if mancanti:
        altre = f" e altre {len(mancanti) - 10}" if len(mancanti) > 10 else ""
        testo += (
            f" ATTENZIONE: colonne non visibili da Trino: {', '.join(mancanti[:10])}{altre} "
            "(tipo non mappato: controlla decimal-mapping nel catalogo di Trino)."
        )
    return testo


def dove_si_trova(colonna: str) -> list[str]:
    """Tabelle/viste mostrate che contengono la colonna (per aiutare il modello a correggersi)."""
    if not _dove:
        descrivi_schema()
    return list(_dove.get(colonna.lower(), []))
