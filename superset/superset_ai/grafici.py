"""Tipi di grafico che l'AI puo' proporre per le dashboard, raggruppati per FORMA DEI DATI.

Ogni forma dice com'e' fatto il risultato della query (quante righe, quali colonne): il modello
la legge nel prompt, la verifica la controlla prima di creare il grafico e, se non va, la
spiega al modello per farsi correggere la query. Tipi con la stessa forma (barre, torta,
imbuto...) differiscono solo nel disegno, che sceglie assistant.js (formData).

Campi del piano di un grafico: x, y, serie, dimensione (vedi le forme qui sotto).
"""

from __future__ import annotations

import math
import re
from typing import Any

# tipo -> forma, nome leggibile, uso della serie ("no" | "facoltativa" | "obbligatoria")
TIPI: dict[str, dict[str, str]] = {
    "big_number": {"forma": "valore", "nome": "KPI"},
    "gauge": {"forma": "valore", "nome": "indicatore a lancetta"},
    "big_number_trend": {"forma": "tempo", "nome": "KPI con andamento", "serie": "no"},
    "line": {"forma": "tempo", "nome": "grafico a linee", "serie": "facoltativa"},
    "area": {"forma": "tempo", "nome": "grafico ad aree", "serie": "facoltativa"},
    "bar": {"forma": "categorie", "nome": "grafico a barre", "serie": "facoltativa"},
    "bar_horizontal": {"forma": "categorie", "nome": "grafico a barre orizzontali", "serie": "facoltativa"},
    "bar_stacked": {"forma": "categorie", "nome": "grafico a barre impilate", "serie": "obbligatoria"},
    "pie": {"forma": "categorie", "nome": "grafico a torta", "serie": "no"},
    "donut": {"forma": "categorie", "nome": "grafico a ciambella", "serie": "no"},
    "funnel": {"forma": "categorie", "nome": "grafico a imbuto", "serie": "no"},
    "treemap": {"forma": "categorie", "nome": "treemap", "serie": "facoltativa"},
    "heatmap": {"forma": "matrice", "nome": "mappa di calore", "serie": "obbligatoria"},
    "pivot": {"forma": "matrice", "nome": "tabella pivot", "serie": "obbligatoria"},
    "bubble": {"forma": "punti", "nome": "grafico a bolle"},
    "box_plot": {"forma": "osservazioni", "nome": "box plot", "serie": "facoltativa"},
    "histogram": {"forma": "osservazioni", "nome": "istogramma", "serie": "facoltativa"},
    "table": {"forma": "dettaglio", "nome": "tabella"},
}
ELENCO = list(TIPI)
# Tipi mostrati nella riga dei KPI e tipi a tutta larghezza (usati da assistant.js per il layout)
KPI = ["big_number", "gauge", "big_number_trend"]
LARGHI = ["table", "pivot", "heatmap"]

_FORME = {
    "valore": "1 riga con 1 colonna numerica; y = quella colonna, x = \"\".",
    "tempo": (
        "x = colonna data di tipo date (es. {mese}), y = valore numerico, una riga per periodo "
        "(e per serie), ordinata per x; serie = colonna di testo per avere piu' linee o aree, oppure \"\"."
    ),
    "categorie": (
        "x = categoria (testo), y = valore numerico, una riga per categoria (e per serie), al massimo "
        "15 categorie ordinate per y decrescente; serie = seconda categoria (testo) oppure \"\"."
    ),
    "matrice": (
        "x = prima categoria (testo o data), serie = seconda categoria (testo), y = valore numerico; "
        "una riga per coppia x/serie."
    ),
    "punti": (
        "una riga per elemento (al massimo 100): serie = etichetta dell'elemento (testo), x = primo valore "
        "numerico (asse orizzontale), y = secondo valore numerico (asse verticale), dimensione = terzo "
        "valore numerico per la grandezza della bolla oppure \"\"."
    ),
    "osservazioni": (
        "NON aggregata: UNA RIGA PER OSSERVAZIONE (es. una per ordine, per giorno o per cliente), al "
        "massimo 10000; y = valore numerico dell'osservazione, x = colonna che identifica l'osservazione "
        "(es. l'id dell'ordine) oppure \"\" se non c'e', serie = categoria (testo) per avere un box per "
        "categoria oppure \"\"."
    ),
    "dettaglio": "piu' colonne, al massimo 50 righe; x = \"\" e y = \"\".",
}


