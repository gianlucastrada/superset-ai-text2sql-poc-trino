"""Registro delle richieste all'AI: domanda, SQL, esito, tempi e voto dell'utente.

Sta nella tabella "ai_registro" del database dei metadati di Superset (la stessa connessione
di dashboard e utenti), creata al primo uso. Serve a tre cose:
- capire come viene usata l'AI e dove sbaglia (esiti, errori, tempi, motore, modello);
- raccogliere il giudizio degli utenti (voto +1 / -1 con una nota facoltativa);
- avere in futuro una raccolta di domande -> SQL verificate da riusare come esempi.

Scrivere nel registro non deve mai far fallire una richiesta: gli errori finiscono nel log.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from typing import Any, Callable

from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, Text, desc, func, select, update
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

_meta = MetaData()
TABELLA = Table(
    "ai_registro",
    _meta,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("quando", DateTime, nullable=False, index=True),  # UTC
    Column("utente", String(255)),
    Column("tipo", String(20)),  # sql, riesegui, dashboard, prova
    Column("motore", String(30)),
    Column("modello", String(100)),
    Column("domanda", Text),
    Column("sql", Text),
    Column("spiegazione", Text),
    Column("esito", String(20)),  # ok, avviso, errore
    Column("errore", Text),
    Column("righe", Integer),
    Column("tentativi", Integer),
    Column("durata_ms", Integer),
    Column("voto", Integer),  # +1, -1 oppure NULL
    Column("nota", Text),
)


def _engine_superset() -> Engine:
    from superset.extensions import db

    return db.engine


# Sostituibile nei test e negli script fuori da Superset
fornitore_engine: Callable[[], Engine] = _engine_superset
_pronto: set[int] = set()
_lock = threading.Lock()


def _engine() -> Engine:
    eng = fornitore_engine()
    if id(eng) not in _pronto:
        with _lock:
            if id(eng) not in _pronto:
                _meta.create_all(eng, tables=[TABELLA], checkfirst=True)
                _pronto.add(id(eng))
    return eng


def _taglia(v: Any, n: int) -> Any:
    return v[:n] if isinstance(v, str) else v


def scrivi(**campi: Any) -> int | None:
    """Aggiunge una riga e ne restituisce l'id (None se il registro non e' disponibile)."""
    valori = {k: v for k, v in campi.items() if k in TABELLA.c and k != "id"}
    valori["quando"] = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    for chiave, lunghezza in (("utente", 255), ("tipo", 20), ("motore", 30), ("modello", 100), ("esito", 20)):
        valori[chiave] = _taglia(valori.get(chiave), lunghezza)
    try:
        with _engine().begin() as conn:
            risultato = conn.execute(TABELLA.insert().values(**valori))
            return int(risultato.inserted_primary_key[0])
    except Exception:  # noqa: BLE001
        logger.warning("Registro AI non disponibile", exc_info=True)
        return None


def vota(id_riga: int, voto: int, nota: str | None, utente: str | None, admin: bool = False) -> bool:
    """Registra il voto di un utente su una sua richiesta (un amministratore su tutte)."""
    voto = 1 if voto > 0 else -1
    condizione = TABELLA.c.id == int(id_riga)
    if not admin:
        condizione = condizione & (TABELLA.c.utente == utente)
    with _engine().begin() as conn:
        risultato = conn.execute(update(TABELLA).where(condizione).values(voto=voto, nota=_taglia(nota, 2000)))
        return bool(risultato.rowcount)


def elenco(limite: int = 100, utente: str | None = None, tipo: str | None = None) -> list[dict[str, Any]]:
    """Ultime richieste, piu' recenti per prime (solo di `utente`, se indicato)."""
    query = select(TABELLA).order_by(desc(TABELLA.c.id)).limit(max(1, min(int(limite), 5000)))
    if utente:
        query = query.where(TABELLA.c.utente == utente)
    if tipo:
        query = query.where(TABELLA.c.tipo == tipo)
    with _engine().connect() as conn:
        righe = conn.execute(query).mappings().all()
    return [
        {k: (v.isoformat() + "Z" if isinstance(v, dt.datetime) else v) for k, v in dict(r).items()}
        for r in righe
    ]


def riepilogo(utente: str | None = None) -> dict[str, Any]:
    """Conteggi per esito e voti, e tempo medio, per la testata della pagina del registro."""
    filtro = TABELLA.c.utente == utente if utente else None
    with _engine().connect() as conn:
        def conta(*condizioni) -> int:
            q = select(func.count()).select_from(TABELLA)
            for c in [x for x in (filtro, *condizioni) if x is not None]:
                q = q.where(c)
            return int(conn.execute(q).scalar() or 0)

        q_tempo = select(func.avg(TABELLA.c.durata_ms))
        if filtro is not None:
            q_tempo = q_tempo.where(filtro)
        tempo = conn.execute(q_tempo).scalar()
        return {
            "totale": conta(),
            "ok": conta(TABELLA.c.esito == "ok"),
            "avviso": conta(TABELLA.c.esito == "avviso"),
            "errore": conta(TABELLA.c.esito == "errore"),
            "voti_positivi": conta(TABELLA.c.voto > 0),
            "voti_negativi": conta(TABELLA.c.voto < 0),
            "durata_media_ms": int(tempo) if tempo is not None else None,
        }
