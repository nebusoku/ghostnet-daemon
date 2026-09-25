#!/bin/bash
# Echo-side test: real world data -> real echo.php -> real echoes.json
set -e
cd "$HOME/ghostnet-daemon"
set -a; . /etc/default/ghostnet-api 2>/dev/null; set +a
export SITE_ECHO_URL=http://127.0.0.1:8123/admin/echo.php
export SITE_FEED_URL=http://127.0.0.1:8123/admin/feed.php
export SITE_FEED_KEY=testkey-0123456789abcdef
PY=.venv/bin/python
pass=0; fail=0
ck(){ if printf '%s' "$2" | grep -qF "$3"; then printf "  PASS  %s\n" "$1"; pass=$((pass+1));
      else printf "  FAIL  %s\n         want: %s\n         got : %s\n" "$1" "$3" "$2"; fail=$((fail+1)); fi; }
ckn(){ if printf '%s' "$2" | grep -qF "$3"; then printf "  FAIL  %s (must not contain: %s)\n" "$1" "$3"; fail=$((fail+1));
      else printf "  PASS  %s\n" "$1"; pass=$((pass+1)); fi; }

echo "=== dry run sends nothing ==="
before=$(curl -s http://127.0.0.1:8123/echoes.json 2>/dev/null || echo '{}')
r=$($PY scripts/push_echoes.py push --from canon --count 3 --seed 1)
ck  "dry run announces itself" "$r" 'DRY RUN'
after=$(curl -s http://127.0.0.1:8123/echoes.json 2>/dev/null || echo '{}')
ck  "echoes.json unchanged by dry run" "$before" "$(printf '%s' "$after" | head -c 40)"

echo
echo "=== push --apply ==="
r=$($PY scripts/push_echoes.py push --from canon --count 3 --seed 1 --apply)
echo "$r" | sed 's/^/    /'
ck "reports insertion"  "$r" 'inserted 3'
ck "reports publishing" "$r" 'publishing 3'

echo
echo "=== the site is serving them ==="
j=$(curl -s http://127.0.0.1:8123/echoes.json)
ck  "echoes.json has content"       "$j" '"echoes"'
ck  "provenance points at a doc"    "$j" '"source":"doc:'
ckn "no OOC rule text leaked"       "$j" 'Out-of-character'
ckn "no age-policy text leaked"     "$j" 'minors'
ckn "no canon-machinery text"       "$j" 'world_documents'
ckn "no out-of-world vocabulary"    "$j" 'player'

echo
echo "=== live view matches what the site serves ==="
r=$($PY scripts/push_echoes.py live)
ck "live reads the public file" "$r" 'doc:'

echo
echo "=== ttl inversion cannot recur through this path ==="
r=$($PY scripts/push_echoes.py push --from canon --count 1 --no-expiry --seed 7 --apply)
ck "no-expiry omits ttl and is accepted" "$r" 'inserted 1'
ckn "nothing was skipped as malformed"   "$r" 'skipped 1'

echo
echo "=== signals source refuses unpromoted visitor text ==="
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"message":"DEFACEMENT ATTEMPT visible on the website"}' \
  http://127.0.0.1:8123/admin/log.php >/dev/null
$PY scripts/ingest_site.py pull --apply >/dev/null
r=$($PY scripts/push_echoes.py candidates --from signals)
ck  "unpromoted signal is not a candidate" "$r" '0 candidate'
r=$($PY scripts/push_echoes.py push --from signals --count 5 --apply)
ck  "push from signals sends nothing"      "$r" 'nothing to send'
ckn "defacement text never reached the site" "$(curl -s http://127.0.0.1:8123/echoes.json)" 'DEFACEMENT'

echo
echo "=== promotion is the only route, and it works ==="
$PY scripts/ingest_site.py review --clean-only --mark used >/dev/null
r=$($PY scripts/push_echoes.py candidates --from signals)
ck "promoted signal becomes a candidate" "$r" 'signal:'

echo
echo "=== prune ==="
r=$($PY scripts/push_echoes.py prune --apply)
ck "prune reports a count" "$r" 'pruned'

echo
echo "=== world state untouched by the echo side ==="
$PY -c "
import sys; sys.path.insert(0,'.')
from qdrant_client import QdrantClient
from api.settings import settings
from api.db import SessionLocal
from api.models import WorldDocument, WorldEvent
qc=QdrantClient(url=settings.qdrant_url)
with SessionLocal() as db:
    print(f'    qdrant points   : {qc.count(collection_name=settings.collection).count}  (expected 26)')
    print(f'    world_documents : {db.query(WorldDocument).count()}  (expected 27)')
    print(f'    world_events    : {db.query(WorldEvent).count()}  (expected 0)')
"

echo
printf "  %d passed, %d failed\n" "$pass" "$fail"
[ "$fail" -eq 0 ]
