"""Differenze tra i motori SQL, raccolte in un solo posto.

Il resto dell'estensione lavora con il nome del dialetto di sqlglot ("postgres", "trino",
"tsql", "oracle"...), ricavato dall'indirizzo di connessione SQLAlchemy. Qui stanno:
- il nome leggibile del motore;
- le regole di sintassi da dare al modello e gli esempi (mese, medie, date costanti);
- i costrutti tipici di un dialetto che un altro non accetta (es. '::' fuori da PostgreSQL),
  da segnalare al modello prima di eseguire la query.

Un motore non elencato funziona lo stesso, con regole generiche: per aggiungerne uno basta
una voce in _REGOLE (e, se serve, in _MESE e _SQLALCHEMY).
"""

from __future__ import annotations

import re

# Nome del backend SQLAlchemy (make_url(uri).get_backend_name()) -> dialetto di sqlglot
_SQLALCHEMY = {
    "postgresql": "postgres",
    "trino": "trino",
    "presto": "presto",
    "mysql": "mysql",
    "mariadb": "mysql",
    "oracle": "oracle",
    "mssql": "tsql",
    "hive": "hive",
    "impala": "hive",
    "spark": "spark",
    "databricks": "databricks",
    "sqlite": "sqlite",
    "snowflake": "snowflake",
    "bigquery": "bigquery",
    "redshift": "redshift",
    "duckdb": "duckdb",
    "clickhousedb": "clickhouse",
    "clickhouse": "clickhouse",
    "awsathena": "athena",
    "teradatasql": "teradata",
    "doris": "doris",
    "starrocks": "starrocks",
}

_NOMI = {
    "postgres": "PostgreSQL",
    "trino": "Trino",
    "presto": "Presto",
    "mysql": "MySQL",
    "oracle": "Oracle",
    "tsql": "SQL Server (T-SQL)",
    "hive": "HiveQL",
    "spark": "Spark SQL",
    "databricks": "Databricks SQL",
    "sqlite": "SQLite",
    "snowflake": "Snowflake",
    "bigquery": "BigQuery",
    "redshift": "Amazon Redshift",
    "duckdb": "DuckDB",
    "clickhouse": "ClickHouse",
    "athena": "Amazon Athena",
}

# Espressione "primo giorno del mese" come DATE, con <colonna_data> come segnaposto
_MESE = {
    "postgres": "date_trunc('month', <colonna_data>)::date",
    "redshift": "date_trunc('month', <colonna_data>)::date",
    "trino": "CAST(date_trunc('month', <colonna_data>) AS DATE)",
    "presto": "CAST(date_trunc('month', <colonna_data>) AS DATE)",
    "athena": "CAST(date_trunc('month', <colonna_data>) AS DATE)",
    "mysql": "CAST(DATE_FORMAT(<colonna_data>, '%Y-%m-01') AS DATE)",
    "oracle": "TRUNC(<colonna_data>, 'MM')",
    "tsql": "DATEFROMPARTS(YEAR(<colonna_data>), MONTH(<colonna_data>), 1)",
    "hive": "CAST(trunc(<colonna_data>, 'MM') AS DATE)",
    "spark": "CAST(date_trunc('MONTH', <colonna_data>) AS DATE)",
    "databricks": "CAST(date_trunc('MONTH', <colonna_data>) AS DATE)",
    "sqlite": "date(<colonna_data>, 'start of month')",
    "snowflake": "CAST(date_trunc('month', <colonna_data>) AS DATE)",
    "bigquery": "DATE_TRUNC(<colonna_data>, MONTH)",
    "duckdb": "CAST(date_trunc('month', <colonna_data>) AS DATE)",
    "clickhouse": "toStartOfMonth(<colonna_data>)",
}
_MESE_GENERICO = "CAST(date_trunc('month', <colonna_data>) AS DATE)"

