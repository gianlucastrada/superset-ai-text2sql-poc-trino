"""Prove automatiche: pongono al modello le domande di un file di prove, eseguono la SQL generata
e confrontano i risultati con i valori attesi. Producono un report (Markdown e JSON) e, se si
passa il report precedente, l'elenco di cosa e' migliorato o peggiorato.

Si eseguono dentro il pod di Superset (stesso modello, stesso database, stessa configurazione
dell'applicazione), di solito con scripts\\prove.ps1:

    python -m superset_ai.prove /tmp/prove/prove.yaml --motore tutti --report /tmp/prove/out
        [--solo 1.2,4.5] [--precedente /tmp/prove/precedente.json]

Il formato del file delle prove e' descritto in config/vendite/prove.yaml.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import unicodedata
from typing import Any

from . import dati, llm, settings

# Le funzioni della pipeline sono le stesse delle API della pagina AI
from .views import _genera_ed_esegui, _verifica_grafico

ESITI = {"ok": "✅", "ko": "❌", "errore": "⚠️"}


# --------------------------------------------------------------------------- confronti

def _norm(testo: Any) -> str:
    s = unicodedata.normalize("NFKD", str(testo)).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).strip().casefold()


def _numeri(valori: list) -> list[float]:
    return [float(v) for v in valori if isinstance(v, (int, float)) and not isinstance(v, bool)]


def _vicino(trovato: float, atteso: float, caso: dict, prova: dict) -> bool:
    rel = float(caso.get("tolleranza", prova.get("tolleranza", 0.005)))
    ass = float(caso.get("tolleranza_assoluta", 0))
    candidati = [atteso]
    if caso.get("percentuale"):
        candidati += [atteso / 100, atteso * 100]
    return any(abs(trovato - a) <= max(ass, rel * abs(a), 1e-9) for a in candidati)


def _confronta(caso: dict, prova: dict, sql: str, ris: dict | None, risposta: dict) -> list[str]:
    """Elenco dei problemi (vuoto = superata)."""
    problemi: list[str] = []
    righe = (ris or {}).get("righe") or []
    colonne = (ris or {}).get("colonne") or []
    tutti = [v for r in righe for v in r]
    numeri = _numeri(tutti)

    for testo in caso.get("sql_contiene") or []:
        if not re.search(testo, sql or "", re.IGNORECASE):
            problemi.append(f"la SQL non contiene {testo!r}")
    for testo in caso.get("sql_non_contiene") or []:
        if re.search(testo, sql or "", re.IGNORECASE):
            problemi.append(f"la SQL contiene {testo!r}")
    if caso.get("senza_avvisi") and risposta.get("avviso"):
        problemi.append("avviso del controllo logico: " + str(risposta["avviso"])[:160])
    if caso.get("una_riga") and len(righe) != 1:
        problemi.append(f"attesa 1 riga, ottenute {len(righe)}")
    if "n_righe" in caso and len(righe) != int(caso["n_righe"]):
        problemi.append(f"attese {caso['n_righe']} righe, ottenute {len(righe)}")
    for regex in caso.get("colonne") or []:
        if not any(re.search(regex, str(c), re.IGNORECASE) for c in colonne):
            problemi.append(f"nessuna colonna come {regex!r} (colonne: {', '.join(map(str, colonne))})")
    for testo in caso.get("testi") or []:
        if not any(_norm(testo) in _norm(v) for v in tutti if isinstance(v, str)):
            problemi.append(f"nessuna cella contiene {testo!r}")

    attesi = ([caso["valore"]] if "valore" in caso else []) + list(caso.get("valori") or [])
    for atteso in attesi:
        if not any(_vicino(n, float(atteso), caso, prova) for n in numeri):
            vicini = sorted(numeri, key=lambda n: abs(n - float(atteso)))[:3]
            problemi.append(f"atteso {atteso}, trovati {', '.join(f'{v:g}' for v in vicini) or 'nessun numero'}")

    ultima_posizione = -1
    for etichetta, atteso in (caso.get("righe") or {}).items():
        posizione = next(
            (
                i for i, r in enumerate(righe)
                if any(isinstance(v, str) and (_norm(v) == _norm(etichetta) or _norm(v).startswith(_norm(etichetta))) for v in r)
                and any(_vicino(n, float(atteso), caso, prova) for n in _numeri(r))
            ),
            None,
        )
        if posizione is None:
            riga = next((r for r in righe if any(isinstance(v, str) and _norm(v) == _norm(etichetta) for v in r)), None)
            dettaglio = f"riga trovata con {', '.join(f'{v:g}' for v in _numeri(riga))}" if riga else "riga non trovata"
            problemi.append(f"{etichetta}: atteso {atteso} ({dettaglio})")
        elif caso.get("ordine") and posizione < ultima_posizione:
            problemi.append(f"{etichetta}: ordine diverso da quello atteso")
        else:
            ultima_posizione = posizione if posizione is not None else ultima_posizione
    return problemi


# --------------------------------------------------------------------------- esecuzione di un caso

def _caso_domanda(caso: dict, prova: dict, motore: str) -> dict:
    risposta = _genera_ed_esegui(str(caso["domanda"]), esegui=True, motore=motore)
    sql = risposta.get("sql") or ""
    if caso.get("sicurezza"):
        # Una richiesta di modifica: va bene se e' stata bloccata o se e' diventata una SELECT
        if risposta.get("errore_sql"):
            return {"esito": "ok", "motivo": "bloccata: " + str(risposta["errore_sql"])[:120], "sql": sql}
        return {"esito": "ok", "motivo": "trasformata in una query di sola lettura", "sql": sql, "risposta": risposta}
    if risposta.get("errore_sql"):
        if caso.get("nessun_crash"):
            return {"esito": "ok", "motivo": "errore gestito: " + str(risposta["errore_sql"])[:120], "sql": sql}
        return {"esito": "errore", "motivo": "la query non funziona: " + str(risposta["errore_sql"])[:200], "sql": sql}
    problemi = _confronta(caso, prova, sql, risposta.get("risultato"), risposta)
    return {
        "esito": "ko" if problemi else "ok",
        "motivo": "; ".join(problemi),
        "sql": sql,
        "spiegazione": risposta.get("spiegazione"),
        "tentativi": risposta.get("tentativi"),
        "righe": len((risposta.get("risultato") or {}).get("righe") or []),
    }


def _caso_dashboard(caso: dict, prova: dict, motore: str) -> dict:
    """Progetta la dashboard e ne verifica i grafici come fa la pagina AI (senza crearla in
    Superset); poi cerca i valori attesi nei dati dei grafici validi."""
    schema = dati.descrivi_schema(motore=motore)
    piano = llm.progetta_dashboard(str(caso["dashboard"]), schema, dati.dialetto(motore))
    grafici = [
        _verifica_grafico(g, str(caso["dashboard"]), schema, motore)
        for g in piano["grafici"][: settings.MAX_CHARTS]
        if isinstance(g, dict)
    ]
    validi = [g for g in grafici if not g.get("errore")]
    righe: list[list] = []
    colonne: list[str] = []
    for g in validi:
        ris = dati.esegui_query(g["sql"], limite=1000, motore=motore)
        righe += ris["righe"]
        colonne += ris["colonne"]
    problemi = _confronta(
        {k: v for k, v in caso.items() if k not in ("una_riga", "n_righe")},
        prova,
        "\n".join(g["sql"] for g in grafici),
        {"righe": righe, "colonne": colonne},
        {},
    )
    minimo = int(caso.get("grafici_min", 1))
    if len(validi) < minimo:
        problemi.append(f"attesi almeno {minimo} grafici validi, ottenuti {len(validi)}")
    scartati = [f"{g['titolo']}: {g['errore']}" for g in grafici if g.get("errore")]
    if scartati:
        problemi.append("scartati: " + "; ".join(scartati)[:200])
    return {
        "esito": "ko" if problemi else "ok",
        "motivo": "; ".join(problemi),
        "sql": "\n\n".join(f"-- {g['titolo']} ({g['tipo']})\n{g['sql']}" for g in grafici),
        "spiegazione": piano.get("titolo"),
        "righe": len(validi),
    }


def _controllo_dopo(caso: dict, prova: dict) -> str | None:
    """Controllo sul database dopo il caso (es. nessun dato cancellato)."""
    dopo = caso.get("dopo")
    if not dopo:
        return None
    ris = dati.esegui_query(str(dopo["sql"]), limite=10, motore="diretto")
    problemi = _confronta(dopo, prova, "", ris, {})
    return "; ".join(problemi) or None


def esegui_caso(caso: dict, prova: dict, motore: str) -> dict:
    inizio = time.monotonic()
    try:
        if "dashboard" in caso:
            esito = _caso_dashboard(caso, prova, motore)
        else:
            esito = _caso_domanda(caso, prova, motore)
        if (problema := _controllo_dopo(caso, prova)):
            esito.update(esito="ko", motivo=(esito.get("motivo") + "; " if esito.get("motivo") else "") + "dopo: " + problema)
    except Exception as ex:  # noqa: BLE001 - una prova che fallisce non ferma le altre
        if caso.get("nessun_crash") and isinstance(ex, llm.ErroreLLM):
            esito = {"esito": "ok", "motivo": f"errore gestito: {ex}"[:160]}
        else:
            esito = {"esito": "errore", "motivo": f"{type(ex).__name__}: {ex}"[:300]}
    esito.pop("risposta", None)
    return {
        "id": str(caso.get("id")),
        "domanda": caso.get("domanda") or caso.get("dashboard"),
        "tipo": "dashboard" if "dashboard" in caso else "domanda",
        "motore": motore,
        "secondi": round(time.monotonic() - inizio, 1),
        **esito,
    }


# --------------------------------------------------------------------------- report

def _riepilogo(risultati: list[dict]) -> dict[str, dict]:
    per_motore: dict[str, dict] = {}
    for r in risultati:
        m = per_motore.setdefault(r["motore"], {"totale": 0, "ok": 0, "ko": 0, "errore": 0, "secondi": 0.0})
        m["totale"] += 1
        m[r["esito"]] += 1
        m["secondi"] += r["secondi"]
    for m in per_motore.values():
        m["percentuale"] = round(100 * m["ok"] / m["totale"], 1) if m["totale"] else 0
        m["secondi_medi"] = round(m["secondi"] / m["totale"], 1) if m["totale"] else 0
    return per_motore


def _confronto(risultati: list[dict], precedente: list[dict]) -> dict[str, list[str]]:
    prima = {(r["id"], r["motore"]): r["esito"] for r in precedente}
    peggiorate, migliorate = [], []
    for r in risultati:
        vecchio = prima.get((r["id"], r["motore"]))
        if vecchio is None or vecchio == r["esito"]:
            continue
        voce = f"{r['id']} [{r['motore']}]: {vecchio} -> {r['esito']}"
        (migliorate if r["esito"] == "ok" else peggiorate).append(voce)
    return {"peggiorate": peggiorate, "migliorate": migliorate}


def _cella(testo: Any) -> str:
    return str(testo or "").replace("|", "\\|").replace("\n", " ")


def report_markdown(info: dict, risultati: list[dict], riepilogo: dict, confronto: dict | None) -> str:
    righe = [
        f"# Prove automatiche - {info['quando']}",
        "",
        f"Modello **{info['modello']}** · file `{info['file']}` · motori: {', '.join(info['motori'])}",
        "",
        "| Motore | Superate | Sbagliate | Errori | % | Tempo medio |",
        "|---|---|---|---|---|---|",
    ]
    for motore, m in riepilogo.items():
        righe.append(f"| {motore} | {m['ok']}/{m['totale']} | {m['ko']} | {m['errore']} | {m['percentuale']}% | {m['secondi_medi']} s |")
    if confronto is not None:
        righe += ["", "## Rispetto alla volta precedente", ""]
        if not (confronto["peggiorate"] or confronto["migliorate"]):
            righe.append("Nessun cambiamento negli esiti.")
        for titolo, chiave in (("Peggiorate", "peggiorate"), ("Migliorate", "migliorate")):
            if confronto[chiave]:
                righe.append(f"**{titolo}:** " + "; ".join(confronto[chiave]))
    righe += ["", "## Esiti", "", "| | Prova | Motore | Domanda | Tempo | Motivo |", "|---|---|---|---|---|---|"]
    for r in risultati:
        righe.append(
            f"| {ESITI[r['esito']]} | {r['id']} | {r['motore']} | {_cella(r['domanda'])[:90]} | {r['secondi']} s | {_cella(r.get('motivo'))[:200]} |"
        )
    falliti = [r for r in risultati if r["esito"] != "ok"]
    if falliti:
        righe += ["", "## Dettaglio delle prove non superate", ""]
        for r in falliti:
            righe += [
                f"### {ESITI[r['esito']]} {r['id']} [{r['motore']}] {r['domanda']}",
                "",
                f"- **Motivo:** {r.get('motivo')}",
                f"- **Spiegazione del modello:** {r.get('spiegazione') or '-'}",
                "",
                "```sql",
                (r.get("sql") or "").strip(),
                "```",
                "",
            ]
    return "\n".join(righe) + "\n"


# --------------------------------------------------------------------------- main

def _carica(percorso: str) -> dict:
    with open(percorso, encoding="utf-8") as f:
        testo = f.read()
    if percorso.endswith(".json"):
        return json.loads(testo)
    import yaml

    return yaml.safe_load(testo)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prove automatiche del Text-to-SQL")
    parser.add_argument("file", help="file delle prove (YAML o JSON)")
    parser.add_argument("--motore", default="tutti", help="diretto, trino o tutti")
    parser.add_argument("--solo", default="", help="id delle prove da eseguire, separati da virgola (es. 1.2,4.5 oppure 4.*)")
    parser.add_argument("--report", default="", help="cartella dove scrivere risultati.md e risultati.json")
    parser.add_argument("--precedente", default="", help="risultati.json di una esecuzione precedente, per il confronto")
    args = parser.parse_args(argv)

    prova = _carica(args.file)
    casi = prova.get("casi") or []
    if args.solo:
        filtri = [s.strip() for s in args.solo.split(",") if s.strip()]
        casi = [c for c in casi if any(re.fullmatch(f.replace(".", r"\.").replace("*", ".*"), str(c.get("id"))) for f in filtri)]
    disponibili = dati.motori_disponibili()
    motori = disponibili if args.motore == "tutti" else [m for m in args.motore.split(",") if m in disponibili]
    if not motori:
        print(f"Nessun motore disponibile tra: {args.motore} (configurati: {', '.join(disponibili)})")
        return 2

    totale = sum(len([m for m in motori if not c.get("motori") or m in c["motori"]]) for c in casi)
    print(f"Prove: {len(casi)} casi, {totale} esecuzioni · motori: {', '.join(motori)} · modello {settings.OLLAMA_MODEL}", flush=True)
    risultati = []
    for caso in casi:
        for motore in motori:
            if caso.get("motori") and motore not in caso["motori"]:
                continue
            r = esegui_caso(caso, prova, motore)
            risultati.append(r)
            print(
                f"{ESITI[r['esito']]} {r['id']:<6} [{motore}] {r['secondi']:>5} s  {str(r['domanda'])[:60]}"
                + (f"\n      {r['motivo']}" if r.get("motivo") and r["esito"] != "ok" else ""),
                flush=True,
            )

    riepilogo = _riepilogo(risultati)
    print("\nRiepilogo:")
    for motore, m in riepilogo.items():
        print(f"  {motore}: {m['ok']}/{m['totale']} superate ({m['percentuale']}%), {m['ko']} sbagliate, {m['errore']} errori, {m['secondi_medi']} s in media")

    confronto = None
    if args.precedente and os.path.exists(args.precedente):
        try:
            confronto = _confronto(risultati, _carica(args.precedente).get("risultati") or [])
            for titolo, chiave in (("Peggiorate", "peggiorate"), ("Migliorate", "migliorate")):
                if confronto[chiave]:
                    print(f"  {titolo} rispetto alla volta precedente: " + "; ".join(confronto[chiave]))
        except Exception as ex:  # noqa: BLE001
            print(f"  Confronto con la volta precedente non riuscito: {ex}")

    if args.report:
        os.makedirs(args.report, exist_ok=True)
        info = {
            "quando": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "modello": settings.OLLAMA_MODEL,
            "file": os.path.basename(args.file),
            "motori": motori,
        }
        with open(os.path.join(args.report, "risultati.json"), "w", encoding="utf-8") as f:
            json.dump({"info": info, "riepilogo": riepilogo, "confronto": confronto, "risultati": risultati}, f, ensure_ascii=False, indent=1)
        with open(os.path.join(args.report, "risultati.md"), "w", encoding="utf-8") as f:
            f.write(report_markdown(info, risultati, riepilogo, confronto))
        print(f"\nReport: {os.path.join(args.report, 'risultati.md')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
