"""Client Ollama e prompt per Text-to-SQL e progettazione di dashboard."""

from __future__ import annotations

import json
import re
from typing import Any

import requests

from . import dialetti, grafici, settings


class ErroreLLM(RuntimeError):
    """Problema nel contattare il modello o nell'interpretarne la risposta."""


TIPI_GRAFICO = grafici.ELENCO

_SCHEMA_SQL = {
    "type": "object",
    "properties": {"sql": {"type": "string"}, "spiegazione": {"type": "string"}},
    "required": ["sql", "spiegazione"],
}

_SCHEMA_DASHBOARD = {
    "type": "object",
    "properties": {
        "titolo": {"type": "string"},
        "grafici": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "titolo": {"type": "string"},
                    "tipo": {"type": "string", "enum": TIPI_GRAFICO},
                    "sql": {"type": "string"},
                    "x": {"type": "string"},
                    "y": {"type": "string"},
                    "serie": {"type": "string"},
                    "dimensione": {"type": "string"},
                },
                "required": ["titolo", "tipo", "sql", "x", "y"],
            },
        },
    },
    "required": ["titolo", "grafici"],
}

def _d(dialetto: str) -> dict[str, str]:
    """Regole ed esempi del dialetto del motore (vedi dialetti.py)."""
    return dialetti.esempi(dialetto)


def esempio_mese(dialetto: str) -> str:
    return _d(dialetto)["mese"]


def _regole_sql(dialetto: str) -> str:
    d = _d(dialetto)
    return f"""Regole per la SQL (negli esempi i nomi tra < > sono segnaposto: usa tabelle e colonne reali dello schema):
{d["regole"]}
- Una sola istruzione SELECT (eventualmente con WITH). Mai INSERT, UPDATE, DELETE, DDL, SET o piu' istruzioni.
- Usa SOLO tabelle e colonne presenti nello schema; non inventare nomi. Qualifica le colonne se usi JOIN.
- Se esiste una vista che contiene gia' i dati necessari (es. una vista che unisce gia' le tabelle e calcola gli importi), preferiscila alle JOIN.
- Prima di una JOIN controlla quante righe ci sono per ogni valore della chiave: unire due tabelle che hanno ENTRAMBE piu' righe per lo stesso valore (es. due tabelle di dettaglio sullo stesso codice) ripete le righe e gonfia somme e conteggi; lo stesso succede se sommi una colonna della tabella con una riga per chiave dopo averla unita a una tabella di dettaglio. Se una vista contiene gia' tutte le colonne necessarie usa quella, senza JOIN; altrimenti aggrega una delle due parti in una sottoquery prima di unirla.
- Usa la colonna il cui nome o commento corrisponde alla parola usata nella domanda, e non dare a una colonna l'alias di un'altra: l'alias deve descrivere il contenuto reale.
- Rispetta le indicazioni scritte nei commenti dello schema (ad esempio quali righe escludere da un totale).
- Alias delle colonne calcolate in minuscolo snake_case, senza spazi ne' accenti (es. totale_mensile).
- Per classifiche usa ORDER BY ... DESC con il limite di righe del dialetto (10 se non indicato).
- I periodi relativi ("ultimo mese", "ultimo trimestre", "quest'anno") vanno calcolati rispetto all'ULTIMA data presente nei dati (indicata nello schema), non alla data di oggi.
- Per "ultimo mese", "ultimo trimestre", "ultimi 3 mesi", "ultimo semestre", "ultimo anno", "quest'anno" usa le date gia' calcolate nella sezione PERIODI DI RIFERIMENTO in fondo allo schema, scritte come costanti {d["data"]}. NON ricalcolarle con date_trunc, MAX() o CURRENT_DATE. I trimestri solari si usano solo se l'utente li nomina esplicitamente (es. "Q3", "terzo trimestre", "trimestre solare").
- Nella spiegazione indica sempre le date esatte del periodo considerato.
- Conteggi di entita' (quanti clienti, quanti ordini, quanti prodotti...): usa la tabella o vista con UNA riga per entita', se esiste; altrimenti COUNT(DISTINCT <id_entita>). Mai COUNT(*) su una tabella con piu' righe per la stessa entita' (es. righe di dettaglio): conterebbe le righe, non le entita'.
- Percentuale di righe in un certo stato: 100.0 * SUM(CASE WHEN <condizione> THEN 1 ELSE 0 END) / COUNT(*), su una tabella o vista che contiene TUTTI i casi (anche quelli che altre viste escludono).
- Se la domanda chiede un unico valore (totale, conteggio, media, percentuale) la query deve restituire UNA sola riga.
- Medie "per <entita'>" (per ordine, per cliente, per mese...) richiedono DUE livelli: prima il totale per entita' in una sottoquery, poi la media dei totali. Esempio:
  {d["media"]}
  SBAGLIATO: SELECT AVG(<valore>) FROM <tabella> GROUP BY <id_entita>  (restituisce una riga per entita', non la media).
- "<misura> media mensile", "<misura> medio mensile", "in media al mese" = UN SOLO valore: la media dei totali mensili. Esempio:
  {d["media_mese"]}
  Invece "<misura> mensile", "per mese", "mese per mese", "andamento" = una riga per mese.
- Ogni colonna usata in GROUP BY nella query finale deve comparire anche tra le colonne restituite.
- Se la domanda non indica un periodo, usa tutti i dati disponibili.
- Se la domanda e' ambigua scegli l'interpretazione piu' ragionevole e dichiarala nella spiegazione.{_istruzioni()}"""


