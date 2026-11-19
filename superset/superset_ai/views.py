"""Blueprint Flask dell'estensione e hook di avvio per Superset."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from functools import lru_cache, wraps
from typing import Any, Callable

from flask import Blueprint, jsonify, redirect, render_template, request, url_for
from flask_login import current_user
from jinja2 import ChoiceLoader, FileSystemLoader

from . import dati, dialetti, llm, registro, settings
from .sqlguard import SqlNonValida, controlla_logica, valida

logger = logging.getLogger(__name__)

_QUI = os.path.dirname(os.path.abspath(__file__))

ai_bp = Blueprint(
    "superset_ai",
    __name__,
    url_prefix="/ai",
    template_folder=os.path.join(_QUI, "templates"),
    static_folder=os.path.join(_QUI, "static"),
    static_url_path="/static",
)


# --------------------------------------------------------------------------- utilita'

def _errore(messaggio: str, codice: int = 400):
    return jsonify({"errore": messaggio}), codice


def _richiede_login_api(f: Callable) -> Callable:
    @wraps(f)
    def wrapper(*args: Any, **kwargs: Any):
        if not current_user or not current_user.is_authenticated:
            return _errore("Accesso richiesto: effettua il login in Superset.", 401)
        return f(*args, **kwargs)

    return wrapper


def _utente() -> str | None:
    try:
        return current_user.username
    except Exception:  # noqa: BLE001
        return None


def _admin() -> bool:
    try:
        return any(r.name == "Admin" for r in current_user.roles)
    except Exception:  # noqa: BLE001
        return False


def _schema(motore: str) -> str:
    try:
        return dati.schema_effettivo(motore)
    except Exception:  # noqa: BLE001 - database non raggiungibile: lo dira' la query
        return settings.AI_SCHEMA


def _registra(tipo: str, motore: str, inizio: float, **campi: Any) -> int | None:
    """Scrive la richiesta nel registro: non fa mai fallire la risposta."""
    return registro.scrivi(
        tipo=tipo,
        motore=motore,
        modello=settings.OLLAMA_MODEL,
        utente=_utente(),
        durata_ms=int((time.monotonic() - inizio) * 1000),
        **campi,
    )


# Colonna inesistente, nei messaggi dei vari motori (il nome e' nel primo gruppo)
_RE_COLONNE = [
    re.compile(r'column "(?:[^".]+\.)?([^".]+)" does not exist'),  # PostgreSQL
    re.compile(r"Column '(?:[^'.]+\.)?([^'.]+)' cannot be resolved"),  # Trino / Presto
    re.compile(r"Unknown column '(?:[^'.]+\.)?([^'.]+)'"),  # MySQL
    re.compile(r'ORA-00904: "?(?:[^".]+"?\.)?"?([^".]+)"?: invalid identifier'),  # Oracle
    re.compile(r"Invalid column name '([^']+)'"),  # SQL Server
    re.compile(r"Invalid column reference '(?:[^'.]+\.)?([^'.]+)'"),  # Hive
    re.compile(r"(?:no such column|cannot resolve) [`'\"]?(?:[^`'\".]+\.)?([^`'\".\s]+)"),  # SQLite, Spark
]
# Trino: il messaggio utile sta in message="..." dentro TrinoUserError(...)
_RE_MSG_TRINO = re.compile(r'message="((?:[^"\\]|\\.)*)"')


def _errore_db(ex: Exception) -> str:
    """Messaggio d'errore utile al modello: testo di Postgres (con HINT) + dove si trova
    davvero la colonna mancante, se esiste in un'altra tabella/vista."""
    righe = [
        r.strip()
        for r in str(ex).strip().splitlines()
        if r.strip() and not r.strip().startswith("LINE ") and set(r.strip()) != {"^"}
    ]
    testo = " ".join(righe[:3]) or type(ex).__name__
    if m := _RE_MSG_TRINO.search(testo):
        testo = "Errore di Trino: " + m.group(1)
    if m := next((m for r in _RE_COLONNE if (m := r.search(testo))), None):
        colonna = m.group(1)
        try:
            dove = dati.dove_si_trova(colonna)
        except Exception:  # noqa: BLE001
            dove = []
        if dove:
            testo += (
                f" | Suggerimento: la colonna {colonna} esiste solo in: {', '.join(dove)}. "
                "Usa quella tabella o vista (oppure la JOIN corretta)."
            )
        else:
            testo += f" | La colonna {colonna} non esiste in nessuna tabella: usa solo le colonne dello schema."
    return testo


