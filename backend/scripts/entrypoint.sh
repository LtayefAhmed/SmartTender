#!/usr/bin/env sh
# =============================================================================
# Optional container entrypoint.
#
# The compose stack runs migrations as a separate one-shot service, which is
# the correct pattern: N replicas starting together would otherwise run
# `alembic upgrade` N times concurrently.
#
# Use this script only where an orchestrator cannot express a pre-deploy job.
# It serialises on a PostgreSQL advisory lock so concurrent replicas are safe.
# =============================================================================
set -eu

ROLE="${1:-api}"

wait_for() {
    host="$1"; port="$2"; label="$3"; attempts=0
    until python - "$host" "$port" <<'PY'
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
with socket.create_connection((host, port), timeout=2):
    pass
PY
    do
        attempts=$((attempts + 1))
        if [ "$attempts" -ge 60 ]; then
            echo "FATAL: $label at $host:$port never became reachable." >&2
            exit 1
        fi
        echo "waiting for $label at $host:$port ($attempts/60)..."
        sleep 2
    done
}

wait_for "${SMARTTENDER_DB__HOST:-postgres}" "${SMARTTENDER_DB__PORT:-5432}" "PostgreSQL"
wait_for "${SMARTTENDER_REDIS__HOST:-redis}" "${SMARTTENDER_REDIS__PORT:-6379}" "Redis"

if [ "${RUN_MIGRATIONS:-false}" = "true" ]; then
    echo "applying migrations..."
    alembic upgrade head
fi

case "$ROLE" in
    api)
        exec uvicorn app.main:app --host 0.0.0.0 --port "${SMARTTENDER_API__PORT:-8000}" \
             --proxy-headers
        ;;
    worker)
        exec celery -A app.workers.celery_app:celery_app worker \
             --queues "${CELERY_QUEUES:-scraping,parsing,scoring,notifications,maintenance,default}" \
             --concurrency "${CELERY_CONCURRENCY:-4}" \
             --hostname "${CELERY_HOSTNAME:-worker@%h}" \
             --loglevel "${CELERY_LOGLEVEL:-INFO}"
        ;;
    beat)
        # Exactly one of these must ever run: two would double-fire schedules.
        exec celery -A app.workers.celery_app:celery_app beat \
             --loglevel "${CELERY_LOGLEVEL:-INFO}"
        ;;
    *)
        exec "$@"
        ;;
esac
