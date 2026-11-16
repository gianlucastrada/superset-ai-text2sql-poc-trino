# Superset 6.1.0 ufficiale + driver Postgres e Trino + estensione AI (Text-to-SQL con Ollama).
# Build:  docker build -t superset-ai:0.1 .\superset
ARG SUPERSET_VERSION=6.1.0
FROM apache/superset:${SUPERSET_VERSION}

USER root

# Driver Postgres e Trino (l'immagine ufficiale "lean" non li include), come da documentazione Superset
RUN . /app/.venv/bin/activate && uv pip install --no-cache psycopg2-binary "trino>=0.330"

# Configurazione ed estensione: /app/pythonpath e' gia' nel PYTHONPATH dell'immagine
COPY --chown=superset:superset superset_config.py /app/pythonpath/superset_config.py
COPY --chown=superset:superset superset_ai /app/pythonpath/superset_ai
COPY --chown=superset:superset --chmod=755 init-superset.sh /app/pythonpath/init-superset.sh
# Se il file e' stato salvato su Windows con fine riga CRLF, bash non lo eseguirebbe
RUN sed -i 's/\r$//' /app/pythonpath/init-superset.sh

USER superset