def _motore(corpo: dict | None = None) -> str:
    """Motore richiesto (corpo JSON o parametro ?motore=), validato."""
    richiesto = (corpo or {}).get("motore") or request.args.get("motore")
    return dati.motore_valido(str(richiesto) if richiesto else None)


def _id_database(nome: str | None = None) -> int | None:
    """Id del database dei dati registrato in Superset (serve per SQL Lab e per i dataset)."""
    from superset.extensions import db
    from superset.models.core import Database

    rec = (
        db.session.query(Database)
        .filter_by(database_name=nome or settings.AI_DATABASE_NAME)
        .one_or_none()
    )
    return rec.id if rec else None


def _genera_ed_esegui(
    domanda: str, esegui: bool, motore: str = "diretto", tentativi: int = 2
) -> dict[str, Any]:
    """Domanda -> SQL validata (-> risultati). Se il database da errore, chiede una correzione."""
    dial = dati.dialetto(motore)
    schema = dati.descrivi_schema(motore=motore)
    risposta = llm.genera_sql(domanda, schema, dial)
    ultimo_errore = ""
    for tentativo in range(tentativi):
        ultimo = tentativo + 1 == tentativi
        try:
            sql = valida(risposta["sql"], dial)
        except SqlNonValida as ex:
            ultimo_errore = str(ex)
        else:
            # Errori logici tipici (es. GROUP BY su colonna non restituita): si chiede una correzione;
            # all'ultimo tentativo la query viene eseguita comunque, con un avviso.
            avviso = controlla_logica(sql, dial, lambda t, c: dati.colonna_unica(t, c, motore))
            if avviso and not ultimo:
                ultimo_errore = avviso
            else:
                try:
                    # Senza esecuzione (SQL Lab) verifica comunque che la query giri, con 1 riga
                    risultato = dati.esegui_query(sql, limite=None if esegui else 1, motore=motore)
                    uscita = {**risposta, "sql": sql, "tentativi": tentativo + 1, "motore": motore}
                    if esegui:
                        uscita["risultato"] = risultato
                    if avviso:
                        uscita["avviso"] = avviso
                    return uscita
                except Exception as ex:  # noqa: BLE001
                    ultimo_errore = _errore_db(ex)
        if not ultimo:
            logger.info("Text-to-SQL: correzione richiesta al modello (%s)", ultimo_errore)
            risposta = llm.correggi_sql(domanda, risposta["sql"], ultimo_errore, schema, dial)
    return {**risposta, "errore_sql": ultimo_errore, "motore": motore}


def _primo(colonne: list[str], tipi: list[str], voluto: str, ammessi: set[str]) -> str:
    if voluto in colonne and tipi[colonne.index(voluto)] in ammessi:
        return voluto
    for c, t in zip(colonne, tipi):
        if t in ammessi:
            return c
    return ""


# Forma dei dati che ogni tipo di grafico richiede (usata nelle richieste di correzione)
_FORMA = {
    "big_number": "1 sola riga con 1 colonna numerica (il valore del KPI).",
    "bar": "2 colonne: x = categoria (testo) e y = valore numerico; una riga per categoria "
           "(almeno 2, al massimo 15), ordinate per y decrescente.",
    "pie": "2 colonne: x = categoria (testo) e y = valore numerico; da 2 a 8 righe.",
    "line": "2 colonne: x = data di tipo date (es. {mese}) "
            "e y = valore numerico; UNA RIGA PER PERIODO (es. una per mese, almeno 2), ordinata per x. "
            "Serve GROUP BY sul periodo.",
    "table": "piu' colonne di dettaglio, al massimo 50 righe.",
}