def _istruzioni() -> str:
    """Regole di business del database in uso (config/<database>/istruzioni.md), se ci sono."""
    if not settings.AI_ISTRUZIONI:
        return ""
    return (
        "\n\nREGOLE DI QUESTO DATABASE (valgono piu' delle regole generali e degli esempi):\n"
        + settings.AI_ISTRUZIONI
    )


def _prompt_sql(schema: str, dialetto: str) -> str:
    return f"""Sei un esperto di {_d(dialetto)["nome"]} e di analisi dati. Trasformi domande in italiano in una query SQL di sola lettura nel dialetto {_d(dialetto)["nome"]}.

{_regole_sql(dialetto)}

Rispondi SOLO con un oggetto JSON: {{"sql": "<query>", "spiegazione": "<una o due frasi in italiano su cosa calcola la query>"}}

SCHEMA DEL DATABASE:
{schema}"""


def _prompt_dashboard(schema: str, max_grafici: int, dialetto: str) -> str:
    return f"""Sei un analista dati che progetta dashboard. Dalla richiesta dell'utente progetta una dashboard con da 3 a {max_grafici} grafici sui dati descritti dallo schema. Le query vanno scritte nel dialetto {_d(dialetto)["nome"]}. Per ogni grafico scrivi la query SQL che restituisce ESATTAMENTE i dati da disegnare, gia' aggregati (tranne il box_plot, che vuole le singole osservazioni).

{grafici.descrizione_prompt(esempio_mese(dialetto))}

Metti prima 1-3 KPI ("big_number", "gauge" o "big_number_trend") con i valori principali, poi gli altri. Titoli brevi in italiano.
I grafici della stessa dashboard devono essere COERENTI tra loro: stessa definizione di ogni misura e stessi filtri (ad esempio, se una misura esclude certe righe, le escludono TUTTI i grafici su quella misura; la somma dell'andamento mensile deve coincidere con il KPI del totale nello stesso periodo).

{_regole_sql(dialetto)}

Rispondi SOLO con un oggetto JSON:
{{"titolo": "<titolo della dashboard>", "grafici": [{{"titolo": "...", "tipo": "<uno dei tipi ammessi>", "sql": "...", "x": "...", "y": "...", "serie": "", "dimensione": ""}}]}}

SCHEMA DEL DATABASE:
{schema}"""


def _estrai_json(testo: str) -> dict[str, Any]:
    testo = re.sub(r"<think>.*?</think>", "", testo or "", flags=re.DOTALL).strip()
    testo = re.sub(r"^```(?:json)?\s*|\s*```$", "", testo).strip()
    try:
        return json.loads(testo)
    except json.JSONDecodeError:
        inizio, fine = testo.find("{"), testo.rfind("}")
        if inizio >= 0 and fine > inizio:
            try:
                return json.loads(testo[inizio : fine + 1])
            except json.JSONDecodeError:
                pass
    raise ErroreLLM("Il modello non ha restituito un JSON valido: " + testo[:300])