# Regole di sintassi per il modello. {mese} e {data} vengono sostituiti.
_REGOLE = {
    "postgres": """- Dialetto PostgreSQL.
- Arrotonda importi e medie con ROUND(espressione::numeric, 2).
- Per andamenti nel tempo raggruppa per mese con {mese} AS mese e ordina per mese.
- Le date costanti si scrivono {data}.""",
    "trino": """- Dialetto Trino (NON PostgreSQL). Le tabelle si indicano come <schema>.<nome> (con lo schema indicato nello SCHEMA DEL DATABASE) oppure solo <nome>.
- Niente cast con '::': usa sempre CAST(espressione AS tipo).
- Le date costanti si scrivono SEMPRE {data}: una colonna date non si confronta con una stringa.
- Niente ILIKE: per confronti senza maiuscole usa lower(colonna) LIKE lower('%testo%').
- Intervalli: INTERVAL '3' MONTH (numero tra apici, unita' fuori), oppure date_add('month', -3, data).
- In Trino la divisione tra interi e' intera: per percentuali e rapporti usa 100.0 * ... oppure CAST(... AS DOUBLE).
- Arrotonda importi e medie con ROUND(espressione, 2).
- Per andamenti nel tempo raggruppa per mese con {mese} AS mese e ordina per mese.
- Gli identificatori vanno tra doppi apici solo se servono; le stringhe tra apici singoli.""",
    "mysql": """- Dialetto MySQL (NON PostgreSQL).
- Niente cast con '::': usa CAST(espressione AS tipo) (tipi: SIGNED, DECIMAL(18,2), DATE, CHAR).
- Niente ILIKE: per confronti senza maiuscole usa lower(colonna) LIKE lower('%testo%').
- Niente FILTER (WHERE ...) negli aggregati: usa SUM(CASE WHEN ... THEN 1 ELSE 0 END).
- Intervalli: DATE_SUB(data, INTERVAL 3 MONTH) oppure data - INTERVAL 3 MONTH.
- Le date costanti si scrivono {data}.
- Arrotonda con ROUND(espressione, 2). Limita le righe con LIMIT n.
- Per andamenti nel tempo raggruppa per mese con {mese} AS mese e ordina per mese.
- Gli identificatori vanno tra backtick (`nome`) solo se servono.""",
    "oracle": """- Dialetto Oracle (NON PostgreSQL).
- Niente LIMIT: usa ORDER BY ... FETCH FIRST n ROWS ONLY.
- Niente cast con '::': usa CAST(espressione AS tipo).
- Niente ILIKE: usa LOWER(colonna) LIKE LOWER('%testo%').
- Niente FILTER (WHERE ...) negli aggregati: usa SUM(CASE WHEN ... THEN 1 ELSE 0 END).
- Gli alias delle tabelle si scrivono senza AS (FROM vendite v).
- Intervalli: ADD_MONTHS(data, -3) oppure data - INTERVAL '30' DAY.
- Le date costanti si scrivono {data}. La stringa vuota '' equivale a NULL.
- Per andamenti nel tempo raggruppa per mese con {mese} AS mese e ordina per mese.""",
    "tsql": """- Dialetto SQL Server / T-SQL (NON PostgreSQL).
- Niente LIMIT: usa SELECT TOP n ... ORDER BY ... .
- Niente cast con '::': usa CAST(espressione AS tipo).
- Niente FILTER (WHERE ...) negli aggregati: usa SUM(CASE WHEN ... THEN 1 ELSE 0 END).
- Le date costanti si scrivono {data}.
- Intervalli: DATEADD(month, -3, data).
- La divisione tra interi e' intera: per percentuali e rapporti usa 100.0 * ... .
- Per andamenti nel tempo raggruppa per mese con {mese} AS mese e ordina per mese (ORDER BY nella query esterna).
- Gli identificatori vanno tra parentesi quadre ([nome]) solo se servono.""",
    "hive": """- Dialetto HiveQL (NON PostgreSQL).
- Niente cast con '::': usa CAST(espressione AS tipo).
- Niente ILIKE: usa lower(colonna) LIKE lower('%testo%').
- Niente FILTER (WHERE ...) negli aggregati: usa SUM(CASE WHEN ... THEN 1 ELSE 0 END).
- Intervalli: add_months(data, -3) oppure date_sub(data, 30).
- Le date costanti si scrivono {data}.
- Le colonne in ORDER BY devono comparire nel SELECT (usa gli alias).
- Per andamenti nel tempo raggruppa per mese con {mese} AS mese e ordina per mese.
- Gli identificatori vanno tra backtick (`nome`) solo se servono.""",
    "spark": """- Dialetto Spark SQL (NON PostgreSQL).
- Niente cast con '::': usa CAST(espressione AS tipo).
- Niente FILTER (WHERE ...) negli aggregati: usa SUM(CASE WHEN ... THEN 1 ELSE 0 END).
- Intervalli: add_months(data, -3) oppure data - INTERVAL 3 MONTHS.
- Le date costanti si scrivono {data}.
- Per andamenti nel tempo raggruppa per mese con {mese} AS mese e ordina per mese.
- Gli identificatori vanno tra backtick (`nome`) solo se servono.""",
    "sqlite": """- Dialetto SQLite (NON PostgreSQL).
- Le date sono testo 'aaaa-mm-gg': si confrontano come stringhe, es. data BETWEEN '2026-01-01' AND '2026-03-31'.
- Intervalli: date(data, '-3 months').
- Niente ILIKE (LIKE e' gia' senza distinzione tra maiuscole e minuscole) e niente cast con '::'.
- La divisione tra interi e' intera: per percentuali e rapporti usa 100.0 * ... .
- Per andamenti nel tempo raggruppa per mese con {mese} AS mese e ordina per mese.""",
}
_REGOLE["presto"] = _REGOLE["trino"].replace("Trino", "Presto")
_REGOLE["athena"] = _REGOLE["trino"].replace("Trino", "Athena")
_REGOLE["databricks"] = _REGOLE["spark"].replace("Spark SQL", "Databricks SQL")
_REGOLE_GENERICHE = """- Dialetto {nome}: usa solo sintassi e funzioni di {nome}.
- Le date costanti si scrivono {data}.
- Per andamenti nel tempo raggruppa per mese con {mese} AS mese e ordina per mese."""

