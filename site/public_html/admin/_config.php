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