def _problema_forma(tipo: str, colonne: list[str], tipi: list[str], n: int) -> str | None:
    """Controlla che i dati abbiano la forma adatta al tipo di grafico."""
    numerici = [c for c, t in zip(colonne, tipi) if t == "numerico"]
    date = [c for c, t in zip(colonne, tipi) if t == "data"]
    testi = [c for c, t in zip(colonne, tipi) if t == "testo"]
    if n == 0:
        return "La query non restituisce righe."
    if tipo == "big_number" and (n != 1 or not numerici):
        return f"Per un KPI serve 1 riga con un valore numerico, ma la query restituisce {n} righe."
    if tipo == "line" and (not date or n < 2 or not numerici):
        motivo = f"la query restituisce {n} righe" + ("" if date else " e nessuna colonna di tipo data")
        return f"Per un grafico a linee servono una colonna data e una riga per periodo: {motivo}."
    if tipo in ("bar", "pie") and (n < 2 or not (testi or date) or not numerici):
        return (f"Per un grafico a {'barre' if tipo == 'bar' else 'torta'} servono una colonna di "
                f"categorie e piu' righe, ma la query restituisce {n} righe.")
    return None


def _verifica_grafico(
    g: dict[str, Any], richiesta: str, schema: str, motore: str = "diretto"
) -> dict[str, Any]:
    """Valida ed esegue la SQL di un grafico. Se la query fallisce o i dati non hanno la forma
    adatta al tipo di grafico, chiede al modello fino a 2 correzioni spiegando la forma richiesta.
    Solo alla fine, se serve, adatta il tipo di grafico ai dati (e lo segnala in "nota")."""
    dial = dati.dialetto(motore)
    titolo = str(g.get("titolo") or "Grafico")
    tipo_chiesto = g.get("tipo") if g.get("tipo") in llm.TIPI_GRAFICO else "table"
    tipo = tipo_chiesto
    sql = str(g.get("sql") or "")
    errore = ""
    tentativi = 3  # la query originale + fino a 2 correzioni del modello
    for tentativo in range(tentativi):
        ultimo = tentativo + 1 == tentativi
        try:
            sql = valida(sql, dial)
            avviso = controlla_logica(sql, dial, lambda t, c: dati.colonna_unica(t, c, motore))
            if avviso and not ultimo:
                raise SqlNonValida(avviso)
            ris = dati.esegui_query(sql, limite=1000, motore=motore)
            problema = _problema_forma(tipo, ris["colonne"], ris["tipi"], len(ris["righe"]))
            if problema and not ultimo:
                raise SqlNonValida(problema)
            errore = problema or ""
            break
        except Exception as ex:  # noqa: BLE001
            errore = str(ex) if isinstance(ex, SqlNonValida) else _errore_db(ex)
            if not ultimo:
                logger.info("Grafico '%s': correzione %d richiesta (%s)", titolo, tentativo + 1, errore)
                corr = llm.correggi_sql(
                    f"{richiesta}\nGrafico da correggere: '{titolo}', di tipo {tipo}.\n"
                    f"Forma dei dati richiesta: {_FORMA[tipo].format(mese=llm.esempio_mese(dial))}",
                    sql, errore, schema, dial,
                )
                sql = corr["sql"]
    else:
        logger.warning("Grafico '%s' scartato: %s | SQL: %s", titolo, errore, sql)
        return {"titolo": titolo, "tipo": tipo, "errore": errore, "sql": sql}

    colonne, tipi, n = ris["colonne"], ris["tipi"], len(ris["righe"])
    if n == 0:
        return {"titolo": titolo, "tipo": tipo, "errore": "La query non restituisce righe.", "sql": sql}

    x = str(g.get("x") or "")
    y = _primo(colonne, tipi, str(g.get("y") or ""), {"numerico"})
    if tipo in ("bar", "pie", "line", "big_number") and not y:
        tipo = "table"  # nessuna colonna numerica da disegnare
    if tipo == "line":
        x = _primo(colonne, tipi, x, {"data"})
        if not x:
            tipo = "bar"
            x = str(g.get("x") or "")
    if tipo in ("bar", "pie"):
        x = _primo(colonne, tipi, x, {"testo", "data"})
        if not x:
            tipo = "big_number" if n == 1 else "table"
    if tipo == "big_number":
        x = ""
        if n > 1:
            tipo = "table"
    if tipo == "table":
        x, y = "", ""
    risultato = {
        "titolo": titolo,
        "tipo": tipo,
        "sql": sql,
        "x": x,
        "y": y,
        "colonne": colonne,
        "tipi": tipi,
        "righe": n,
    }
    if tipo != tipo_chiesto:
        risultato["nota"] = (
            f"mostrato come {tipo} invece di {tipo_chiesto}: dopo 2 correzioni i dati non hanno "
            f"ancora la forma adatta ({errore or 'forma non adatta'})"
        )
        logger.warning("Grafico '%s': %s | SQL: %s", titolo, risultato["nota"], sql)
    return risultato



