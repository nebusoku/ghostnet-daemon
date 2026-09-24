<?php
/**
 * GhostNet console log viewer.
 *
 * Changed 2026-09-24. The previous version held
 * $ADMIN_PASSWORD = 'What-is-a-password' in plaintext, in the web root, in a
 * file that shipped in every zip export of the site. It now compares against
 * a bcrypt hash kept above public_html, so neither the password nor anything
 * reversible into it exists inside the served tree.
 *
 * The log itself also moved out of the web root, so the raw download is
 * streamed through this page -- behind the login -- rather than linked as a
 * static file.
 */

require_once __DIR__ . '/_config.php';

session_start();

$cfg = ghost_config();
$configured = ($cfg !== null
    && !empty($cfg['admin_hash'])
    && strpos($cfg['admin_hash'], 'REPLACE_ME') === false);

$logFile = ($cfg && !empty($cfg['data_dir']))
    ? rtrim($cfg['data_dir'], '/\\') . '/ghost_console_log.jsonl'
    : null;

$loggedIn = !empty($_SESSION['ghost_admin']);
$error = '';

// Throttle. This page is reachable by anyone who guesses /admin/, so a bare
// comparison loop is a free offline-speed guessing oracle. Delay grows with
// attempts and is held in the session.
$attempts = isset($_SESSION['ghost_attempts']) ? (int) $_SESSION['ghost_attempts'] : 0;

if (!$loggedIn && $_SERVER['REQUEST_METHOD'] === 'POST') {
    if (!$configured) {
        $error = 'Not configured. See site/ghost_config.sample.php.';
    } else {
        if ($attempts > 0) {
            sleep(min($attempts, 5));
        }
        $pass = isset($_POST['password']) ? (string) $_POST['password'] : '';
        if (password_verify($pass, $cfg['admin_hash'])) {
            // New id on privilege change, so a pre-set session cookie cannot
            // be reused as a logged-in one.
            session_regenerate_id(true);
            $_SESSION['ghost_admin'] = true;
            $_SESSION['ghost_attempts'] = 0;
            $loggedIn = true;
        } else {
            $_SESSION['ghost_attempts'] = $attempts + 1;
            $error = 'Invalid password.';
        }
    }
}

if (isset($_GET['logout'])) {
    $_SESSION = [];
    session_destroy();
    header('Location: index.php');
    exit;
}

// Raw download, gated. The file is outside the web root now, so this is the
// only route to it.
if ($loggedIn && isset($_GET['download']) && $logFile && is_file($logFile)) {
    header('Content-Type: application/x-ndjson');
    header('Content-Disposition: attachment; filename="ghost_console_log.jsonl"');
    header('X-Content-Type-Options: nosniff');
    readfile($logFile);
    exit;
}

function load_entries($path) {
    if (!$path || !file_exists($path)) {
        return [];
    }
    $lines = file($path, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);
    $entries = [];
    foreach ($lines as $line) {
        $j = json_decode($line, true);
        if (is_array($j)) {
            $entries[] = $j;
        }
    }
    return array_reverse($entries);
}

$entries = $loggedIn ? load_entries($logFile) : [];