def chat_json(sistema: str, utente: str, formato: dict[str, Any]) -> dict[str, Any]:
    corpo: dict[str, Any] = {
        "model": settings.OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": sistema},
            {"role": "user", "content": utente},
        ],
        "stream": False,
        "format": formato,
        "think": settings.OLLAMA_THINK,
        "options": {"temperature": 0.1},
    }
    if settings.OLLAMA_NUM_CTX > 0:
        corpo["options"]["num_ctx"] = settings.OLLAMA_NUM_CTX
    try:
        r = requests.post(
            f"{settings.OLLAMA_URL}/api/chat", json=corpo, timeout=settings.OLLAMA_TIMEOUT
        )
    except requests.Timeout as ex:
        raise ErroreLLM(
            f"Il modello non ha risposto entro {settings.OLLAMA_TIMEOUT} secondi."
        ) from ex
    except requests.RequestException as ex:
        raise ErroreLLM(f"Impossibile contattare Ollama su {settings.OLLAMA_URL}: {ex}") from ex
    if r.status_code >= 400:
        raise ErroreLLM(f"Ollama ha risposto {r.status_code}: {r.text[:300]}")
    try:
        contenuto = r.json().get("message", {}).get("content", "")
    except ValueError as ex:
        raise ErroreLLM("Risposta di Ollama non leggibile.") from ex
    return _estrai_json(contenuto)


def genera_sql(domanda: str, schema: str, dialetto: str = "postgres") -> dict[str, str]:
    dati = chat_json(_prompt_sql(schema, dialetto), domanda, _SCHEMA_SQL)
    return {"sql": str(dati.get("sql", "")), "spiegazione": str(dati.get("spiegazione", ""))}


def correggi_sql(domanda: str, sql: str, errore: str, schema: str, dialetto: str = "postgres") -> dict[str, str]:
    richiesta = (
        f"Domanda originale: {domanda}\n\n"
        f"Questa query non e' accettabile o ha prodotto un errore:\n{sql}\n\n"
        f"Errore:\n{errore}\n\n"
        f"Correggi la query (dialetto {_d(dialetto)['nome']}) rispettando lo schema. "
        "Cambia SOLO cio' che serve a risolvere l'errore: "
        "mantieni tutti i filtri e le condizioni della query originale (ad esempio le righe "
        "escluse da un totale) e tutte le regole indicate nei commenti dello schema."
    )
    dati = chat_json(_prompt_sql(schema, dialetto), richiesta, _SCHEMA_SQL)
    return {"sql": str(dati.get("sql", "")), "spiegazione": str(dati.get("spiegazione", ""))}


def progetta_dashboard(richiesta: str, schema: str, dialetto: str = "postgres") -> dict[str, Any]:
    dati = chat_json(
        _prompt_dashboard(schema, settings.MAX_CHARTS, dialetto), richiesta, _SCHEMA_DASHBOARD
    )
    grafici = dati.get("grafici") or []
    if not isinstance(grafici, list):
        grafici = []
    return {"titolo": str(dati.get("titolo") or "Dashboard AI"), "grafici": grafici}


def stato() -> dict[str, Any]:
    """Verifica che Ollama risponda e che il modello sia presente."""
    try:
        r = requests.get(f"{settings.OLLAMA_URL}/api/tags", timeout=10)
        r.raise_for_status()
        modelli = [m.get("name") for m in r.json().get("models", [])]
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "messaggio": f"Ollama non raggiungibile su {settings.OLLAMA_URL}: {ex}"}
    presente = settings.OLLAMA_MODEL in modelli
    return {
        "ok": presente,
        "messaggio": (
            f"Ollama raggiungibile, modello {settings.OLLAMA_MODEL} presente."
            if presente
            else f"Ollama raggiungibile ma il modello {settings.OLLAMA_MODEL} non c'e'. Disponibili: {', '.join(modelli)}"
        ),
        "modelli": modelli,
    }