@lru_cache(maxsize=None)
def _versione_static(filename: str) -> str:
    """Impronta del contenuto di un file statico: cambia solo se cambia il file."""
    try:
        with open(os.path.join(_QUI, "static", filename), "rb") as f:
            return hashlib.md5(f.read()).hexdigest()[:10]  # noqa: S324 - non e' un uso di sicurezza
    except OSError:
        return "0"


@ai_bp.app_template_global("ai_static")
def ai_static(filename: str) -> str:
    """URL di un file statico dell'estensione con la sua impronta (?v=...): dopo un deploy il
    browser scarica il file nuovo invece di usare la copia vecchia che ha in cache."""
    return url_for("superset_ai.static", filename=filename) + "?v=" + _versione_static(filename)

# --------------------------------------------------------------------------- pagine

@ai_bp.route("/")
def pagina_assistant():
    if not current_user or not current_user.is_authenticated:
        return redirect("/login/?next=" + url_for("superset_ai.pagina_assistant"))
    return render_template(
        "superset_ai/assistant.html",
        modello=settings.OLLAMA_MODEL,
        ollama_url=settings.OLLAMA_URL,
        nome_database=settings.AI_DATABASE_NAME,
        motori=[(m, dati.etichetta(m)) for m in dati.motori_disponibili()],
        esempi_domande=settings.AI_ESEMPI_DOMANDE,
        esempi_dashboard=settings.AI_ESEMPI_DASHBOARD,
    )


# --------------------------------------------------------------------------- API

@ai_bp.route("/api/info")
@_richiede_login_api
def api_info():
    motore = _motore()
    nome = dati.nome_database(motore)
    return jsonify(
        {
            "motore": motore,
            "dialetto": dati.dialetto(motore),
            "dialetto_nome": dialetti.nome(dati.dialetto(motore)),
            "motori": [
                {"chiave": m, "etichetta": dati.etichetta(m)} for m in dati.motori_disponibili()
            ],
            "database_id": _id_database(nome),
            "database_nome": nome,
            "schema": _schema(motore),
            "modello": settings.OLLAMA_MODEL,
            "ollama_url": settings.OLLAMA_URL,
            "esempio": settings.AI_ESEMPI_DOMANDE[0] if settings.AI_ESEMPI_DOMANDE else "",
        }
    )


@ai_bp.route("/api/stato")
@_richiede_login_api
def api_stato():
    risultato = llm.stato()
    try:
        dati.verifica_connessione("diretto")
        risultato["database"] = f"Database dei dati raggiungibile ({dati.etichetta('diretto')})."
    except Exception as ex:  # noqa: BLE001
        risultato["ok"] = False
        risultato["database"] = f"Database dei dati non raggiungibile: {ex}"
    if "trino" in dati.motori_disponibili():
        try:
            risultato["trino"] = dati.verifica_trino()
        except Exception as ex:  # noqa: BLE001
            risultato["trino"] = f"Trino non raggiungibile: {_errore_db(ex)}"
    return jsonify(risultato)


@ai_bp.route("/api/schema")
@_richiede_login_api
def api_schema():
    try:
        return jsonify(
            {
                "motore": _motore(),
                "schema": dati.descrivi_schema(
                    forza=request.args.get("aggiorna") == "1", motore=_motore()
                ),
            }
        )
    except Exception as ex:  # noqa: BLE001
        return _errore(f"Impossibile leggere lo schema: {ex}", 500)


