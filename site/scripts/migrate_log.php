<?php
/**
 * Load JSONL console logs into the signals table, scrubbing as it goes.
 *
 *     php scripts/migrate_log.php                 # report only
 *     php scripts/migrate_log.php --apply
 *
 * Handles both files that can exist:
 *
 *   public_html/admin/ghost_console_log.jsonl
 *       The original. Holds raw `ip` and full `ua`, and was publicly
 *       fetchable. Relocating it would not be enough — it has already been
 *       served, so those addresses are exposed and are replaced rather than
 *       hidden. Each `ip` becomes the same salted HMAC log.php now writes, so
 *       a visitor from before the change and the same visitor afterwards
 *       resolve to one identity.
 *
 *   <data_dir>/ghost_console_log.jsonl
 *       The fallback log.php writes when MySQL is unreachable. Already
 *       scrubbed; this drains it into the database so an outage delays
 *       ingestion instead of losing it. Safe to run on a schedule.
 *
 * Neither source file is deleted. Check the table, then remove them yourself.
 */

// Locate the directory that CONTAINS public_html, rather than assuming this
// script sits in scripts/. It gets run from wherever it was dropped -- beside
// public_html after an unzip, or from the repo checkout -- and a wrong guess
// here fails with a bare "file not found" that says nothing useful.
$root = null;
foreach ([dirname(__DIR__), __DIR__, dirname(dirname(__DIR__))] as $candidate) {
    if (is_file($candidate . '/public_html/admin/_config.php')) {
        $root = $candidate;
        break;
    }
}
if ($root === null) {
    fwrite(STDERR,
        "cannot find public_html/admin/_config.php relative to this script.\n" .
        "Run it from the directory containing public_html, or from the repo\n" .
        "checkout as scripts/migrate_log.php.\n");
    exit(1);
}
require_once $root . '/public_html/admin/_config.php';

$apply = in_array('--apply', $argv, true);

$cfg = ghost_config();
if ($cfg === null) {
    fwrite(STDERR, "no ghost_config.php found above the web root\n");
    exit(1);
}
$salt = isset($cfg['ip_salt']) ? $cfg['ip_salt'] : '';
if ($salt === '' || $salt === 'REPLACE_ME') {
    fwrite(STDERR, "set ip_salt in ghost_config.php first\n");
    exit(1);
}
$pdo = ghost_db($cfg);
if ($pdo === null) {
    fwrite(STDERR, "cannot reach the database -- check the db section and that schema.sql is loaded\n");
    exit(1);
}

function client_label_from_ua($ua) {
    $ua = strtolower((string) $ua);
    if ($ua === '') return 'unknown';
    if (strpos($ua, 'bot') !== false || strpos($ua, 'crawl') !== false
        || strpos($ua, 'spider') !== false) return 'bot';
    $mobile = (strpos($ua, 'mobile') !== false || strpos($ua, 'android') !== false
               || strpos($ua, 'iphone') !== false);
    foreach (['firefox' => 'firefox', 'edg/' => 'edge', 'chrome' => 'chrome',
              'safari' => 'safari'] as $needle => $name) {
        if (strpos($ua, $needle) !== false) {
            return $mobile ? $name . '-mobile' : $name;
        }
    }
    return $mobile ? 'other-mobile' : 'other';
}

$sources = [$root . '/public_html/admin/ghost_console_log.jsonl'];
if (!empty($cfg['data_dir'])) {
    // Guarded: data_dir is optional in the config, and rtrim(null) is
    // deprecated on PHP 8.1+ — it would emit a warning into the output and
    // then build a path rooted at "/".
    $sources[] = rtrim($cfg['data_dir'], '/\\') . '/ghost_console_log.jsonl';
}

$pending = [];
$addresses = [];
$skipped = 0;

foreach ($sources as $path) {
    if (!is_file($path)) {
        printf("  (no file at %s)\n", $path);
        continue;
    }
    $lines = file($path, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);
    $n = 0;
    foreach ($lines as $line) {
        $e = json_decode($line, true);
        if (!is_array($e)) { $skipped++; continue; }

        if (isset($e['ip'])) {
            $addresses[$e['ip']] = true;
            $visitor = substr(hash_hmac('sha256', $e['ip'], $salt), 0, 16);
        } else {
            $visitor = isset($e['visitor']) ? (string) $e['visitor'] : 'unknown';
        }

        $client = isset($e['ua'])
            ? client_label_from_ua($e['ua'])
            : (isset($e['client']) ? (string) $e['client'] : 'unknown');

        // Stored as DATETIME; the JSONL carries an ISO-8601 Z timestamp.
        $ts = isset($e['ts']) ? str_replace(['T', 'Z'], [' ', ''], $e['ts']) : gmdate('Y-m-d H:i:s');

        $pending[] = [
            $ts,
            mb_substr($visitor, 0, 16),
            mb_substr($client, 0, 24),
            isset($e['page']) ? mb_substr((string) $e['page'], 0, 190) : null,
            mb_substr((string) ($e['message'] ?? ''), 0, 500),
        ];
        $n++;
    }
    printf("  %-64s %d entries\n", basename(dirname($path)) . '/' . basename($path), $n);
}

printf("\n  %d rows to insert, %d raw addresses -> pseudonyms, %d unparseable\n",
       count($pending), count($addresses), $skipped);

$existing = (int) $pdo->query('SELECT COUNT(*) FROM signals')->fetchColumn();
printf("  signals currently holds %d row(s)\n", $existing);

if (!$pending) {
    exit(0);
}
if (!$apply) {
    echo "\n  DRY RUN -- re-run with --apply to insert.\n";
    exit(0);
}

$pdo->beginTransaction();
try {
    $st = $pdo->prepare(
        'INSERT INTO signals (ts, visitor, client, page, message) VALUES (?, ?, ?, ?, ?)'
    );
    foreach ($pending as $row) {
        $st->execute($row);
    }
    $pdo->commit();
} catch (PDOException $e) {
    $pdo->rollBack();
    fwrite(STDERR, "insert failed, nothing written: " . $e->getMessage() . "\n");
    exit(1);
}

$after = (int) $pdo->query('SELECT COUNT(*) FROM signals')->fetchColumn();
printf("\n  inserted %d row(s); signals now holds %d\n", count($pending), $after);
echo "  Source files were NOT deleted. Check the table, then remove them:\n";
foreach ($sources as $path) {
    if (is_file($path)) {
        printf("    rm %s\n", $path);
    }
}
