#!/bin/sh
# Inject runtime configuration into the built index.html.
#
# The bundle is built once and reads window.__SMARTTENDER_API__ / _API_KEY_ at
# runtime, so the same image serves any environment. Here we point the client
# at nginx's own /api proxy (same-origin) and pass through the API key.
set -eu

API_BASE="${SMARTTENDER_UI_API_BASE:-/api}"
API_KEY="${SMARTTENDER_UI_API_KEY:-dev-local-key}"

INDEX=/usr/share/nginx/html/index.html

# Replace the two window globals. Escape ampersands/slashes for sed safety.
esc() { printf '%s' "$1" | sed -e 's/[&/\]/\\&/g'; }

sed -i \
  -e "s/window.__SMARTTENDER_API__ = \"[^\"]*\"/window.__SMARTTENDER_API__ = \"$(esc "$API_BASE")\"/" \
  -e "s/window.__SMARTTENDER_API_KEY__ = \"[^\"]*\"/window.__SMARTTENDER_API_KEY__ = \"$(esc "$API_KEY")\"/" \
  "$INDEX"

echo "smarttender-ui: API base=$API_BASE"
