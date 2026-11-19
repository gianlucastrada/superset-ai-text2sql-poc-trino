#!/bin/bash
# Inizializzazione di Superset: eseguita dall'initContainer a ogni avvio del pod.
# Tutti i passi sono idempotenti (si possono ripetere senza danni).
set -euo pipefail

echo "==> Attendo che Postgres sia pronto"
for i in $(seq 1 60); do
  if python -c "import os, sqlalchemy as sa; sa.create_engine(os.environ['SUPERSET_DATABASE_URI']).connect().close()" 2>/dev/null; then
    break
  fi
  if [ "$i" -eq 60 ]; then echo "Postgres non raggiungibile dopo 3 minuti"; exit 1; fi
  sleep 3
done

echo "==> Aggiornamento schema dei metadati"
superset db upgrade

echo "==> Utente amministratore"
superset fab create-admin \
  --username "${ADMIN_USERNAME:-admin}" \
  --firstname Admin --lastname POC \
  --email "${ADMIN_EMAIL:-admin@example.com}" \
  --password "${ADMIN_PASSWORD}" || echo "(utente gia' presente)"

echo "==> Ruoli e permessi"
superset init

echo "==> Connessione al database dei dati: ${AI_DATABASE_NAME}"
superset set-database-uri -d "${AI_DATABASE_NAME}" -u "${AI_DATA_URI}"

# Trino e' facoltativo: deploy.ps1 lascia vuota la variabile se Trino non e' attivo
if [ -n "${AI_TRINO_SUPERSET_URI:-}" ]; then
  echo "==> Connessione a Trino: ${AI_TRINO_DATABASE_NAME:-Vendite via Trino}"
  superset set-database-uri -d "${AI_TRINO_DATABASE_NAME:-Vendite via Trino}" -u "${AI_TRINO_SUPERSET_URI}"
fi

# Esecuzione asincrona in SQL Lab solo se c'e' il worker Celery (deploy.ps1 -Asincrono):
# senza worker le query asincrone resterebbero in attesa per sempre.
echo "==> Esecuzione asincrona in SQL Lab: ${SUPERSET_ASINCRONO:-false}"
python - <<'EOF'
import os

from superset.app import create_app

app = create_app()
with app.app_context():
    from superset.extensions import db
    from superset.models.core import Database

    attiva = os.environ.get("SUPERSET_ASINCRONO", "false").lower() == "true"
    nomi = [os.environ.get("AI_DATABASE_NAME", ""), os.environ.get("AI_TRINO_DATABASE_NAME", "")]
    for nome in [n for n in nomi if n]:
        rec = db.session.query(Database).filter_by(database_name=nome).one_or_none()
        if rec is not None:
            rec.allow_run_async = attiva
            print(f"    {nome}: asincrona={attiva}")
    db.session.commit()
EOF

echo "==> Inizializzazione completata"
