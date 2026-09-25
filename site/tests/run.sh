#!/bin/bash
# Stand up Apache+PHP+MySQL against the real site tree, then run the harness.
set -e
REPO=$HOME/ghostnet-daemon
W=/tmp/gsite
NET=ghosttest

cleanup() {
  docker rm -f gweb gmysql >/dev/null 2>&1 || true
  docker network rm $NET >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup

# --- site tree, mirroring the deploy layout (config ABOVE public_html) ---
docker run --rm -v /tmp:/t alpine rm -rf /t/gsite >/dev/null 2>&1 || true
mkdir -p $W/public_html $W/scripts
cp -r $REPO/site/public_html/. $W/public_html/
cp -r $REPO/site/scripts/. $W/scripts/
cp $REPO/site/schema.sql $W/

cat > $W/ghost_config.php <<'PHPEOF'
<?php
return [
  'admin_hash' => '$2y$10$abcdefghijklmnopqrstuuMEXaMPLEhashNOTusedBYtests12345678',
  'ip_salt'    => 'e2e-salt-not-a-real-one-0123456789abcdef',
  'data_dir'   => __DIR__ . '/ghost_data',
  'feed_key'   => 'testkey-0123456789abcdef',
  'db' => [
    'host' => 'gmysql', 'name' => 'ghost',
    'user' => 'ghost',  'pass' => 'ghostpw', 'charset' => 'utf8mb4',
  ],
];
PHPEOF

mkdir -p $W/ghost_data
chmod -R 777 $W/ghost_data $W/public_html

# --- image with pdo_mysql and AllowOverride so .htaccess is actually applied ---
cat > $W/Dockerfile <<'DOCKEREOF'
FROM php:8.3-apache
RUN docker-php-ext-install pdo_mysql >/dev/null
RUN sed -i 's|AllowOverride None|AllowOverride All|g' /etc/apache2/apache2.conf
DOCKEREOF

echo "  building web image..."
docker build -q -t ghost-e2e $W >/dev/null

docker network create $NET >/dev/null

echo "  starting mysql..."
docker run -d --name gmysql --network $NET \
  -e MYSQL_ROOT_PASSWORD=rootpw -e MYSQL_DATABASE=ghost \
  -e MYSQL_USER=ghost -e MYSQL_PASSWORD=ghostpw \
  mysql:8 >/dev/null

printf "  waiting for mysql"
for i in $(seq 1 60); do
  if docker exec gmysql mysqladmin ping -h127.0.0.1 -ughost -pghostpw --silent >/dev/null 2>&1; then
    echo " ready"; break
  fi
  printf "."; sleep 2
  [ "$i" = 60 ] && { echo " TIMEOUT"; docker logs gmysql | tail -20; exit 1; }
done

echo "  loading schema.sql..."
docker exec -i gmysql mysql -h127.0.0.1 -ughost -pghostpw ghost < $W/schema.sql 2>&1 | grep -v "Using a password" || true
docker exec gmysql mysql -h127.0.0.1 -ughost -pghostpw ghost -e 'SHOW TABLES;' 2>/dev/null | grep -v "Using a password"

echo "  starting apache..."
docker run -d --name gweb --network $NET -p 8123:80 \
  -v $W:/var/www/site \
  -e APACHE_DOCUMENT_ROOT=/var/www/site/public_html \
  ghost-e2e >/dev/null
docker exec gweb sh -c 'sed -ri "s!/var/www/html!/var/www/site/public_html!g" /etc/apache2/sites-available/000-default.conf /etc/apache2/apache2.conf && apache2ctl graceful' 2>/dev/null
sleep 3

for i in $(seq 1 20); do
  curl -sf -o /dev/null http://127.0.0.1:8123/admin/ && break
  sleep 1
done

echo
sh /tmp/e2e.sh
rc=$?

echo
echo "--- migrate_log.php against the same db ---"
printf '%s\n' \
 '{"ts":"2026-09-19T07:40:58Z","ip":"203.0.113.47","ua":"Mozilla/5.0 (Android 10) Chrome/120 Mobile Safari/537.36","message":"legacy help"}' \
 '{"ts":"2026-09-19T07:41:11Z","ip":"198.51.100.9","ua":"Googlebot/2.1","message":"legacy crawl"}' \
 > $W/public_html/admin/ghost_console_log.jsonl
docker run --rm --network $NET -v $W:/var/www/site -w /var/www/site ghost-e2e php scripts/migrate_log.php
docker run --rm --network $NET -v $W:/var/www/site -w /var/www/site ghost-e2e php scripts/migrate_log.php --apply
echo
echo "  rows now in signals (visitor column must hold no addresses):"
docker exec gmysql mysql -h127.0.0.1 -ughost -pghostpw ghost -e 'SELECT id,visitor,client,COALESCE(page,"-") AS page,message FROM signals ORDER BY id;' 2>/dev/null | grep -v "Using a password"

exit $rc
