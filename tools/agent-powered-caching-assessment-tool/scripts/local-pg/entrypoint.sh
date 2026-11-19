#!/bin/bash
# Start sshd, start PostgreSQL through the image's own entrypoint, then keep a pgbench
# workload running until the container stops.
set -euo pipefail

# Host keys persist in /etc/ssh/keys (a volume), so the key in known_hosts stays valid
# across container rebuilds.
mkdir -p /etc/ssh/keys
for t in ed25519; do
  [ -f /etc/ssh/keys/ssh_host_${t}_key ] || ssh-keygen -q -t "$t" -N '' -f /etc/ssh/keys/ssh_host_${t}_key
done
echo 'HostKey /etc/ssh/keys/ssh_host_ed25519_key' > /etc/ssh/sshd_config.d/hostkey.conf

install -d -m 700 -o apcat -g apcat /home/apcat/.ssh
install -m 600 -o apcat -g apcat /run/apcat/authorized_keys /home/apcat/.ssh/authorized_keys
/usr/sbin/sshd

docker-entrypoint.sh "$@" &
PG=$!

# Wait for the real server: during first-time setup the image runs a temporary server on
# the socket only, so TCP on 127.0.0.1 answering means setup is over.
until pg_isready -q -h 127.0.0.1; do sleep 1; done
until psql -h /var/run/postgresql -U postgres -d shop -tAc "SELECT 1 FROM shop.product LIMIT 1" >/dev/null 2>&1; do sleep 1; done

CLIENTS=${LOAD_CLIENTS:-8}
RATE=${LOAD_TPS:-150}
echo "apcat-local-pg: load running, $CLIENTS clients, about $RATE transactions/s"
(
  while kill -0 "$PG" 2>/dev/null; do
    su postgres -c "pgbench -h /var/run/postgresql -n -c $CLIENTS -j 2 -R $RATE -T 3600 \
      -f /opt/load/product_lookup.sql@45 -f /opt/load/category_list.sql@15 \
      -f /opt/load/customer_orders.sql@15 -f /opt/load/top_sellers.sql@5 \
      -f /opt/load/place_order.sql@15 -f /opt/load/restock.sql@5 shop" >/var/log/pgbench.log 2>&1 || sleep 5
  done
) &

wait "$PG"