# Dialetti che arrotondano un AVG solo dopo un cast (ROUND(double, 2) non esiste in PostgreSQL)
_CAST_ROUND = {"postgres": "::numeric", "redshift": "::numeric"}

# Costrutti validi solo in alcuni dialetti: (espressione regolare, dialetti dove vale, correzione)
_DOVE_VALE_CAST = {"postgres", "redshift", "snowflake", "duckdb", "databricks", "clickhouse"}
_DOVE_VALE_ILIKE = {"postgres", "redshift", "snowflake", "duckdb", "databricks", "clickhouse", "spark"}
_DOVE_VALE_INTERVAL_PG = {"postgres", "redshift", "duckdb", "snowflake"}
_INTERVALLI = {
    "trino": "INTERVAL '3' MONTH (numero tra apici, unita' fuori)",
    "presto": "INTERVAL '3' MONTH (numero tra apici, unita' fuori)",
    "athena": "INTERVAL '3' MONTH (numero tra apici, unita' fuori)",
    "oracle": "ADD_MONTHS(data, -3) oppure INTERVAL '3' MONTH",
    "mysql": "DATE_SUB(data, INTERVAL 3 MONTH)",
    "tsql": "DATEADD(month, -3, data)",
    "hive": "add_months(data, -3)",
    "spark": "add_months(data, -3)",
    "databricks": "add_months(data, -3)",
    "sqlite": "date(data, '-3 months')",
    "bigquery": "DATE_SUB(data, INTERVAL 3 MONTH)",
}
# Dialetti in cui una colonna date NON si confronta con una stringa '2026-01-01'
_DATE_RIGIDE = {"trino", "presto", "athena"}


def da_backend(backend: str) -> str:
    """Dialetto sqlglot dal nome del backend SQLAlchemy ("" se sconosciuto: sqlglot generico)."""
    backend = (backend or "").lower()
    return _SQLALCHEMY.get(backend, _SQLALCHEMY.get(backend.split("+")[0], ""))


def nome(dialetto: str) -> str:
    return _NOMI.get(dialetto, dialetto or "SQL standard")


