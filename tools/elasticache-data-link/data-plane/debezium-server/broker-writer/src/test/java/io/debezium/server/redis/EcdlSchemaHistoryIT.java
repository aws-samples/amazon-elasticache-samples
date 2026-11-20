/*
 * EcdlSchemaHistory against a real Valkey. Not run by the image build (there is
 * no server there); run by hand:
 *
 *   ECDL_TEST_VALKEY=127.0.0.1:A ECDL_TEST_VALKEY2=127.0.0.1:B \
 *   [ECDL_TEST_VALKEY_TLS=localhost:T -Djavax.net.ssl.trustStore=<ca.p12> -Djavax.net.ssl.trustStorePassword=<pw>] \
 *   java -cp ... io.debezium.server.redis.EcdlSchemaHistoryIT
 *
 * Either part can run alone. A and B are two standalone Valkey servers you started yourself: the test
 * makes B a replica of A and back, and deletes its own keys (ecdl:it:history*).
 * T, if given, is a TLS-only Valkey whose certificate names "localhost" and not
 * 127.0.0.1, signed by a CA in the trust store: it shows the history and the
 * offset store refuse a certificate that does not name the host.
 */
package io.debezium.server.redis;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

import io.debezium.config.Configuration;
import io.debezium.relational.history.HistoryRecordComparator;
import io.debezium.relational.history.SchemaHistoryListener;
import io.debezium.storage.redis.history.RedisSchemaHistory;
import redis.clients.jedis.HostAndPort;
import redis.clients.jedis.Jedis;
import redis.clients.jedis.StreamEntryID;

public final class EcdlSchemaHistoryIT {

    private static int passed;
    private static final List<String> failures = new ArrayList<>();

    static final String KEY = "ecdl:it:history";
    static String a;
    static String b;

    static void check(String name, boolean cond, String detail) {
        if (cond) {
            passed++;
            System.out.println("PASS " + name);
        }
        else {
            failures.add(name + ": " + detail);
            System.out.println("FAIL " + name + ": " + detail);
        }
    }

    static Jedis admin(String address) {
        return new Jedis(HostAndPort.from(address));
    }

    static void replicaOf(String replica, String primary) throws InterruptedException {
        try (Jedis j = admin(replica)) {
            if (primary == null) {
                j.replicaofNoOne();
                return;
            }
            HostAndPort p = HostAndPort.from(primary);
            j.replicaof(p.getHost(), p.getPort());
            for (int i = 0; i < 100; i++) {
                if (j.info("replication").contains("master_link_status:up")) {
                    return;
                }
                Thread.sleep(100);
            }
            throw new IllegalStateException(replica + " did not sync from " + primary);
        }
    }

    static EcdlSchemaHistory history(String address, String key, boolean wait, int waitAttempts) {
        Configuration c = Configuration.create()
                .with("schema.history.internal.redis.address", address)
                .with("schema.history.internal.redis.key", key)
                .with("schema.history.internal.redis.wait.enabled", String.valueOf(wait))
                .with("schema.history.internal.redis.wait.timeout.ms", "200")
                .with("schema.history.internal.redis.retry.max.attempts", "3")
                .with("schema.history.internal.redis.retry.initial.delay.ms", "50")
                .with("schema.history.internal.redis.retry.max.delay.ms", "100")
                .with(EcdlSchemaHistory.WAIT_ATTEMPTS, String.valueOf(waitAttempts))
                .build();
        EcdlSchemaHistory h = new EcdlSchemaHistory();
        h.configure(c, HistoryRecordComparator.INSTANCE, SchemaHistoryListener.NOOP, false);
        h.start();
        return h;
    }

    static RuntimeException record(EcdlSchemaHistory h, String ddl) {
        try {
            Map<String, Object> source = new HashMap<>();
            source.put("server", "it");
            Map<String, Object> position = new HashMap<>();
            position.put("file", "mysql-bin.000001");
            position.put("pos", 100L);
            h.record(source, position, "shop", ddl);
            return null;
        }
        catch (RuntimeException e) {
            return e;
        }
    }

    static List<String> ddls(EcdlSchemaHistory h) {
        List<String> out = new ArrayList<>();
        h.recoverRecords(r -> out.add(r.document().getString("ddl")));
        return out;
    }

    static long xlen(String address, String key) {
        try (Jedis j = admin(address)) {
            return j.xlen(key);
        }
    }

    static void clean() {
        try (Jedis j = admin(a)) {
            j.del(KEY, KEY + ":upstream", KEY + ":pages", KEY + ":nowait");
        }
    }

    static void replicatedWrite() throws InterruptedException {
        replicaOf(b, a);
        EcdlSchemaHistory h = history(a, KEY, true, 3);
        RuntimeException e1 = record(h, "CREATE TABLE items (id INT PRIMARY KEY)");
        RuntimeException e2 = record(h, "ALTER TABLE items ADD COLUMN v INT");
        check("replicated.ok", e1 == null && e2 == null, e1 + " " + e2);
        check("replicated.on-replica", xlen(b, KEY) == 2, "replica xlen " + xlen(b, KEY));
        check("replicated.read-back", ddls(h).equals(List.of("CREATE TABLE items (id INT PRIMARY KEY)",
                "ALTER TABLE items ADD COLUMN v INT")), "" + ddls(h));
        check("replicated.exists", h.exists(), "");
        h.stop();
    }

