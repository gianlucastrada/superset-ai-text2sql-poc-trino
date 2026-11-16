"""Controlli sulla SQL generata dal modello: una sola istruzione, solo lettura.

E' il primo livello di protezione. Il secondo (quello che conta davvero) e' l'utente
Postgres "ai_reader", che ha solo permessi SELECT: lo usa anche il catalogo postgresql di Trino.
Per Trino si segnalano anche i costrutti PostgreSQL che non esistono nel suo dialetto.
"""

import re

import sqlglot
from sqlglot import exp


class SqlNonValida(ValueError):
    """La SQL non e' accettabile (vuota, piu' istruzioni o non di sola lettura)."""


# Nodi vietati ovunque nell'albero della query (alcune classi cambiano nome tra le
# versioni di sqlglot: prendo solo quelle esistenti).
_VIETATI = tuple(
    cls
    for cls in (
        getattr(exp, name, None)
        for name in (
            "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "AlterTable",
            "TruncateTable", "Command", "Into", "Lock", "Grant", "Copy", "Set",
        )
    )
    if isinstance(cls, type)
)

_LETTURA = tuple(
    cls
    for cls in (getattr(exp, n, None) for n in ("Select", "SetOperation", "Union", "Intersect", "Except"))
    if isinstance(cls, type)
)

_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


def pulisci(sql: str) -> str:
    """Toglie blocchi ``` e punti e virgola finali."""
    sql = (sql or "").strip()
    sql = _FENCE.sub("", sql).strip()
    while sql.endswith(";"):
        sql = sql[:-1].rstrip()
    return sql


_VISTE_PER_RIGA = {"v_vendite", "v_vendite_tutte", "righe_ordine"}


def _conta_righe_per_ordini(radice) -> str | None:
    """COUNT(*) su una vista con una riga per riga d'ordine conta le righe, non gli ordini."""
    if not any(t.name.lower() in _VISTE_PER_RIGA for t in radice.find_all(exp.Table)):
        return None
    for c in radice.find_all(exp.Count):
        if isinstance(c.this, exp.Star):
            return (
                "COUNT(*) su una vista/tabella con una riga per riga d'ordine conta le RIGHE, non gli "
                "ordini (un ordine ha piu' righe): i numeri risultano gonfiati. Per contare gli ordini "
                "usa la vista con una riga per ordine (v_ordini) oppure COUNT(DISTINCT ordine_id)."
            )
    return None


def controlla_logica(sql: str, dialetto: str = "postgres") -> str | None:
    """Cerca errori logici tipici dei modelli. Restituisce un messaggio o None.

    Caso principale: la query finale raggruppa per colonne che poi NON restituisce
    (es. AVG(importo) ... GROUP BY ordine_id). Produce tante righe senza etichetta:
    quasi sempre il modello voleva un'aggregazione in due passaggi.
    """
    try:
        radice = sqlglot.parse_one(sql, read=dialetto)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(radice, exp.Select):
        return None
    conta_righe = _conta_righe_per_ordini(radice)
    if conta_righe:
        return conta_righe
    gruppo = radice.args.get("group")
    if not gruppo or not gruppo.expressions:
        return None
    uscite_nomi = {p.alias_or_name.lower() for p in radice.expressions}
    uscite_expr = {p.unalias().sql(dialect=dialetto).lower() for p in radice.expressions}
    mancanti = []
    for g in gruppo.expressions:
        if isinstance(g, exp.Literal):  # GROUP BY 1, 2 ...: si riferisce a colonne restituite
            continue
        testo = g.sql(dialect=dialetto).lower()
        nome = g.alias_or_name.lower() if isinstance(g, exp.Column) else testo
        if testo not in uscite_expr and nome not in uscite_nomi:
            mancanti.append(g.sql(dialect=dialetto))
    if mancanti and len(mancanti) == len(gruppo.expressions):
        colonne = ", ".join(mancanti)
        return (
            f"La query raggruppa per {colonne} ma non restituisce {colonne}: produce una riga "
            "per ogni gruppo senza etichetta. Se la domanda chiede un unico valore (es. una media "
            f"per {colonne}), calcola prima il totale per {colonne} in una sottoquery e poi "
            "applica AVG/MAX/... sul risultato della sottoquery, restituendo una sola riga. "
            f"Altrimenti aggiungi {colonne} alle colonne restituite."
        )
    return None