def descrizione_prompt(mese: str) -> str:
    """Elenco dei tipi per il prompt della dashboard, raggruppati per forma."""
    gruppi = [
        ("UN VALORE", "valore", [
            '"big_number" (KPI)',
            '"gauge" (indicatore a lancetta: percentuali, avanzamento verso un obiettivo)',
        ]),
        ("ANDAMENTO NEL TEMPO", "tempo", [
            '"line" (linee)',
            '"area" (aree; con la serie, aree impilate)',
            '"big_number_trend" (KPI con il valore dell\'ultimo periodo e la linea dell\'andamento; senza serie)',
        ]),
        ("CATEGORIE", "categorie", [
            '"bar" (barre verticali; con la serie, barre affiancate)',
            '"bar_horizontal" (barre orizzontali, per nomi lunghi o classifiche)',
            '"bar_stacked" (barre impilate: serie obbligatoria)',
            '"pie" e "donut" (composizione percentuale, al massimo 8 categorie, senza serie)',
            '"funnel" (fasi in sequenza, senza serie)',
            '"treemap" (proporzioni; la serie diventa il secondo livello)',
        ]),
        ("DUE CATEGORIE", "matrice", ['"heatmap" (mappa di calore)', '"pivot" (tabella pivot con totali)']),
        ("PUNTI", "punti", ['"bubble" (dispersione / bolle: relazione tra due misure)']),
        ("OSSERVAZIONI", "osservazioni", [
            '"box_plot" (Superset calcola mediana, quartili, media, minimo e massimo)',
            '"histogram" (distribuzione dei valori in fasce)',
        ]),
        ("DETTAGLIO", "dettaglio", ['"table" (elenco)']),
    ]
    righe = ["Tipi di grafico ammessi, raggruppati per forma dei dati che la query deve restituire:"]
    for i, (titolo, forma, tipi) in enumerate(gruppi, 1):
        righe.append(f"{i}. {titolo} - {_FORME[forma].format(mese=mese)}")
        righe.append("   Tipi: " + "; ".join(tipi) + ".")
    righe.append(
        "Scegli il tipo che risponde meglio alla richiesta. Se l'utente nomina un tipo di grafico "
        "(box plot, heatmap, imbuto, ciambella, pivot...) usa quello. Per evidenziare singoli valori "
        "(media, valore piu' alto, piu' basso) aggiungi KPI \"big_number\"."
    )
    return "\n".join(righe)


def forma_testo(tipo: str, mese: str) -> str:
    """Forma dei dati richiesta da un tipo, per le richieste di correzione al modello."""
    t = TIPI.get(tipo, TIPI["table"])
    testo = _FORME[t["forma"]].format(mese=mese)
    if t.get("serie") == "obbligatoria":
        testo += " Per questo tipo la serie e' obbligatoria."
    if t.get("serie") == "no" and t["forma"] in ("tempo", "categorie"):
        testo += " Per questo tipo non usare la serie."
    return testo


def _col(colonne: list[str], tipi: list[str], voluto: str, ammessi: set[str], escluse: set[str] = frozenset()) -> str:
    """La colonna indicata dal modello se ha un tipo ammesso, altrimenti la prima adatta."""
    if voluto in colonne and voluto not in escluse and tipi[colonne.index(voluto)] in ammessi:
        return voluto
    return next((c for c, t in zip(colonne, tipi) if t in ammessi and c not in escluse), "")


# Nomi che di solito indicano un identificativo, non una misura (valgono per ogni schema)
_ID = re.compile(r"(^id$|^id_|_id$|^cod|codice|matricola|^num(ero)?_|_key$|^pk_)", re.IGNORECASE)


def _valore(colonne: list[str], tipi: list[str], voluto: str, escluse: set[str] = frozenset()) -> str:
    """Colonna numerica da disegnare: quella indicata, altrimenti una che non sembri un identificativo."""
    if voluto in colonne and voluto not in escluse and tipi[colonne.index(voluto)] == "numerico":
        return voluto
    numeriche = [c for c, t in zip(colonne, tipi) if t == "numerico" and c not in escluse]
    return next((c for c in numeriche if not _ID.search(c)), numeriche[0] if numeriche else "")


def _massimo_gauge(valore: float) -> float:
    """Fondo scala dell'indicatore: 100 per le percentuali, altrimenti un valore 'tondo'."""
    if 0 <= valore <= 100:
        return 100
    potenza = 10 ** math.floor(math.log10(abs(valore)))
    return math.ceil(abs(valore) / potenza) * potenza


