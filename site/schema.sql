-- Overworld Nexus — site database.
--
-- Run once against the MySQL database created in hPanel:
--
--     mysql -u <user> -p <dbname> < schema.sql
--
-- or paste into phpMyAdmin. Safe to re-run; every statement is IF NOT EXISTS.
--
-- utf8mb4 throughout, not utf8: the console takes arbitrary text from the
-- open internet, and MySQL's "utf8" is three-byte only, so a single emoji
-- would truncate a row or error the insert depending on strict mode.
--
-- Index prefixes are capped at 190 characters for the same reason. On older
-- MySQL an index key is limited to 767 bytes, which is 191 four-byte
-- characters, and a VARCHAR(255) utf8mb4 column will not index.

SET NAMES utf8mb4;

-- ---------------------------------------------------------------------------
-- signals: site -> world
--
-- One row per message typed into the hidden console. The VM reads these
-- through feed.php and folds them into the world.
--
-- There is deliberately no `consumed` flag. The VM keeps its own watermark
-- and asks for everything after it, which means the read path never writes,
-- two pullers cannot race, and replaying a range is just a smaller `since`.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS signals (
  id       BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  ts       DATETIME        NOT NULL,
  visitor  CHAR(16)        NOT NULL COMMENT 'salted HMAC of IP, never the IP',
  client   VARCHAR(24)     NOT NULL COMMENT 'coarse label, not a user agent',
  page     VARCHAR(190)        NULL COMMENT 'where the console was opened',
  message  VARCHAR(500)    NOT NULL,
  PRIMARY KEY (id),
  KEY idx_ts      (ts),
  KEY idx_visitor (visitor)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- echoes: world -> site
--
-- Fragments the daemon has produced that are allowed to surface on the site.
-- The VM pushes these through echo.php, which also regenerates a static
-- echoes.json so that serving one costs no database work at all.
--
-- `source` is what makes an echo traceable: it names the world event or
-- document the fragment came from, so a whisper on the website can be walked
-- back to the thing in the Nexus that caused it.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS echoes (
  id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  created_at DATETIME        NOT NULL,
  expires_at DATETIME            NULL COMMENT 'NULL = no expiry',
  weight     TINYINT UNSIGNED NOT NULL DEFAULT 1 COMMENT 'surfacing frequency',
  scope      VARCHAR(32)     NOT NULL DEFAULT 'any' COMMENT 'page or area',
  body       VARCHAR(280)    NOT NULL,
  source     VARCHAR(64)         NULL COMMENT 'world event or doc id',
  PRIMARY KEY (id),
  KEY idx_live (expires_at, weight)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- traffic: ambient pressure
--
-- Hourly rollup rather than a row per request. The world wants to know that
-- the Undercroft page drew attention this week, not that someone loaded it at
-- 14:03:22 — and an aggregate cannot be turned back into a browsing history.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS traffic (
  bucket DATETIME        NOT NULL COMMENT 'truncated to the hour, UTC',
  page   VARCHAR(190)    NOT NULL,
  hits   INT UNSIGNED    NOT NULL DEFAULT 0,
  PRIMARY KEY (bucket, page)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
