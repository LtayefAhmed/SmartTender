#!/usr/bin/env bash
# Pre-demo check: is every screen actually reachable from the browser?
#
# Docker Desktop on Windows forwards published ports through a relay
# (`wslrelay`) that intermittently loses its binding when containers are
# recreated in quick succession. The container stays healthy and serves
# correctly on its own network, so `docker compose ps` shows nothing wrong —
# the browser simply gets ERR_EMPTY_RESPONSE. Restarting the service rebinds it.
#
# This checks each published port the way a browser would, and repairs what is
# broken. Run it before a demo.
#
#   ./scripts/preflight.sh          check and repair
#   ./scripts/preflight.sh --check  report only, exit 1 if anything is down
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

# service:port:path:label — the URL is what matters, not the container state.
TARGETS=(
  "frontend:3000:/:Interface"
  "frontend:3000:/api/health:Proxy API (nginx -> api)"
  "api:8000:/health:API"
  "mailpit:8025:/:Mailpit (emails)"
  "minio:9001:/:MinIO (console)"
  "flower:5555:/:Flower (files Celery)"
  "prometheus:9090:/-/ready:Prometheus"
  "grafana:3001:/api/health:Grafana"
)

probe() {  # port path -> HTTP code ("000" = no response at all)
  curl -s -m 6 -o /dev/null -w "%{http_code}" "http://127.0.0.1:$1$2" 2>/dev/null
}

failed=0
repaired=0

for target in "${TARGETS[@]}"; do
  IFS=: read -r service port path label <<<"$target"
  code=$(probe "$port" "$path")

  # 2xx/3xx/404 all prove the port is bound and something is answering; only a
  # complete non-response means the forwarding is broken.
  if [ "$code" != "000" ]; then
    printf "  \033[32mOK\033[0m    %-28s %s\n" "$label" "http://localhost:$port"
    continue
  fi

  if [ "$CHECK_ONLY" -eq 1 ]; then
    printf "  \033[31mDOWN\033[0m  %-28s port %s ne répond pas\n" "$label" "$port"
    failed=$((failed + 1))
    continue
  fi

  printf "  \033[33m..\033[0m    %-28s port %s muet, redémarrage de '%s'\n" "$label" "$port" "$service"
  docker compose restart "$service" >/dev/null 2>&1
  sleep 8
  code=$(probe "$port" "$path")

  if [ "$code" != "000" ]; then
    printf "  \033[32mOK\033[0m    %-28s réparé\n" "$label"
    repaired=$((repaired + 1))
  else
    printf "  \033[31mKO\033[0m    %-28s toujours muet — voir: docker compose logs %s\n" "$label" "$service"
    failed=$((failed + 1))
  fi
done

echo
[ "$repaired" -gt 0 ] && echo "$repaired service(s) réparé(s)."
if [ "$failed" -gt 0 ]; then
  echo "$failed service(s) indisponible(s)."
  exit 1
fi
echo "Tout est joignable depuis le navigateur."