_STRINGHE = re.compile(r"'(?:[^']|'')*'")
_DATA_NUDA = re.compile(r"(?<![A-Za-z_])'\d{4}-\d{2}-\d{2}'")
# Una data tra apici e' un errore solo se e' confrontata direttamente (>=, =, BETWEEN ... AND ...):
# DATE '...', TIMESTAMP '...' e CAST('...' AS DATE) sono validi in Trino.
_CONFRONTO_PRIMA = re.compile(r"(?i)(<=|>=|<>|!=|=|<|>|\bbetween|\band)\s*$")
_CONFRONTO_DOPO = re.compile(r"^\s*(<=|>=|<>|!=|=|<|>)")


def _errori_trino(sql: str) -> str | None:
    """Costrutti PostgreSQL che Trino non accetta: si segnalano al modello con la correzione
    giusta prima di eseguire (l'errore di Trino sarebbe meno chiaro)."""
    senza_stringhe = _STRINGHE.sub("''", sql)
    problemi = []
    if "::" in senza_stringhe:
        problemi.append("il cast con '::' non esiste in Trino: usa CAST(espressione AS tipo)")
    if re.search(r"(?i)\bilike\b", senza_stringhe):
        problemi.append("ILIKE non esiste in Trino: usa lower(colonna) LIKE lower('%testo%')")
    if re.search(r"(?i)\binterval\s*'\s*\d+\s+[a-z]+", sql):
        problemi.append("in Trino gli intervalli si scrivono INTERVAL '3' MONTH (numero tra apici, unita' fuori)")
    for m in _DATA_NUDA.finditer(sql):
        if _CONFRONTO_PRIMA.search(sql[: m.start()]) or _CONFRONTO_DOPO.match(sql[m.end():]):
            problemi.append(
                f"la data {m.group(0)} e' una stringa: in Trino una colonna date si confronta solo "
                f"con un valore date, scrivi DATE {m.group(0)}"
            )
            break
    if not problemi:
        return None
    return "La query usa sintassi PostgreSQL non valida in Trino: " + "; ".join(problemi) + "."


def valida(sql: str, dialetto: str = "postgres") -> str:
    """Restituisce la SQL pulita, oppure solleva SqlNonValida."""
    sql = pulisci(sql)
    if not sql:
        raise SqlNonValida("La query e' vuota.")
    if dialetto == "trino" and (errore := _errori_trino(sql)):
        raise SqlNonValida(errore)

    try:
        istruzioni = [s for s in sqlglot.parse(sql, read=dialetto) if s is not None]
    except Exception:  # noqa: BLE001 - sqlglot non riconosce tutta la sintassi del dialetto
        # Ripiego prudente: accetto solo testo che inizia con SELECT/WITH e senza ';'.
        # Le scritture sarebbero comunque rifiutate dai permessi dell'utente ai_reader.
        if ";" in sql or not re.match(r"^\s*(select|with)\b", sql, re.IGNORECASE):
            raise SqlNonValida("Query non riconosciuta come SELECT di sola lettura.")
        return sql

    if len(istruzioni) != 1:
        raise SqlNonValida("E' ammessa una sola istruzione SQL.")
    radice = istruzioni[0]
    if not isinstance(radice, _LETTURA):
        raise SqlNonValida("Sono ammesse solo query SELECT (anche con WITH).")
    for nodo in radice.walk():
        nodo = nodo[0] if isinstance(nodo, tuple) else nodo
        if isinstance(nodo, _VIETATI):
            raise SqlNonValida(
                f"La query contiene un'operazione non consentita ({type(nodo).__name__})."
            )
    return sql