@ai_bp.route("/api/sql", methods=["POST"])
@_richiede_login_api
def api_sql():
    corpo = request.get_json(silent=True) or {}
    domanda = str(corpo.get("domanda") or "").strip()
    if not domanda:
        return _errore("Scrivi una domanda.")
    motore = _motore(corpo)
    inizio = time.monotonic()
    try:
        risposta = _genera_ed_esegui(domanda, esegui=bool(corpo.get("esegui", True)), motore=motore)
    except llm.ErroreLLM as ex:
        _registra("sql", motore, inizio, domanda=domanda, esito="errore", errore=str(ex))
        return _errore(str(ex), 502)
    except Exception as ex:  # noqa: BLE001
        logger.exception("Errore Text-to-SQL")
        _registra("sql", motore, inizio, domanda=domanda, esito="errore", errore=str(ex))
        return _errore(f"Errore inatteso: {ex}", 500)
    ris = risposta.get("risultato") or {}
    risposta["registro_id"] = _registra(
        "sql", motore, inizio,
        domanda=domanda,
        sql=risposta.get("sql"),
        spiegazione=risposta.get("spiegazione"),
        esito="errore" if risposta.get("errore_sql") else ("avviso" if risposta.get("avviso") else "ok"),
        errore=risposta.get("errore_sql") or risposta.get("avviso"),
        righe=len(ris.get("righe") or []) if ris else None,
        tentativi=risposta.get("tentativi"),
    )
    return jsonify(risposta)


@ai_bp.route("/api/esegui", methods=["POST"])
@_richiede_login_api
def api_esegui():
    corpo = request.get_json(silent=True) or {}
    motore = _motore(corpo)
    inizio = time.monotonic()
    testo = str(corpo.get("sql") or "")
    try:
        sql = valida(testo, dati.dialetto(motore))
    except SqlNonValida as ex:
        _registra("riesegui", motore, inizio, sql=testo, esito="errore", errore=str(ex))
        return _errore(str(ex))
    try:
        risultato = dati.esegui_query(sql, motore=motore)
    except Exception as ex:  # noqa: BLE001
        errore = _errore_db(ex)
        _registra("riesegui", motore, inizio, sql=sql, esito="errore", errore=errore)
        return _errore(f"Errore del database: {errore}")
    id_riga = _registra("riesegui", motore, inizio, sql=sql, esito="ok", righe=len(risultato["righe"]))
    return jsonify({"sql": sql, "motore": motore, "risultato": risultato, "registro_id": id_riga})


@ai_bp.route("/api/dashboard", methods=["POST"])
@_richiede_login_api
def api_dashboard():
    """Restituisce il PIANO della dashboard (grafici con SQL verificata).
    La creazione di dataset, grafici e dashboard la fa il browser con le REST API di
    Superset, cosi' rispetta i permessi dell'utente collegato."""
    corpo = request.get_json(silent=True) or {}
    richiesta = str(corpo.get("richiesta") or "").strip()
    if not richiesta:
        return _errore("Descrivi la dashboard che vuoi.")
    motore = _motore(corpo)
    inizio = time.monotonic()
    try:
        schema = dati.descrivi_schema(motore=motore)
        piano = llm.progetta_dashboard(richiesta, schema, dati.dialetto(motore))
        grafici = [
            _verifica_grafico(g, richiesta, schema, motore)
            for g in piano["grafici"][: settings.MAX_CHARTS]
            if isinstance(g, dict)
        ]
    except llm.ErroreLLM as ex:
        _registra("dashboard", motore, inizio, domanda=richiesta, esito="errore", errore=str(ex))
        return _errore(str(ex), 502)
    except Exception as ex:  # noqa: BLE001
        logger.exception("Errore progettazione dashboard")
        _registra("dashboard", motore, inizio, domanda=richiesta, esito="errore", errore=str(ex))
        return _errore(f"Errore inatteso: {ex}", 500)
    validi = [g for g in grafici if not g.get("errore")]
    scartati = [g for g in grafici if g.get("errore")]
    elenco_sql = "\n\n".join(f"-- {g['titolo']} ({g['tipo']})\n{g['sql']}" for g in grafici)
    problemi = "; ".join(f"{g['titolo']}: {g['errore']}" for g in scartati)
    id_riga = _registra(
        "dashboard", motore, inizio,
        domanda=richiesta,
        sql=elenco_sql,
        spiegazione=piano.get("titolo"),
        esito="errore" if not validi else ("avviso" if scartati else "ok"),
        errore=problemi or None,
        righe=len(validi),
    )
    if not validi:
        return _errore("Nessun grafico valido. Errori: " + problemi)
    return jsonify(
        {
            "titolo": piano["titolo"],
            "grafici": validi,
            "scartati": scartati,
            "database_id": _id_database(dati.nome_database(motore)),
            "database_nome": dati.nome_database(motore),
            "motore": motore,
            "schema": _schema(motore),
            "registro_id": id_riga,
        }
    )


