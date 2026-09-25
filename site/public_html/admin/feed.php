<?php
/**
 * Signal feed — the VM pulls what visitors typed.
 *
 *     GET /admin/feed.php?since=<id>&limit=<n>
 *     X-Ghost-Key: <feed_key>
 *
 * Returns JSON: {ok, rows:[...], last_id, more}. The VM keeps its own
 * watermark and passes it back as `since`, so this endpoint never writes and
 * two pullers cannot race each other. Replaying a range is just a smaller
 * `since`.
 *
 * The site never initiates contact with the VM. The VM is behind a VPN, may
 * be down, and must never be something a visitor's page load waits on — so
 * every crossing between the two hosts is VM-initiated, and this is the
 * read half of that.
 */

require_once __DIR__ . '/_config.php';

header('Content-Type: application/json');
header('X-Content-Type-Options: nosniff');
header('Cache-Control: no-store');

function out($code, $payload) {
    http_response_code($code);
    echo json_encode($payload, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
    exit;
}

$cfg = ghost_config();
if ($cfg === null) {
    out(503, ['ok' => false, 'error' => 'Not configured']);
}

if (!ghost_check_key($cfg)) {
    // Deliberately the same shape and status as any other refusal: this
    // endpoint should not confirm to an unauthenticated caller that it is
    // configured, has rows, or even exists in a working state.
    out(403, ['ok' => false, 'error' => 'Forbidden']);
}

$pdo = ghost_db($cfg);
if ($pdo === null) {
    out(503, ['ok' => false, 'error' => 'Database unavailable']);
}

$since = isset($_GET['since']) ? (int) $_GET['since'] : 0;
if ($since < 0) {
    $since = 0;
}

// Capped so one call cannot try to serialise the whole table into memory on
// shared hosting. The VM pages with `more`.
$limit = isset($_GET['limit']) ? (int) $_GET['limit'] : 200;
$limit = max(1, min($limit, 500));

try {
    // LIMIT cannot be a bound parameter with emulation off, so it is cast to
    // int above and interpolated. `since` stays bound.
    $st = $pdo->prepare(
        'SELECT id, ts, visitor, client, page, message
           FROM signals
          WHERE id > ?
       ORDER BY id ASC
          LIMIT ' . $limit
    );
    $st->execute([$since]);
    $rows = $st->fetchAll();
} catch (PDOException $e) {
    out(500, ['ok' => false, 'error' => 'Query failed']);
}

$lastId = $rows ? (int) $rows[count($rows) - 1]['id'] : $since;

foreach ($rows as &$r) {
    $r['id'] = (int) $r['id'];
}
unset($r);

out(200, [
    'ok'      => true,
    'rows'    => $rows,
    'last_id' => $lastId,
    // True when the page was filled exactly, so the VM knows to come back
    // immediately rather than waiting for its next tick.
    'more'    => count($rows) === $limit,
]);
