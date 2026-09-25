#!/bin/sh
# End-to-end test of the site data layer: real Apache, real MySQL, real HTTP.
B=http://127.0.0.1:8123
K=testkey-0123456789abcdef
MY="docker exec gmysql mysql -h127.0.0.1 -ughost -pghostpw ghost"
pass=0; fail=0
ck() {
  if printf '%s' "$2" | grep -qF "$3"; then
    printf "  PASS  %s\n" "$1"; pass=$((pass+1))
  else
    printf "  FAIL  %s\n         want: %s\n         got : %s\n" "$1" "$3" "$2"; fail=$((fail+1))
  fi
}
ckn() {
  if printf '%s' "$2" | grep -qF "$3"; then
    printf "  FAIL  %s\n         must not contain: %s\n" "$1" "$3"; fail=$((fail+1))
  else
    printf "  PASS  %s\n" "$1"; pass=$((pass+1))
  fi
}

echo "--- endpoint auth ---"
ck "feed rejects missing key"  "$(curl -s "$B/admin/feed.php?since=0")" 'Forbidden'
ck "feed rejects wrong key"    "$(curl -s -H "X-Ghost-Key: wrong" "$B/admin/feed.php?since=0")" 'Forbidden'
ck "echo rejects wrong key"    "$(curl -s -X POST -H "X-Ghost-Key: wrong" -d '{}' "$B/admin/echo.php")" 'Forbidden'
ck "echo rejects GET"          "$(curl -s "$B/admin/echo.php")" 'Method not allowed'
ck "feed accepts good key"     "$(curl -s -H "X-Ghost-Key: $K" "$B/admin/feed.php?since=0")" '"ok":true'

echo "--- log.php -> mysql ---"
r=$(curl -s -X POST -H 'Content-Type: application/json' -H 'Referer: http://x/undercroft/' -d '{"message":"who is watching"}' "$B/admin/log.php")
ck  "log accepts a message"     "$r" '"ok":true'
ckn "log used db, not fallback" "$r" 'degraded'
curl -s -X POST -d '{"message":"ghost-prime"}' "$B/admin/log.php" >/dev/null
curl -s -X POST -H 'Content-Type: application/json' -d '{"message":"drift ☺ 👻"}' "$B/admin/log.php" >/dev/null
ck "empty message rejected" "$(curl -s -X POST -d '{"message":"  "}' "$B/admin/log.php")" 'Empty message'

echo "--- feed: cursor, paging, content ---"
r=$(curl -s -H "X-Ghost-Key: $K" "$B/admin/feed.php?since=0")
ck  "returns the rows"            "$r" 'who is watching'
ck  "page captured from referer"  "$r" '/undercroft/'
ck  "utf8mb4 survived round trip" "$r" '👻'
ck  "last_id advances to 3"       "$r" '"last_id":3'
ck  "more=false on a short page"  "$r" '"more":false'
ckn "no raw IP in feed output"    "$r" '"ip"'
r=$(curl -s -H "X-Ghost-Key: $K" "$B/admin/feed.php?since=0&limit=2")
ck  "limit honoured, more=true"   "$r" '"more":true'
ck  "paged last_id is 2"          "$r" '"last_id":2'
ck  "watermark excludes consumed" "$(curl -s -H "X-Ghost-Key: $K" "$B/admin/feed.php?since=3")" '"rows":[]'

echo "--- echo push + static publish ---"
r=$(curl -s -X POST -H "X-Ghost-Key: $K" -H 'Content-Type: application/json' -d '{"echoes":[{"body":"the relay went quiet at 03:12","weight":3,"source":"world_event:91"},{"body":"a debt left standing","weight":1},{"body":"   "}]}' "$B/admin/echo.php")
ck "inserted 2, skipped empty body" "$r" '"inserted":2'
ck "empty body counted as skipped"  "$r" '"skipped":1'
ck "published to static file"       "$r" '"published":2'
r=$(curl -s "$B/echoes.json")
ck "echoes.json readable"    "$r" 'the relay went quiet'
ck "carries provenance"      "$r" 'world_event:91'
ck "weight preserved as int" "$r" '"weight":3'
ck "has generated timestamp" "$r" 'generated'

echo "--- ttl + prune ---"
r=$(curl -s -X POST -H "X-Ghost-Key: $K" -H 'Content-Type: application/json' -d '{"echoes":[{"body":"already stale","ttl_hours":-5}]}' "$B/admin/echo.php")
ck  "non-positive ttl refused, not made permanent" "$r" '"inserted":0'
ck  "refusal reported as skipped"                  "$r" '"skipped":1'
ckn "refused echo never published"                 "$(curl -s "$B/echoes.json")" 'already stale'
r=$(curl -s -X POST -H "X-Ghost-Key: $K" -H 'Content-Type: application/json' -d '{"echoes":[{"body":"short lived","ttl_hours":1}]}' "$B/admin/echo.php")
ck  "positive ttl accepted"      "$r" '"inserted":1'
ck  "live ttl echo is published" "$(curl -s "$B/echoes.json")" 'short lived'
$MY -e "UPDATE echoes SET expires_at = NOW() - INTERVAL 1 HOUR WHERE body = 'short lived';" 2>/dev/null
r=$(curl -s -X POST -H "X-Ghost-Key: $K" -H 'Content-Type: application/json' -d '{"prune":true}' "$B/admin/echo.php")
ck  "prune removes a genuinely expired row" "$r" '"pruned":1'
ckn "expired echo dropped from static file" "$(curl -s "$B/echoes.json")" 'short lived'
ck  "live echoes survived prune"            "$(curl -s "$B/echoes.json")" 'the relay went quiet'

echo "--- exposure ---"
ck  "jsonl denied under /admin" "$(curl -s -o /dev/null -w '%{http_code}' "$B/admin/ghost_console_log.jsonl")" '403'
ck  "admin page loads"          "$(curl -s -o /dev/null -w '%{http_code}' "$B/admin/")" '200'
ck  "echoes.json still served"  "$(curl -s -o /dev/null -w '%{http_code}' "$B/echoes.json")" '200'
ckn "admin page hides password" "$(curl -s "$B/admin/")" 'What-is-a-password'

echo
printf "  %d passed, %d failed\n" "$pass" "$fail"
[ "$fail" -eq 0 ]