def adatta(tipo: str, g: dict[str, Any], colonne: list[str], tipi: list[str], righe: list) -> tuple[dict, str | None]:
    """Assegna le colonne del risultato ai campi del grafico e controlla la forma dei dati.
    Restituisce (campi, problema): problema e' None se i dati vanno bene per il tipo."""
    t = TIPI.get(tipo, TIPI["table"])
    forma, uso_serie, nome = t["forma"], t.get("serie", "no"), t["nome"]
    vx, vy, vd = (str(g.get(k) or "") for k in ("x", "y", "dimensione"))
    vs = str(g.get("serie") or g.get("gruppo") or "")
    campi: dict[str, Any] = {"x": "", "y": "", "serie": "", "dimensione": ""}
    n = len(righe)
    if n == 0:
        return campi, "La query non restituisce righe."
    if forma == "dettaglio":
        return campi, None

    if forma == "valore":
        y = _valore(colonne, tipi, vy)
        if n != 1 or not y:
            return campi, f"Per un {nome} serve 1 riga con un valore numerico, ma la query restituisce {n} righe."
        campi["y"] = y
        if tipo == "gauge":
            valore = righe[0][colonne.index(y)]
            campi["massimo"] = _massimo_gauge(float(valore)) if isinstance(valore, (int, float)) else 100
        return campi, None

    if forma == "punti":
        x = _valore(colonne, tipi, vx)
        y = _valore(colonne, tipi, vy, {x})
        d = _valore(colonne, tipi, vd, {x, y}) if vd else ""
        etichetta = _col(colonne, tipi, vs, {"testo"})
        campi.update(x=x, y=y, dimensione=d, serie=etichetta)
        if not (x and y and etichetta) or n < 2:
            return campi, (f"Per un {nome} servono una riga per elemento con un'etichetta di testo e almeno due "
                           f"colonne numeriche, ma la query restituisce {n} righe e colonne {', '.join(colonne)}.")
        return campi, None

    if forma == "osservazioni":
        y = _valore(colonne, tipi, vy)
        if not y or n < 5:
            campi["y"] = y
            return campi, (f"Per un {nome} serve una riga per osservazione (almeno 5) con un valore numerico, "
                           f"ma la query restituisce {n} righe e {len(colonne)} colonne.")

        def distinti(c: str) -> int:
            return len({r[colonne.index(c)] for r in righe})

        serie = vs if vs in colonne and vs != y and tipi[colonne.index(vs)] == "testo" else ""
        # identificativo: la colonna indicata o una dal nome adatto, purche' abbia un valore diverso
        # per riga; altrimenti nessuno e il box plot numera le righe (views.py)
        candidati = ([vx] if vx in colonne else []) + [c for c in colonne if _ID.search(c)]
        x = next((c for c in candidati if c not in (y, serie) and distinti(c) == n), "")
        if not serie:
            # una colonna di testo con valori ripetuti e' una categoria: un box per categoria
            serie = next(
                (c for c in ([vx] if vx in colonne else []) + colonne
                 if c not in (x, y) and tipi[colonne.index(c)] == "testo" and distinti(c) < n),
                "",
            )
        campi.update(x=x, y=y, serie=serie)
        if serie and distinti(serie) == n:
            return campi, (f"La query restituisce una sola riga per categoria: e' gia' aggregata. Per un {nome} "
                           "servono le singole osservazioni (es. una riga per ordine) con la loro categoria.")
        return campi, None

    # tempo, categorie, matrice
    y = _valore(colonne, tipi, vy)
    x = _col(colonne, tipi, vx, {"data"} if forma == "tempo" else {"testo", "data"}, {y})
    serie = ""
    if uso_serie == "obbligatoria":
        serie = _col(colonne, tipi, vs, {"testo"}, {x, y})
    elif uso_serie == "facoltativa" and vs:
        serie = _col(colonne, tipi, vs, {"testo"}, {x, y}) if vs in colonne else ""
    campi.update(x=x, y=y, serie=serie)
    if not y:
        return campi, f"Per un {nome} serve una colonna numerica con il valore da disegnare."
    if not x:
        cosa = "una colonna data (una riga per periodo)" if forma == "tempo" else "una colonna di categorie"
        return campi, f"Per un {nome} serve {cosa}, ma la query restituisce colonne {', '.join(colonne)}."
    if uso_serie == "obbligatoria" and not serie:
        return campi, f"Per un {nome} serve anche una seconda colonna di testo (la serie)."
    if n < 2:
        return campi, f"Per un {nome} servono piu' righe, ma la query ne restituisce {n}."
    return campi, None


def ripiego(colonne: list[str], tipi: list[str], n: int) -> tuple[str, dict]:
    """Tipo da usare se dopo le correzioni i dati non hanno ancora la forma richiesta."""
    numerica = next((c for c, t in zip(colonne, tipi) if t == "numerico"), "")
    if n == 1 and numerica:
        return "big_number", {"x": "", "y": numerica, "serie": "", "dimensione": ""}
    return "table", {"x": "", "y": "", "serie": "", "dimensione": ""}