    static void noReplicaStops() throws InterruptedException {
        replicaOf(b, null);
        EcdlSchemaHistory h = history(a, KEY, true, 2);
        long before = xlen(a, KEY);
        long t0 = System.currentTimeMillis();
        RuntimeException e = record(h, "ALTER TABLE items DROP COLUMN v");
        long took = System.currentTimeMillis() - t0;
        check("no-replica.refused", e != null && e.getMessage().contains("no replica acknowledged"), String.valueOf(e));
        check("no-replica.removed-again", xlen(a, KEY) == before, before + " -> " + xlen(a, KEY));
        check("no-replica.bounded", took < 5000, took + " ms");
        h.stop();

        EcdlSchemaHistory nowait = history(a, KEY + ":nowait", false, 2);
        check("no-wait.single-node-ok", record(nowait, "CREATE TABLE n (id INT)") == null, "");
        check("no-wait.stored", xlen(a, KEY + ":nowait") == 1, "" + xlen(a, KEY + ":nowait"));
        nowait.stop();
    }

    static void replicaRefusesWrites() throws InterruptedException {
        replicaOf(b, a);
        EcdlSchemaHistory h = history(b, KEY, true, 2);
        RuntimeException e = record(h, "CREATE TABLE r (id INT)");
        check("readonly.refused", e != null && e.getMessage().contains("READONLY"), String.valueOf(e));
        h.stop();
        replicaOf(b, null);
    }

    static void readsUpstreamHistoryInPages() {
        // Written by upstream's class, read by ours: the layout is the same.
        Configuration c = Configuration.create()
                .with("schema.history.internal.redis.address", a)
                .with("schema.history.internal.redis.key", KEY + ":upstream")
                .build();
        RedisSchemaHistory up = new RedisSchemaHistory();
        up.configure(c, HistoryRecordComparator.INSTANCE, SchemaHistoryListener.NOOP, false);
        up.start();
        Map<String, Object> source = new HashMap<>();
        source.put("server", "it");
        Map<String, Object> position = new HashMap<>();
        position.put("pos", 1L);
        up.record(source, position, "shop", "CREATE TABLE u (id INT)");
        up.record(source, position, "shop", "ALTER TABLE u ADD COLUMN w INT");
        up.stop();
        EcdlSchemaHistory ours = history(a, KEY + ":upstream", false, 1);
        check("upstream.readable", ddls(ours).equals(List.of("CREATE TABLE u (id INT)",
                "ALTER TABLE u ADD COLUMN w INT")), "" + ddls(ours));
        ours.stop();

        try (Jedis j = admin(a)) {
            for (int i = 0; i < 2500; i++) {
                // Same millisecond for many entries, so the next-id step is exercised.
                j.xadd(KEY + ":pages", new StreamEntryID(1700000000000L + i / 700, i % 700),
                        Map.of(EcdlSchemaHistory.FIELD, "{\"ddl\":\"d" + i + "\"}"));
            }
        }
        EcdlSchemaHistory pages = history(a, KEY + ":pages", false, 1);
        List<String> got = ddls(pages);
        boolean inOrder = got.size() == 2500;
        for (int i = 0; inOrder && i < 2500; i++) {
            inOrder = got.get(i).equals("d" + i);
        }
        check("pages.all-in-order", inOrder, "read " + got.size());
        pages.stop();
    }

    static void tls(String tlsAddress) {
        HostAndPort hp = HostAndPort.from(tlsAddress);
        String byName = hp.getHost() + ":" + hp.getPort();
        String byIp = "127.0.0.1:" + hp.getPort();
        Object[][] cases = {
                {"history.name-verified", byName, true, true},
                {"history.ip-refused", byIp, true, false},
                {"history.ip-unverified", byIp, false, true},
        };
        for (Object[] c : cases) {
            JedisHistoryBroker hb = new JedisHistoryBroker((String) c[1], 0, null, null, 2000, 2000, true,
                    (Boolean) c[2], "it");
            boolean ok;
            String detail;
            try {
                hb.connect();
                hb.length("x");
                ok = true;
                detail = "connected";
            }
            catch (RuntimeException e) {
                ok = false;
                detail = String.valueOf(e);
            }
            finally {
                hb.close();
            }
            check("tls." + c[0], ok == (Boolean) c[3], detail);
        }
        // The offset store's connection, with the setting application.properties now gives it.
        for (Object[] c : new Object[][]{{"offsets.name-verified", byName, true}, {"offsets.ip-refused", byIp, false}}) {
            JedisOffsetBroker ob = new JedisOffsetBroker((String) c[1], 0, null, null, 2000, 2000, true, true, "it");
            boolean ok;
            String detail;
            try {
                ob.connect();
                ok = true;
                detail = "connected";
            }
            catch (RuntimeException e) {
                ok = false;
                detail = String.valueOf(e);
            }
            finally {
                ob.close();
            }
            check("tls." + c[0], ok == (Boolean) c[2], detail);
        }
    }

    public static void main(String[] args) throws Exception {
        a = System.getenv("ECDL_TEST_VALKEY");
        b = System.getenv("ECDL_TEST_VALKEY2");
        String t = System.getenv("ECDL_TEST_VALKEY_TLS");
        boolean plain = a != null && b != null;
        boolean withTls = t != null && !t.isEmpty();
        if (!plain && !withTls) {
            System.out.println("EcdlSchemaHistoryIT: set ECDL_TEST_VALKEY and ECDL_TEST_VALKEY2, and/or ECDL_TEST_VALKEY_TLS");
            System.exit(2);
        }
        if (plain) {
            clean();
            try {
                replicatedWrite();
                noReplicaStops();
                replicaRefusesWrites();
                readsUpstreamHistoryInPages();
            }
            finally {
                replicaOf(b, null);
                clean();
            }
        }
        if (withTls) {
            tls(t);
        }
        for (String f : failures) {
            System.out.println("FAILED " + f);
        }
        System.out.println("EcdlSchemaHistoryIT: " + passed + " passed, " + failures.size() + " failed");
        System.exit(failures.isEmpty() ? 0 : 1);
    }
}