def data_letterale(iso: str, dialetto: str) -> str:
    """Una data costante (es. '2026-08-01') scritta nel modo che il dialetto accetta."""
    if dialetto == "tsql":
        return f"CAST('{iso}' AS DATE)"
    if dialetto == "sqlite":
        return f"'{iso}'"
    return f"DATE '{iso}'"


def mese(dialetto: str) -> str:
    """Espressione del primo giorno del mese, come esempio per il modello."""
    return _MESE.get(dialetto, _MESE_GENERICO)


def esempi(dialetto: str) -> dict[str, str]:
    """Regole e query d'esempio (con segnaposto tra < >) per il prompt."""
    m = mese(dialetto)
    data = data_letterale("aaaa-mm-gg", dialetto)
    regole = _REGOLE.get(dialetto, _REGOLE_GENERICHE).format(mese=m, data=data, nome=nome(dialetto))
    cast = _CAST_ROUND.get(dialetto, "")
    alias = "" if dialetto == "oracle" else "AS "
    return {
        "nome": nome(dialetto),
        "regole": regole,
        "mese": f"{m} AS mese",
        "data": data,
        "media": (
            f"SELECT ROUND(AVG(totale){cast}, 2) AS media_per_entita FROM (SELECT <id_entita>, "
            f"SUM(<valore>) AS totale FROM <tabella> GROUP BY <id_entita>) {alias}t"
        ),
        "media_mese": (
            f"SELECT ROUND(AVG(totale){cast}, 2) AS media_mensile FROM (SELECT {m} AS mese, "
            f"SUM(<valore>) AS totale FROM <tabella> GROUP BY {m}) {alias}t"
        ),
    }


_STRINGHE = re.compile(r"'(?:[^']|'')*'")
_DATA_NUDA = re.compile(r"(?<![A-Za-z_])'\d{4}-\d{2}-\d{2}'")
# Una data tra apici e' un errore solo se e' confrontata direttamente (>=, =, BETWEEN ... AND ...):
# DATE '...', TIMESTAMP '...' e CAST('...' AS DATE) sono validi.
_CONFRONTO_PRIMA = re.compile(r"(?i)(<=|>=|<>|!=|=|<|>|\bbetween|\band)\s*$")
_CONFRONTO_DOPO = re.compile(r"^\s*(<=|>=|<>|!=|=|<|>)")


def errori_sintassi(sql: str, dialetto: str) -> str | None:
    """Costrutti di altri dialetti (soprattutto PostgreSQL) che questo motore non accetta.
    Si segnalano al modello con la correzione giusta prima di eseguire: l'errore del database
    sarebbe meno chiaro."""
    if not dialetto:
        return None
    n = nome(dialetto)
    senza_stringhe = _STRINGHE.sub("''", sql)
    problemi = []
    if dialetto not in _DOVE_VALE_CAST and "::" in senza_stringhe:
        problemi.append(f"il cast con '::' non esiste in {n}: usa CAST(espressione AS tipo)")
    if dialetto not in _DOVE_VALE_ILIKE and re.search(r"(?i)\bilike\b", senza_stringhe):
        problemi.append(f"ILIKE non esiste in {n}: usa lower(colonna) LIKE lower('%testo%')")
    if dialetto not in _DOVE_VALE_INTERVAL_PG and re.search(r"(?i)\binterval\s*'\s*\d+\s+[a-z]+", sql):
        forma = _INTERVALLI.get(dialetto, "la sintassi degli intervalli del motore")
        problemi.append(f"in {n} gli intervalli si scrivono {forma}")
    if dialetto in _DATE_RIGIDE:
        for m in _DATA_NUDA.finditer(sql):
            if _CONFRONTO_PRIMA.search(sql[: m.start()]) or _CONFRONTO_DOPO.match(sql[m.end():]):
                problemi.append(
                    f"la data {m.group(0)} e' una stringa: in {n} una colonna date si confronta solo "
                    f"con un valore date, scrivi DATE {m.group(0)}"
                )
                break
    if not problemi:
        return None
    return f"La query usa sintassi non valida in {n}: " + "; ".join(problemi) + "."
