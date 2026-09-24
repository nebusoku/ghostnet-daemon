<?php
/**
 * Console signal intake.
 *
 * Accepts JSON POST {"message": "..."} from assets/ghost-console.js and
 * appends one JSONL record. This is the site-to-world half of the leak loop:
 * the VM reads this file on a timer and folds what visitors typed into the
 * world.
 *
 * Changed 2026-09-24. The previous version wrote raw IP addresses and full
 * user-agent strings into public_html/admin/ghost_console_log.jsonl, which
 * was served over HTTP with a 200. Three things are different now:
 *
 *   1. The log is written outside the web root, so it cannot be fetched.
 *   2. Addresses are replaced by a salted HMAC. Returning visitors are still
 *      recognisable, which is all the loop needed; the addresses themselves
 *      are never recorded.
 *   3. User agents are reduced to a coarse label rather than a fingerprint.
 *
 * It still never calls out to the VM. A visitor typing into the console must
 * not wait on, or fail because of, a machine on the other side of a VPN.
 */

require_once __DIR__ . '/_config.php';

header('Content-Type: application/json');
header('X-Content-Type-Options: nosniff');

function fail($code, $error) {
    http_response_code($code);
    echo json_encode(['ok' => false, 'error' => $error]);
    exit;
}

if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    fail(405, 'Method not allowed');
}

$cfg = ghost_config();
if ($cfg === null) {
    // Fail closed. Falling back to the old behaviour here would quietly
    // reinstate the exact exposure this change exists to remove.
    fail(503, 'Not configured');
}

$visitor = ghost_visitor_id($cfg);
if ($visitor === null) {
    fail(503, 'Not configured');
}

// 8 KB is far more than the console input can send and small enough that a
// flood costs little. Beyond this, drop rather than truncate.
$raw = file_get_contents('php://input', false, null, 0, 8192);
$data = json_decode($raw, true);
$message = (is_array($data) && isset($data['message'])) ? trim($data['message']) : '';

if ($message === '') {
    fail(400, 'Empty message');
}
if (mb_strlen($message) > 500) {
    $message = mb_substr($message, 0, 500);
}
// Control characters would corrupt the JSONL line the VM reads back.
$message = preg_replace('/[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]/u', '', $message);

$dir = isset($cfg['data_dir']) ? $cfg['data_dir'] : null;
if (!$dir || !is_dir($dir)) {
    fail(503, 'Not configured');
}
$logFile = rtrim($dir, '/\\') . '/ghost_console_log.jsonl';

// A public endpoint with no identity behind it needs a ceiling. 32 MB of
// JSONL is years of ordinary use and minutes of deliberate abuse.
if (is_file($logFile) && filesize($logFile) > 33554432) {
    fail(507, 'Log full');
}

$entry = [
    'ts'      => gmdate('Y-m-d\TH:i:s\Z'),
    'visitor' => $visitor,
    'client'  => ghost_client_label(),
    'message' => $message,
];

$line = json_encode($entry, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE) . PHP_EOL;

$fh = @fopen($logFile, 'ab');
if (!$fh) {
    fail(500, 'Cannot write');
}
if (flock($fh, LOCK_EX)) {
    fwrite($fh, $line);
    flock($fh, LOCK_UN);
}
fclose($fh);
@chmod($logFile, 0600);

echo json_encode(['ok' => true]);
