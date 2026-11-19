"""Controlli sulla SQL generata dal modello: una sola istruzione, solo lettura.

E' il primo livello di protezione. Il secondo (quello che conta davvero) e' l'utente del
database, che deve avere solo permessi di lettura (nella POC "ai_reader", usato anche da Trino).
Si segnalano anche i costrutti di altri dialetti che il motore non accetta (vedi dialetti.py).
"""

import re
from typing import Callable

import sqlglot
from sqlglot import exp

from . import dialetti


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


# Funzione che dice se una colonna ha un valore diverso per ogni riga della sua tabella/vista:
# True, False o None (non si sa). La fornisce dati.colonna_unica; senza, il controllo e' saltato.
Unicita = Callable[[str, str], "bool | None"]

_AGGREGATI_SOMMANO = tuple(
    cls for cls in (getattr(exp, n, None) for n in ("Sum", "Avg", "Count")) if isinstance(cls, type)
)


def _sorgenti(sel) -> list:
    """Espressioni nel FROM e nelle JOIN di UNA select (non delle sue sottoquery)."""
    da = sel.args.get("from") or sel.args.get("from_")
    sorgenti = [da.this] if da is not None else []
    return sorgenti + [j.this for j in sel.args.get("joins") or []]


def _uguaglianze(sel, alias: dict[str, str]) -> list[tuple[str, str, str, str]]:
    """Coppie (alias_a, colonna_a, alias_b, colonna_b) usate per collegare due sorgenti:
    condizioni ON, USING e uguaglianze nel WHERE (JOIN scritte con la virgola)."""
    condizioni = [j.args.get("on") for j in sel.args.get("joins") or []]
    where = sel.args.get("where")
    condizioni.append(where.this if where is not None else None)
    coppie = []
    for cond in condizioni:
        if cond is None:
            continue
        for uguale in cond.find_all(exp.EQ):
            a, b = uguale.this, uguale.expression
            if not (isinstance(a, exp.Column) and isinstance(b, exp.Column)):
                continue
            ta, tb = a.table.lower(), b.table.lower()
            if ta in alias and tb in alias and ta != tb:
                coppie.append((ta, a.name, tb, b.name))
    # JOIN ... USING (colonna): collega la sorgente unita con la prima del FROM
    nomi = list(alias)
    for j in sel.args.get("joins") or []:
        destra = j.this.alias_or_name.lower() if isinstance(j.this, exp.Table) else None
        if destra in alias and nomi and nomi[0] != destra:
            for col in j.args.get("using") or []:
                coppie.append((nomi[0], col.name, destra, col.name))
    return coppie


def _join_che_moltiplica(radice, unicita: Unicita) -> str | None:
    """JOIN che ripetono le righe, riconosciute dai dati e non dai nomi delle tabelle.

    Per ogni JOIN tra due tabelle/viste si chiede se la colonna di collegamento ha valori unici:
    - unica da nessuna delle due parti (molti-a-molti): ogni riga si ripete, tutto e' gonfiato;
    - unica da una parte sola: le righe di quella parte si ripetono per ogni riga collegata
      dell'altra, quindi SUM/AVG/COUNT sulle sue colonne risultano gonfiati."""
    cte = {c.alias_or_name.lower() for c in radice.find_all(exp.CTE)}
    for sel in radice.find_all(exp.Select):
        alias = {
            s.alias_or_name.lower(): s.name
            for s in _sorgenti(sel)
            if isinstance(s, exp.Table) and s.name.lower() not in cte
        }
        if len(alias) < 2:
            continue
        for aa, ca, ab, cb in _uguaglianze(sel, alias):
            ta, tb = alias[aa], alias[ab]
            ua, ub = unicita(ta, ca), unicita(tb, cb)
            if ua is False and ub is False:
                return (
                    f"La JOIN tra {ta} e {tb} su {aa}.{ca} = {ab}.{cb} collega piu' righe a piu' "
                    "righe: la colonna non ha valori unici in nessuna delle due, quindi ogni riga si "
                    "ripete e somme e conteggi risultano gonfiati. Usa una sola tabella o vista che "
                    "contenga gia' tutte le colonne necessarie, oppure aggrega una delle due parti in "
                    "una sottoquery prima della JOIN."
                )
            if {ua, ub} != {True, False}:
                continue  # entrambe uniche (uno-a-uno) oppure non si sa
            uno, molti, t_uno, t_molti = (aa, ab, ta, tb) if ua else (ab, aa, tb, ta)
            for proiezione in sel.expressions:
                for agg in proiezione.find_all(*_AGGREGATI_SOMMANO):
                    if agg.find_ancestor(exp.Select) is not sel:
                        continue
                    if isinstance(agg.this, exp.Distinct) or agg.args.get("distinct"):
                        continue  # COUNT(DISTINCT ...) non risente delle ripetizioni
                    if any(c.table.lower() == uno for c in agg.find_all(exp.Column)):
                        return (
                            f"La query calcola {agg.sql()} su colonne di {t_uno}, ma la JOIN con "
                            f"{t_molti} ripete ogni riga di {t_uno} per ogni riga collegata di "
                            f"{t_molti}: il risultato e' gonfiato. Calcola quel valore su {t_uno} in "
                            "una sottoquery separata (senza JOIN), oppure prendi le colonne da sommare "
                            f"da {t_molti}."
                        )
    return None


def controlla_logica(
    sql: str, dialetto: str = "postgres", unicita: Unicita | None = None
) -> str | None:
    """Cerca errori logici tipici dei modelli. Restituisce un messaggio o None.

    Caso principale: la query finale raggruppa per colonne che poi NON restituisce
    (es. AVG(importo) ... GROUP BY ordine_id). Produce tante righe senza etichetta:
    quasi sempre il modello voleva un'aggregazione in due passaggi.
    """
    try:
        radice = sqlglot.parse_one(sql, read=dialetto or None)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(radice, exp.Select):
        return None
    if unicita is not None and (moltiplica := _join_che_moltiplica(radice, unicita)):
        return moltiplica
    gruppo = radice.args.get("group")
    if not gruppo or not gruppo.expressions:
        return None
    uscite_nomi = {p.alias_or_name.lower() for p in radice.expressions}
    uscite_expr = {p.unalias().sql(dialect=dialetto or None).lower() for p in radice.expressions}
    mancanti = []
    for g in gruppo.expressions:
        if isinstance(g, exp.Literal):  # GROUP BY 1, 2 ...: si riferisce a colonne restituite
            continue
        testo = g.sql(dialect=dialetto or None).lower()
        nome = g.alias_or_name.lower() if isinstance(g, exp.Column) else testo
        if testo not in uscite_expr and nome not in uscite_nomi:
            mancanti.append(g.sql(dialect=dialetto or None))
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


def valida(sql: str, dialetto: str = "postgres") -> str:
    """Restituisce la SQL pulita, oppure solleva SqlNonValida."""
    sql = pulisci(sql)
    if not sql:
        raise SqlNonValida("La query e' vuota.")
    if errore := dialetti.errori_sintassi(sql, dialetto):
        raise SqlNonValida(errore)

    try:
        istruzioni = [s for s in sqlglot.parse(sql, read=dialetto or None) if s is not None]
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