@ai_bp.route("/api/voto", methods=["POST"])
@_richiede_login_api
def api_voto():
    """Voto dell'utente su una risposta: {"id": ..., "voto": 1 | -1, "nota": "..."}."""
    corpo = request.get_json(silent=True) or {}
    try:
        id_riga, voto = int(corpo.get("id")), int(corpo.get("voto"))
    except (TypeError, ValueError):
        return _errore("Indica id e voto (1 oppure -1).")
    try:
        ok = registro.vota(id_riga, voto, str(corpo.get("nota") or "") or None, _utente(), _admin())
    except Exception as ex:  # noqa: BLE001
        return _errore(f"Registro non disponibile: {ex}", 500)
    if not ok:
        return _errore("Richiesta non trovata nel registro.", 404)
    return jsonify({"ok": True})


@ai_bp.route("/api/registro")
@_richiede_login_api
def api_registro():
    """Ultime richieste all'AI (un amministratore vede quelle di tutti). ?formato=csv per il file."""
    try:
        limite = int(request.args.get("limite") or 100)
    except ValueError:
        limite = 100
    utente = None if _admin() else _utente()
    try:
        righe = registro.elenco(limite, utente=utente, tipo=request.args.get("tipo") or None)
        riepilogo = registro.riepilogo(utente)
    except Exception as ex:  # noqa: BLE001
        return _errore(f"Registro non disponibile: {ex}", 500)
    if request.args.get("formato") == "csv":
        import csv
        import io

        buffer = io.StringIO()
        campi = [c.name for c in registro.TABELLA.columns]
        scrittore = csv.DictWriter(buffer, fieldnames=campi, delimiter=";")
        scrittore.writeheader()
        scrittore.writerows(righe)
        return (
            "\ufeff" + buffer.getvalue(),
            200,
            {
                "Content-Type": "text/csv; charset=utf-8",
                "Content-Disposition": "attachment; filename=registro-ai.csv",
            },
        )
    return jsonify({"righe": righe, "riepilogo": riepilogo, "tutti": utente is None})


# --------------------------------------------------------------------------- aggancio a Superset

def app_mutator(app) -> None:
    """Chiamato da Superset all'avvio (FLASK_APP_MUTATOR).

    1. Mette i nostri template "override" davanti a quelli di Superset: cosi'
       tail_js_custom_extra.html (incluso in ogni pagina) carica il pannello per SQL Lab.
    2. Aggiunge la voce di menu "AI Assistant".
    """
    override = FileSystemLoader(os.path.join(_QUI, "templates", "override"))
    app.jinja_loader = ChoiceLoader([override, app.jinja_loader])

    try:
        from superset.extensions import appbuilder

        appbuilder.add_link(
            "AI Assistant",
            href="/ai/",
            icon="fa-magic",
            label="AI Assistant",
            category="AI",
            category_label="AI",
            category_icon="fa-magic",
        )
    except Exception:  # noqa: BLE001 - il menu e' un di piu': il pulsante flottante c'e' comunque
        logger.exception("Impossibile aggiungere la voce di menu AI Assistant")
