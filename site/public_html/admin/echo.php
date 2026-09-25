<?php
/**
 * Echo intake — the VM pushes fragments that may surface on the site.
 *
 *     POST /admin/echo.php
 *     X-Ghost-Key: <feed_key>
 *     {"echoes":[{"body":"...","scope":"any","weight":1,
 *                 "ttl_hours":48,"source":"world_event:91"}]}
 *
 * Two things happen per push: rows go into `echoes`, and a static
 * echoes.json is regenerated in the web root.
 *
 * The static file is the point. The database holds the structure — expiry,
 * weight, scope, provenance — but a page load should never touch MySQL to
 * find out what to whisper. The console fetches a flat file that a webserver
 * can serve from cache, and if the VM goes quiet the echoes simply go stale,
 * which reads as the mesh going quiet rather than as a broken page.
 *
 * Also accepts DELETE-ish maintenance via {"prune":true}, which drops expired
 * rows. Retention matters here: these are fragments about a living world, not
 * an archive, and an echo nobody can reach is just weight in the table.
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

if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    out(405, ['ok' => false, 'error' => 'Method not allowed']);
}

$cfg = ghost_config();
if ($cfg === null) {
    out(503, ['ok' => false, 'error' => 'Not configured']);
}
if (!ghost_check_key($cfg)) {
    out(403, ['ok' => false, 'error' => 'Forbidden']);
}
$pdo = ghost_db($cfg);
if ($pdo === null) {
    out(503, ['ok' => false, 'error' => 'Database unavailable']);
}

$raw = file_get_contents('php://input', false, null, 0, 262144);
$data = json_decode($raw, true);
if (!is_array($data)) {
    out(400, ['ok' => false, 'error' => 'Bad JSON']);
}

$now = gmdate("Y-m-d H:i:s");
$inserted = 0;
$skipped  = 0;

if (!empty($data['echoes']) && is_array($data['echoes'])) {
    try {
        $st = $pdo->prepare(
            'INSERT INTO echoes (created_at, expires_at, weight, scope, body, source)
             VALUES (?, ?, ?, ?, ?, ?)'
        );
        foreach ($data['echoes'] as $e) {
            if (!is_array($e)) {
                continue;
            }
            $body = isset($e['body']) ? trim((string) $e['body']) : '';
            if ($body === '') {
                $skipped++;
                continue;
            }
            $body = mb_substr($body, 0, 280);
            // Control characters would break the JSON the console reads.
            $body = preg_replace('/[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]/u', '', $body);

            // Absent ttl_hours means no expiry. A ttl_hours that is present
            // but not positive is a caller bug -- and the dangerous reading
            // is the permissive one: folding it into "no expiry" would take
            // a request for a short or already-dead lifetime and silently
            // turn it into permanent content on a public page. Refuse it and
            // say so instead.
            $expires = null;
            if (array_key_exists('ttl_hours', $e)) {
                $ttl = (int) $e['ttl_hours'];
                if ($ttl <= 0) {
                    $skipped++;
                    continue;
                }
                $expires = gmdate('Y-m-d H:i:s', time() + $ttl * 3600);
            }

            $weight = isset($e['weight']) ? (int) $e['weight'] : 1;
            $weight = max(1, min($weight, 255));

            $scope = isset($e['scope'])
                ? mb_substr(preg_replace('/[^a-z0-9:_\-\/]/i', '', (string) $e['scope']), 0, 32)
                : 'any';
            if ($scope === '') {
                $scope = 'any';
            }

            $source = isset($e['source'])
                ? mb_substr((string) $e['source'], 0, 64)
                : null;

            $st->execute([$now, $expires, $weight, $scope, $body, $source]);
            $inserted++;
        }
    } catch (PDOException $e) {
        out(500, ['ok' => false, 'error' => 'Insert failed']);
    }
}

$pruned = 0;
if (!empty($data['prune'])) {
    try {
        $st = $pdo->prepare('DELETE FROM echoes WHERE expires_at IS NOT NULL AND expires_at < ?');
        $st->execute([$now]);
        $pruned = $st->rowCount();
    } catch (PDOException $e) {
        // Not fatal. A failed prune costs table size, not correctness.
    }
}

// ---------------------------------------------------------------------------
// Regenerate the static file the console actually reads.
// ---------------------------------------------------------------------------
$published = 0;
$target = dirname(__DIR__) . '/echoes.json';

try {
    $st = $pdo->prepare(
        'SELECT body, scope, weight, source
           FROM echoes
          WHERE expires_at IS NULL OR expires_at > ?
       ORDER BY weight DESC, id DESC
          LIMIT 200'
    );
    $st->execute([$now]);
    $live = $st->fetchAll();

    foreach ($live as &$row) {
        $row['weight'] = (int) $row['weight'];
    }
    unset($row);

    $payload = json_encode(
        ['generated' => gmdate('c'), 'echoes' => $live],
        JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE
    );

    // Atomic replace. A half-written echoes.json served to a visitor mid-write
    // is a parse error in their console; rename() on the same filesystem is
    // the cheapest way to make the swap all-or-nothing.
    $tmp = $target . '.tmp';
    if (file_put_contents($tmp, $payload, LOCK_EX) !== false) {
        if (@rename($tmp, $target)) {
            @chmod($target, 0644);
            $published = count($live);
        } else {
            @unlink($tmp);
        }
    }
} catch (PDOException $e) {
    out(500, ['ok' => false, 'error' => 'Publish failed']);
}

out(200, [
    'ok'        => true,
    'inserted'  => $inserted,
    // Non-zero means the VM sent something malformed -- an empty body, or a
    // ttl_hours that was not positive. Reported rather than swallowed so the
    // pusher can notice it is dropping echoes.
    'skipped'   => $skipped,
    'pruned'    => $pruned,
    'published' => $published,
]);
