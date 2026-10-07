#!/usr/bin/env bash
# End-to-end smoke test against a running stack: create key -> create link -> redirect -> stats.
set -euo pipefail
BASE="${1:-http://localhost:8000}"
KEY="$(docker compose exec -T service python -m service.cli create-key --name smoke 2>/dev/null | head -1)"
echo "readyz:   $(curl -fsS "$BASE/readyz")"
CREATE="$(curl -fsS -X POST "$BASE/api/v1/links" -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"url": "https://example.com/smoke", "custom_alias": "smoke-test"}')"
echo "create:   $CREATE"
for i in 1 2 3; do
  curl -s -o /dev/null -w "redirect: %{http_code} -> %{redirect_url}\n" -A "smoke/$i" -e "https://ref.example/" "$BASE/Smoke-Test"
done
sleep 1  # clicks are written off the hot path
echo "stats:    $(curl -fsS "$BASE/api/v1/links/smoke-test/stats")"
echo "metrics:  $(curl -fsS "$BASE/metrics" | grep -E '^redirects_total' | head -1)"
