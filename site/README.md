# overworldnex.us — site files

The website is **not** on the VM. It is PHP on Hostinger shared hosting
(LiteSpeed, hPanel), a separate host the API cannot reach. Nothing here
deploys itself; these files are uploaded by hand.

Only the files this project authors live here. The rest of the site — the
HTML pages, `assets/`, the logo — is unchanged and not tracked.

```
site/
  ghost_config.sample.php     -> copy to ABOVE public_html, fill in, rename
  public_html/admin/
    .htaccess                 -> deny data files, no listings
    _config.php               -> shared config loader + pseudonymiser
    log.php                   -> console intake (site -> world)
    index.php                 -> log viewer, behind a real password
  scripts/
    migrate_log.php           -> one-shot: relocate + scrub the existing log
```

## What this changed, and why

Two live exposures, both confirmed on 2026-09-24:

1. `https://overworldnex.us/admin/ghost_console_log.jsonl` returned **HTTP 200
   and 4.3 KB** of visitor IP addresses and user-agent strings. Nothing linked
   to it; the path was guessable, and it had already been served.
2. `admin/index.php` carried `$ADMIN_PASSWORD = 'What-is-a-password'` in
   plaintext, inside the web root, in a file included in every zip export of
   the site.

The fix is four things, not one:

- **The log moved out of the web root**, into `ghost_data/` beside
  `ghost_config.php`. It cannot be fetched over HTTP at all now.
- **`.htaccess` denies data files anyway.** Defence in depth: a file that is
  merely unlinked is still public, and a future host or a stray copy might not
  honour the move.
- **Addresses are no longer recorded.** `log.php` writes a salted HMAC of the
  IP instead. Returning visitors are still recognisable — which is everything
  the world loop actually needs — but the address itself is never written, and
  if the log leaks again it leaks nothing about people. User agents are reduced
  to a coarse label rather than a fingerprint.
- **The password is a bcrypt hash above the web root**, with a growing delay on
  failed attempts and a fresh session id on login.

`log.php` still never calls the VM. A visitor typing into the console must not
wait on, or fail because of, a machine on the other side of a VPN — so the
world-loop ingest is a pull from the VM's side, on its own timer.

## Deploying

1. **Create the config**, above `public_html`:

   ```bash
   php -r 'echo password_hash("your new password", PASSWORD_DEFAULT), "\n";'
   php -r 'echo bin2hex(random_bytes(32)), "\n";'
   ```

   Copy `ghost_config.sample.php` to `/home/<user>/ghost_config.php` and paste
   the hash into `admin_hash` and the random string into `ip_salt`. Generate
   the salt **once** — changing it makes every returning visitor look new.

2. **Upload** `public_html/admin/` over the existing one. Four files.

3. **Migrate the old log:**

   ```bash
   php scripts/migrate_log.php            # report
   php scripts/migrate_log.php --apply
   ```

   It leaves the original in place. Check the output, then delete it.

4. **Verify the hole is closed:**

   ```bash
   curl -o /dev/null -w '%{http_code}\n' https://overworldnex.us/admin/ghost_console_log.jsonl
   ```

   Expect `403` or `404`. A `200` means the upload did not take.

5. Log in at `/admin/`, confirm the history renders — old entries show their
   pseudonym in the Visitor column — then send a message through the hidden
   console on any page and confirm it appears.

## Content feeds (MySQL)

> **Untested.** The security fixes above were verified in a container — 14
> functional checks, migration output confirmed. Everything in *this* section
> was written while the VPN to the VM was down, so it has not been linted or
> run against a real MySQL. Treat it as a first draft until it has been.

MySQL replaces the JSONL for feeds. Files were fine for capture; they are poor
at cursors, concurrent writes, expiry and aggregation, which is all of what a
feed needs.

```
schema.sql                 -> signals, echoes, traffic
public_html/admin/
  feed.php                 -> VM pulls signals since <id>
  echo.php                 -> VM pushes echoes, regenerates echoes.json
public_html/echoes.json    -> generated; the console reads this
```

**The VM never touches MySQL.** It talks to `feed.php` and `echo.php` over
HTTPS with a shared secret in an `X-Ghost-Key` header. The database stays
closed to the internet, and nothing breaks when the VM's public address
changes — which matters, because it sits behind a VPN that drops.

**`signals` has no consumed flag.** The VM keeps its own watermark and asks
for everything after it, so the read path never writes, two pullers cannot
race, and replaying a range is just a smaller `since`.

**`echo.php` writes the row and regenerates a static `echoes.json`.** The
database holds the structure — expiry, weight, scope, provenance — but a page
load should never touch MySQL to find out what to whisper. If the VM goes
quiet the echoes go stale, which reads as the mesh going quiet rather than as
a broken page. The file is replaced atomically via `rename()`, so nobody is
served a half-written one.

`echoes.json` lives in `public_html/`, **not** in `admin/`, because the
`.htaccess` there denies `*.json`. Moving it would take the echo half of the
loop off the air silently.

Setup: create the database in hPanel, load `schema.sql`, fill in the `db`
section and `feed_key` in `ghost_config.php`, then run `migrate_log.php
--apply` to load any existing JSONL into `signals`.

`log.php` still falls back to the JSONL file if MySQL is unreachable, and
`migrate_log.php` drains that file into the table — so a database outage
delays ingestion rather than losing a visitor's message. That makes it safe
to run on a schedule.

## Not done yet

- **The `join/` invite is still `https://discord.gg/your-link-here`**, twice:
  the button href, and a visible "Tip: replace…" line that tells visitors to
  fix it. Needs the real invite URL.
- **The world loop itself.** Ingest (VM pulls this log, folds messages into the
  world) and echo (VM pushes `echoes.json`, the console surfaces it) are both
  unbuilt. The intake half of ingest is what `log.php` already is.
- **The console does not talk to the daemon, on purpose.** Wiring anonymous
  public input straight to an LLM means paying for every passer-by — the
  existing log contains `help`, `hello`, and someone typing `fuck` at it — and
  exposing the guard to the open internet with no identity behind it. Echoes
  give the console living content without making the site an open endpoint.