function h($s) {
    return htmlspecialchars((string) $s, ENT_QUOTES, 'UTF-8');
}
?>
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>GhostNet Console Log • Overworld Nexus</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta name="robots" content="noindex, nofollow" />
  <style>
    body { margin:0; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background:#020617; color:#e5e7eb; }
    .wrap { max-width: 1100px; margin: 0 auto; padding: 2rem 1.5rem 3rem; }
    h1 { font-size: 1.6rem; margin-bottom: 0.5rem; }
    .muted { color:#9ca3af; font-size:0.9rem; }
    form.login { max-width: 320px; margin-top: 1.5rem; padding: 1.25rem; background:#0b1120; border-radius:12px; border:1px solid #1f2937; }
    form.login label { display:block; font-size:0.85rem; margin-bottom:0.25rem; }
    form.login input[type="password"] { width:100%; padding:0.4rem 0.5rem; font-size:0.9rem; border-radius:6px; border:1px solid #4b5563; background:#020617; color:#e5e7eb; }
    form.login button { margin-top:0.8rem; padding:0.4rem 0.9rem; font-size:0.9rem; border-radius:999px; border:1px solid #22c55e; background:#22c55e; color:#020617; cursor:pointer; }
    .error { color:#f97316; margin-top:0.5rem; font-size:0.85rem; }
    .warn { margin-top:1rem; padding:0.75rem 1rem; border-radius:8px; border:1px solid #f97316; color:#fdba74; font-size:0.85rem; }
    table { width:100%; border-collapse:collapse; margin-top:1.5rem; font-size:0.85rem; }
    th, td { padding:0.5rem 0.4rem; border-bottom:1px solid #1f2937; vertical-align:top; }
    th { text-align:left; font-size:0.75rem; letter-spacing:0.15em; text-transform:uppercase; color:#9ca3af; }
    tr:nth-child(even) td { background:#020617; }
    .ts { white-space:nowrap; font-family:"SF Mono", Menlo, Monaco, Consolas, "Courier New", monospace; color:#a5b4fc; }
    .vid { font-family:"SF Mono", Menlo, Monaco, Consolas, "Courier New", monospace; color:#4ade80; }
    .ua { color:#9ca3af; }
    .msg { font-family:"SF Mono", Menlo, Monaco, Consolas, "Courier New", monospace; white-space:pre-wrap; }
    .top-actions { margin-top:1rem; display:flex; justify-content:space-between; align-items:center; gap:1rem; flex-wrap:wrap; }
    .badge { padding:0.15rem 0.5rem; border-radius:999px; border:1px solid #4b5563; font-size:0.75rem; color:#9ca3af; }
    a.download, a.logout { font-size:0.85rem; color:#22d3ee; text-decoration:none; }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>GhostNet Console Log</h1>
    <p class="muted">
      Hidden console inputs captured from the Overworld Nexus site. Raw material
      for canon, transmissions, and the world loop.
    </p>

    <?php if (!$configured): ?>
      <div class="warn">
        No configuration found above the web root. Copy
        <code>ghost_config.sample.php</code> to <code>ghost_config.php</code> in
        the directory above <code>public_html</code> and set
        <code>admin_hash</code> and <code>ip_salt</code>. Until then this page
        cannot be logged into and <code>log.php</code> will refuse writes.
      </div>
    <?php endif; ?>

    <?php if (!$loggedIn): ?>
      <form method="post" class="login">
        <label for="password">Admin password</label>
        <input type="password" id="password" name="password" autocomplete="current-password" />
        <button type="submit">Enter</button>
        <?php if ($error !== ''): ?>
          <div class="error"><?= h($error) ?></div>
        <?php endif; ?>
      </form>
    <?php else: ?>
      <div class="top-actions">
        <span class="badge">Entries: <?= count($entries) ?></span>
        <span>
          <?php if ($logFile && is_file($logFile)): ?>
            <a class="download" href="?download=1">Download raw JSONL log</a> &nbsp;
          <?php endif; ?>
          <a class="logout" href="?logout=1">Log out</a>
        </span>
      </div>

      <?php if (empty($entries)): ?>
        <p class="muted" style="margin-top:1.5rem;">No console activity logged yet.</p>
      <?php else: ?>
        <table>
          <thead>
            <tr>
              <th>Time (UTC)</th>
              <th>Visitor</th>
              <th>Client</th>
              <th>Message</th>
            </tr>
          </thead>
          <tbody>
          <?php foreach ($entries as $e): ?>
            <tr>
              <td class="ts"><?= h($e['ts'] ?? '') ?></td>
              <?php // `ip` and `ua` are the pre-2026-09-24 field names. ?>
              <td class="vid"><?= h($e['visitor'] ?? $e['ip'] ?? '') ?></td>
              <td class="ua"><?= h($e['client'] ?? $e['ua'] ?? '') ?></td>
              <td class="msg"><?= h($e['message'] ?? '') ?></td>
            </tr>
          <?php endforeach; ?>
          </tbody>
        </table>
      <?php endif; ?>
    <?php endif; ?>
  </div>
</body>
</html>
