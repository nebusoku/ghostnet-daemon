<?php
/**
 * Overworld Nexus — site configuration.
 *
 * THIS FILE BELONGS ABOVE public_html, NOT INSIDE IT.
 *
 * On Hostinger the layout is:
 *
 *     /home/<user>/
 *         ghost_config.php      <- here. Not reachable over HTTP.
 *         ghost_data/           <- log lives here, created automatically
 *         public_html/
 *             admin/
 *             index.html
 *
 * Copy this file to /home/<user>/ghost_config.php and fill in the two
 * values below. Nothing here is committed to git; the sample is.
 */

return [

    /**
     * Admin password, hashed. Never the password itself.
     *
     * The old admin/index.php carried $ADMIN_PASSWORD = 'What-is-a-password'
     * in plaintext, inside the web root, in a file that also shipped in every
     * zip export. Generate a hash over SSH and paste only the hash:
     *
     *     php -r 'echo password_hash("your new password", PASSWORD_DEFAULT), "\n";'
     *
     * The result starts with $2y$ and is safe to store here. It is not safe
     * to store inside public_html, which is why this file lives outside it.
     */
    'admin_hash' => '$2y$REPLACE_ME',

    /**
     * Salt for pseudonymising visitor IPs.
     *
     * The log used to record raw addresses. It now records an HMAC keyed with
     * this salt, which still distinguishes one visitor from another and still
     * recognises a returning one -- everything the world loop actually needs
     * -- without holding anyone's address. If the log leaks again, it leaks
     * nothing about people.
     *
     * Generate once and never change it, or every returning visitor will look
     * new:
     *
     *     php -r 'echo bin2hex(random_bytes(32)), "\n";'
     */
    'ip_salt' => 'REPLACE_ME',

    /**
     * Where files are written. Must be outside public_html.
     * Created on first write if missing.
     *
     * Still used after the move to MySQL: it holds the pre-database JSONL log
     * during migration, and is where a failed database write falls back to so
     * that a visitor's message is never silently lost.
     */
    'data_dir' => __DIR__ . '/ghost_data',

    /**
     * Shared secret for the VM-facing endpoints, feed.php and echo.php.
     *
     * The VM sends it as an X-Ghost-Key header. A header rather than a query
     * parameter, because query strings end up in access logs, Referer headers
     * and browser history, and this one grants read access to everything
     * visitors have typed.
     *
     *     php -r 'echo bin2hex(random_bytes(32)), "\n";'
     *
     * Different value from ip_salt. Reusing one secret for two purposes means
     * rotating either forces rotating both, and rotating ip_salt makes every
     * returning visitor look new.
     */
    'feed_key' => 'REPLACE_ME',

    /**
     * MySQL, created in hPanel. Load schema.sql into it once.
     *
     * The database is never exposed to the internet: only PHP on this host
     * connects to it, and the VM reaches it through feed.php and echo.php
     * over HTTPS. That survives the VM's public address changing, which
     * matters because it sits behind a VPN.
     */
    'db' => [
        'host'     => 'localhost',
        'name'     => 'REPLACE_ME',
        'user'     => 'REPLACE_ME',
        'pass'     => 'REPLACE_ME',
        'charset'  => 'utf8mb4',
    ],

];
