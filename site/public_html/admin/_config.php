<?php
/**
 * Locate and load the out-of-root configuration.
 *
 * Shared by log.php and index.php. Kept deliberately small: it resolves one
 * file, validates it, and returns an array. Anything that fails here fails
 * closed -- a missing config must not silently fall back to logging raw IPs
 * or to an open admin page.
 */

function ghost_config() {
    static $cfg = null;
    if ($cfg !== null) {
        return $cfg;
    }

    // __DIR__ is public_html/admin, so two levels up is the account root,
    // above the web root. Both spellings are tried because some panels place
    // public_html one level deeper than others.
    $candidates = [
        __DIR__ . '/../../ghost_config.php',
        __DIR__ . '/../../../ghost_config.php',
    ];

    foreach ($candidates as $path) {
        if (is_file($path)) {
            $loaded = include $path;
            if (is_array($loaded)) {
                $cfg = $loaded;
                break;
            }
        }
    }

    if ($cfg === null) {
        return null;
    }

    $dir = isset($cfg['data_dir']) ? $cfg['data_dir'] : null;
    if ($dir && !is_dir($dir)) {
        @mkdir($dir, 0700, true);
    }

    return $cfg;
}

/**
 * Stable pseudonym for a visitor, derived from their address.
 *
 * Returns 16 hex characters. The same visitor produces the same value for as
 * long as the salt is unchanged, so returning visitors are still recognisable
 * to the world loop, but the address itself is never written down and cannot
 * be recovered from the log.
 */
function ghost_visitor_id($cfg) {
    $ip = isset($_SERVER['HTTP_CF_CONNECTING_IP'])
        ? $_SERVER['HTTP_CF_CONNECTING_IP']
        : (isset($_SERVER['REMOTE_ADDR']) ? $_SERVER['REMOTE_ADDR'] : 'unknown');

    $salt = isset($cfg['ip_salt']) ? $cfg['ip_salt'] : '';
    if ($salt === '' || $salt === 'REPLACE_ME') {
        // An unsalted hash of an IPv4 address is not anonymous -- the whole
        // space is small enough to enumerate in seconds. Refuse rather than
        // pretend.
        return null;
    }

    return substr(hash_hmac('sha256', $ip, $salt), 0, 16);
}

/**
 * PDO handle, or null if the database is unreachable or unconfigured.
 *
 * Returns null rather than throwing: a database that is down must degrade the
 * site, not break it. log.php falls back to a file, and the admin viewer says
 * so plainly instead of showing a stack trace to whoever found /admin.
 */
function ghost_db($cfg) {
    static $pdo = null;
    static $tried = false;
    if ($tried) {
        return $pdo;
    }
    $tried = true;

    $db = isset($cfg['db']) ? $cfg['db'] : null;
    if (!is_array($db) || empty($db['name']) || $db['name'] === 'REPLACE_ME') {
        return null;
    }

    $dsn = sprintf('mysql:host=%s;dbname=%s;charset=%s',
        isset($db['host']) ? $db['host'] : 'localhost',
        $db['name'],
        isset($db['charset']) ? $db['charset'] : 'utf8mb4');

    try {
        $pdo = new PDO($dsn, $db['user'], $db['pass'], [
            PDO::ATTR_ERRMODE            => PDO::ERRMODE_EXCEPTION,
            PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
            // Real prepared statements, not client-side interpolation. The
            // console accepts arbitrary text from the open internet and it
            // goes straight into a query parameter.
            PDO::ATTR_EMULATE_PREPARES   => false,
            PDO::ATTR_TIMEOUT            => 5,
        ]);
    } catch (PDOException $e) {
        $pdo = null;
    }
    return $pdo;
}

/**
 * Constant-time check of the VM's shared secret.
 *
 * Read from a header rather than the query string: query parameters are
 * recorded in access logs and Referer headers, and this key grants read
 * access to everything visitors have typed into the console.
 */
function ghost_check_key($cfg) {
    $want = isset($cfg['feed_key']) ? (string) $cfg['feed_key'] : '';
    if ($want === '' || $want === 'REPLACE_ME') {
        return false;
    }
    $got = isset($_SERVER['HTTP_X_GHOST_KEY']) ? (string) $_SERVER['HTTP_X_GHOST_KEY'] : '';
    if ($got === '') {
        return false;
    }
    return hash_equals($want, $got);
}

/**
 * Coarse client label, in place of the full user-agent string.
 *
 * A complete UA is a fingerprint; the loop only ever wanted to know roughly
 * what kind of thing was on the other end.
 */
function ghost_client_label() {
    $ua = isset($_SERVER['HTTP_USER_AGENT']) ? $_SERVER['HTTP_USER_AGENT'] : '';
    if ($ua === '') {
        return 'unknown';
    }
    $ua = strtolower($ua);

    if (strpos($ua, 'bot') !== false || strpos($ua, 'crawl') !== false
        || strpos($ua, 'spider') !== false) {
        return 'bot';
    }

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
