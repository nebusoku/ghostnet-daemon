<?php
/**
 * Move the existing console log out of the web root and strip the addresses.
 *
 * Run once, on the server, after ghost_config.php exists:
 *
 *     php scripts/migrate_log.php                 # report only
 *     php scripts/migrate_log.php --apply
 *
 * The old log holds real IP addresses and full user-agent strings, and it was
 * publicly fetchable. Relocating it is not enough on its own -- the file has
 * already been served, so the addresses in it should be treated as exposed
 * and replaced rather than merely hidden.
 *
 * Each `ip` becomes the same salted HMAC that log.php now writes, so history
 * and new entries agree: a visitor who appeared before the change and returns
 * afterwards resolves to one identity. The addresses are not recoverable from
 * the result.
 *
 * The original is left where it is. Delete it yourself once the output looks
 * right -- this script will not remove the only copy of anything.
 */

$root = dirname(__DIR__);
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

$old = $root . '/public_html/admin/ghost_console_log.jsonl';
$new = rtrim($cfg['data_dir'], '/\\') . '/ghost_console_log.jsonl';

if (!is_file($old)) {
    echo "  nothing at $old -- already migrated?\n";
    exit(0);
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

$lines = file($old, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);
$out = [];
$addresses = [];
$skipped = 0;

foreach ($lines as $line) {
    $e = json_decode($line, true);
    if (!is_array($e)) { $skipped++; continue; }

    if (isset($e['ip'])) {
        $addresses[$e['ip']] = true;
        $e['visitor'] = substr(hash_hmac('sha256', $e['ip'], $salt), 0, 16);
        unset($e['ip']);
    }
    if (isset($e['ua'])) {
        $e['client'] = client_label_from_ua($e['ua']);
        unset($e['ua']);
    }
    $out[] = json_encode(
        ['ts' => $e['ts'] ?? '', 'visitor' => $e['visitor'] ?? 'unknown',
         'client' => $e['client'] ?? 'unknown', 'message' => $e['message'] ?? ''],
        JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE
    );
}

printf("  %d entries, %d distinct addresses -> pseudonyms, %d unparseable\n",
       count($out), count($addresses), $skipped);
printf("  from : %s\n  to   : %s\n", $old, $new);

if (!$apply) {
    echo "\n  DRY RUN -- re-run with --apply to write.\n";
    exit(0);
}

$dir = dirname($new);
if (!is_dir($dir) && !@mkdir($dir, 0700, true)) {
    fwrite(STDERR, "cannot create $dir\n");
    exit(1);
}
if (file_put_contents($new, implode(PHP_EOL, $out) . PHP_EOL, LOCK_EX) === false) {
    fwrite(STDERR, "cannot write $new\n");
    exit(1);
}
@chmod($new, 0600);

printf("\n  wrote %s\n", $new);
printf("  The original still exists at:\n    %s\n", $old);
printf("  Check the new file, then delete it:\n    rm %s\n", $old);
