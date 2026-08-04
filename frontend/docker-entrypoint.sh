#!/bin/sh
# Inject runtime configuration into the built index.html.
#
# The bundle is built once and reads window.__SMARTTENDER_API__ / _API_KEY_ at
# runtime, so the same image serves any environment. Here we point the client
# at nginx's own /api proxy (same-origin) and pass through the API key.
set -eu

API_BASE="${SMARTTENDER_UI_API_BASE:-/api}"
API_KEY="${SMARTTENDER_UI_API_KEY:-dev-local-key}"
# Must match a seeded notification profile
# (`smarttender-admin seed --user <id>`), or the Notifications screen
# stays empty while alerts accumulate for a different identity.
USER_ID="${SMARTTENDER_UI_USER_ID:-operator}"

INDEX=/usr/share/nginx/html/index.html

# Replace the runtime globals. Escape ampersands/slashes for sed safety.
esc() { printf '%s' "$1" | sed -e 's/[&/\]/\\&/g'; }

sed -i \
  -e "s/window.__SMARTTENDER_API__ = \"[^\"]*\"/window.__SMARTTENDER_API__ = \"$(esc "$API_BASE")\"/" \
  -e "s/window.__SMARTTENDER_API_KEY__ = \"[^\"]*\"/window.__SMARTTENDER_API_KEY__ = \"$(esc "$API_KEY")\"/" \
  -e "s/window.__SMARTTENDER_USER_ID__ = \"[^\"]*\"/window.__SMARTTENDER_USER_ID__ = \"$(esc "$USER_ID")\"/" \
  "$INDEX"

echo "smarttender-ui: API base=$API_BASE user=$USER_ID"
