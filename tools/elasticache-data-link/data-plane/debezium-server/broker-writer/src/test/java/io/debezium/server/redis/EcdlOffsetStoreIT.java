/*
 * EcdlOffsetBackingStore against a real Valkey and, optionally, PostgreSQL.
 * Not run by the image build (there is no server there); run by hand:
 *
 *   ECDL_TEST_VALKEY=127.0.0.1:A ECDL_TEST_VALKEY2=127.0.0.1:B \
 *   [ECDL_TEST_PG=127.0.0.1:P ECDL_TEST_PG_DB=postgres ECDL_TEST_PG_USER=u ECDL_TEST_PG_PASSWORD=pw] \
 *   java -cp ... io.debezium.server.redis.EcdlOffsetStoreIT
 *
 * A and B are two standalone Valkey servers you started yourself: the test
 * changes their replication, maxmemory and data (FLUSHALL), and restores the
 * replication and memory settings at the end. PostgreSQL needs wal_level=logical;
 * the test creates and drops a slot named ecdl_it_slot.
 */
package io.debezium.server.redis;

import java.io.BufferedReader;
import java.io.File;
import java.io.InputStreamReader;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.Statement;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.TimeUnit;

import io.debezium.server.redis.EcdlOffsetBackingStore.SlotResult;
import io.debezium.server.redis.EcdlOffsetBackingStore.SlotState;

import redis.clients.jedis.HostAndPort;
import redis.clients.jedis.Jedis;

public final class EcdlOffsetStoreIT {

    private static int passed;
    private static final List<String> failures = new ArrayList<>();
    static final String KEY = "ecdl:it:offsets";
    static final String P = "[\"engine\",{\"server\":\"it\"}]";

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

    /** Counts connects, and after `moveAfter` connects reaches `to` instead: an endpoint whose DNS moved. */
    static final class Endpoint implements EcdlOffsetBackingStore.OffsetBroker {
        final JedisOffsetBroker first;
        final JedisOffsetBroker moved;
        int moveAfter = Integer.MAX_VALUE;
        int connects;
        JedisOffsetBroker current;

        Endpoint(String address, String movedTo) {
            first = broker(address);
            moved = movedTo == null ? null : broker(movedTo);
        }

        static JedisOffsetBroker broker(String address) {
            return new JedisOffsetBroker(address, 0, null, null, 2000, 2000, false, false,
                    EcdlOffsetBackingStore.CLIENT_NAME);
        }

        @Override
        public void connect() {
            connects++;
            current = connects > moveAfter ? moved : first;
            current.connect();
        }

        @Override
        public Map<byte[], byte[]> readAll(byte[] key) {
            return current.readAll(key);
        }

        @Override
        public void writeAll(byte[] key, Map<byte[], byte[]> fields) {
            current.writeAll(key, fields);
        }

        @Override
        public long waitReplicas(int n, long timeoutMs) {
            return current.waitReplicas(n, timeoutMs);
        }

        @Override
        public void close() {
            first.close();
            if (moved != null) {
                moved.close();
            }
        }
    }

    static Map<String, String> props(String address, String... kv) {
        Map<String, String> m = new HashMap<>();
        m.put("offset.storage.redis.address", address);
        m.put("offset.storage.redis.key", KEY);
        m.put("offset.storage.redis.retry.initial.delay.ms", "50");
        m.put("offset.storage.redis.retry.max.delay.ms", "200");
        m.put("offset.storage.redis.connection.timeout.ms", "2000");
        m.put("offset.storage.redis.socket.timeout.ms", "2000");
        for (int i = 0; i < kv.length; i += 2) {
            m.put(kv[i], kv[i + 1]);
        }
        return m;
    }

    /** A store wired as the engine wires it: configure(props), then start(). */
    static final class Store {
        final List<Integer> exits = new ArrayList<>();
        final EcdlOffsetBackingStore store;
        Throwable startError;

        Store(EcdlOffsetBackingStore.OffsetBroker broker, Map<String, String> props) {
            store = new EcdlOffsetBackingStore(broker, null, Thread::sleep, exits::add);
            store.configure(props);
        }

        Store start() {
            try {
                store.start();
            }
            catch (Throwable t) {
                startError = t;
            }
            return this;
        }

        String commit(String offset) {
            Map<ByteBuffer, ByteBuffer> v = new HashMap<>();
            v.put(ByteBuffer.wrap(P.getBytes(StandardCharsets.UTF_8)), ByteBuffer.wrap(offset.getBytes(StandardCharsets.UTF_8)));
            try {
                store.set(v, null).get(30, TimeUnit.SECONDS);
                return null;
            }
            catch (ExecutionException e) {
                return String.valueOf(e.getCause().getMessage());
            }
            catch (Exception e) {
                return "unexpected " + e;
            }
        }
    }

    static String stored(String address) {
        try (Jedis j = admin(address)) {
            return j.hget(KEY, P);
        }
    }

    static void waitRole(String address, String role) throws InterruptedException {
        for (int i = 0; i < 100; i++) {
            try (Jedis j = admin(address)) {
                String info = j.info("replication");
                if (info.contains("role:" + role) && (!role.equals("slave") || info.contains("master_link_status:up"))) {
                    return;
                }
            }
            Thread.sleep(100);
        }
        throw new IllegalStateException(address + " did not become " + role);
    }

    // ── Valkey ───────────────────────────────────────────────────────────────

    static void normalAndRestart() {
        try (Jedis j = admin(a)) {
            j.flushAll();
        }
        Store s = new Store(Endpoint.broker(a), props(a)).start();
        check("valkey.first-start", s.startError == null, String.valueOf(s.startError));
        String err = s.commit("{\"pos\":1}");
        check("valkey.commit", err == null && "{\"pos\":1}".equals(stored(a)), err + " stored " + stored(a));
        s.store.stop();
        Store again = new Store(Endpoint.broker(a), props(a, EcdlOffsetBackingStore.EXPECT_OFFSET, "true")).start();
        check("valkey.restart-with-offset", again.startError == null, String.valueOf(again.startError));
        ByteBuffer v = again.store.data().get(ByteBuffer.wrap(P.getBytes(StandardCharsets.UTF_8)));
        check("valkey.restart-loads", v != null && "{\"pos\":1}".equals(StandardCharsets.UTF_8.decode(v.duplicate()).toString()),
                "" + v);
        again.store.stop();
    }

    static void upstreamFormatCompatible() {
        // RedisOffsetBackingStore wrote one HSET per partition, field and value
        // being the converter's JSON. A link moving to this store keeps it.
        try (Jedis j = admin(a)) {
            j.flushAll();
            j.hset(KEY, P, "{\"lsn_proc\":123,\"lsn\":123,\"txId\":7}");
        }
        Store s = new Store(Endpoint.broker(a), props(a, EcdlOffsetBackingStore.EXPECT_OFFSET, "true")).start();
        check("valkey.upstream-hash-starts", s.startError == null, String.valueOf(s.startError));
        String err = s.commit("{\"lsn_proc\":124,\"lsn\":124,\"txId\":8}");
        try (Jedis j = admin(a)) {
            check("valkey.upstream-hash-same-shape", err == null && j.hlen(KEY) == 1
                    && j.hget(KEY, P).contains("124"), err + " " + j.hgetAll(KEY));
        }
        s.store.stop();
    }

    static void missingOffset() {
        try (Jedis j = admin(a)) {
            j.flushAll();   // a wiped broker
        }
        Store s = new Store(Endpoint.broker(a), props(a, EcdlOffsetBackingStore.EXPECT_OFFSET, "true")).start();
        check("valkey.expect-missing-refused", s.startError != null
                && s.startError.getMessage().contains(KEY), String.valueOf(s.startError));
        try (Jedis j = admin(a)) {
            check("valkey.expect-missing-writes-nothing", !j.exists(KEY), "key exists");
        }
        Store first = new Store(Endpoint.broker(a), props(a, EcdlOffsetBackingStore.EXPECT_OFFSET, "false")).start();
        check("valkey.first-start-missing-ok", first.startError == null, String.valueOf(first.startError));
        first.store.stop();
        Store changedKey = new Store(Endpoint.broker(a), props(a, EcdlOffsetBackingStore.EXPECT_OFFSET, "true",
                "offset.storage.redis.key", "ecdl:it:renamed")).start();
        check("valkey.changed-key-refused", changedKey.startError != null, "started");
    }

    static void readonlyStaysReplica() throws Exception {
        try (Jedis j = admin(a)) {
            j.flushAll();
        }
        Endpoint ep = new Endpoint(a, null);
        Store s = new Store(ep, props(a, EcdlOffsetBackingStore.MAX_FAILURES, "3")).start();
        String err = s.commit("{\"pos\":1}");
        check("readonly.before", err == null, err);
        HostAndPort bp = HostAndPort.from(b);
        try (Jedis j = admin(a)) {
            j.replicaof(bp.getHost(), bp.getPort());
        }
        waitRole(a, "slave");
        int before = ep.connects;
        err = s.commit("{\"pos\":2}");
        check("readonly.commit-fails", err != null && err.contains("READONLY"), err);
        check("readonly.reconnected", ep.connects > before, "connects " + before + " -> " + ep.connects);
        check("readonly.counted", s.store.failuresInARow() == 1, "" + s.store.failuresInARow());
        s.commit("{\"pos\":3}");
        s.commit("{\"pos\":4}");
        check("readonly.exit-after-n", s.exits.equals(List.of(EcdlOffsetBackingStore.EXIT_CODE)), "" + s.exits);
        try (Jedis j = admin(a)) {
            j.replicaofNoOne();
        }
        waitRole(a, "master");
        err = s.commit("{\"pos\":5}");
        check("readonly.recovers", err == null && "{\"pos\":5}".equals(stored(a)), err + " " + stored(a));
        s.store.stop();
    }

    static void failoverEndpointMoves() throws Exception {
        HostAndPort ap = HostAndPort.from(a);
        HostAndPort bp = HostAndPort.from(b);
        try (Jedis ja = admin(a); Jedis jb = admin(b)) {
            ja.flushAll();
            jb.replicaof(ap.getHost(), ap.getPort());   // B replicates A
        }
        waitRole(b, "slave");
        Endpoint ep = new Endpoint(a, b);
        ep.moveAfter = 1;   // the first reconnect reaches B, as the endpoint's DNS would after a failover
        Store s = new Store(ep, props(a, "offset.storage.redis.wait.enabled", "true")).start();
        String err = s.commit("{\"pos\":1}");
        check("failover.before", err == null, err);
        try (Jedis ja = admin(a); Jedis jb = admin(b)) {
            jb.replicaofNoOne();                        // promote B
            ja.replicaof(bp.getHost(), bp.getPort());   // old primary becomes its replica
        }
        waitRole(a, "slave");
        err = s.commit("{\"pos\":2}");
        check("failover.committed-in-one-flush", err == null, err);
        check("failover.on-new-primary", "{\"pos\":2}".equals(stored(b)), "" + stored(b));
        check("failover.no-failure-counted", s.store.failuresInARow() == 0, "" + s.store.failuresInARow());
        s.store.stop();
        try (Jedis ja = admin(a)) {
            ja.replicaofNoOne();
        }
        waitRole(a, "master");
    }

    static void outOfMemory() throws Exception {
        try (Jedis j = admin(a)) {
            j.flushAll();
        }
        Store s = new Store(Endpoint.broker(a), props(a)).start();
        String err = s.commit("{\"pos\":1}");
        check("oom.before", err == null, err);
        try (Jedis j = admin(a)) {
            j.configSet("maxmemory-policy", "noeviction");
            j.configSet("maxmemory", "1");
        }
        try {
            err = s.commit("{\"pos\":2}");
            check("oom.commit-fails", err != null && err.contains("OOM"), err);
            check("oom.offset-unchanged", "{\"pos\":1}".equals(stored(a)), stored(a));
        }
        finally {
            try (Jedis j = admin(a)) {
                j.configSet("maxmemory", "0");
            }
        }
        err = s.commit("{\"pos\":3}");
        check("oom.recovers", err == null && "{\"pos\":3}".equals(stored(a)), err + " " + stored(a));
        check("oom.count-reset", s.store.failuresInARow() == 0, "" + s.store.failuresInARow());
        s.store.stop();
    }

    static void connectionDrop() {
        Endpoint ep = new Endpoint(a, null);
        Store s = new Store(ep, props(a)).start();
        s.commit("{\"pos\":1}");
        try (Jedis j = admin(a)) {
            for (String line : j.clientList().split("\n")) {
                if (line.contains("name=" + EcdlOffsetBackingStore.CLIENT_NAME + " ")) {
                    String id = line.replaceAll(".*\\bid=(\\d+).*", "$1").trim();
                    j.clientKill(redis.clients.jedis.params.ClientKillParams.clientKillParams().id(id));
                }
            }
        }
        String err = s.commit("{\"pos\":2}");
        check("drop.committed", err == null && "{\"pos\":2}".equals(stored(a)), err + " " + stored(a));
        check("drop.reconnected", ep.connects == 2, "connects " + ep.connects);
        s.store.stop();
    }

    static void brokerDownAtStart() {
        Store s = new Store(Endpoint.broker("127.0.0.1:1"), props("127.0.0.1:1",
                EcdlOffsetBackingStore.MAX_FAILURES, "3")).start();
        check("down.start-refused", s.startError != null && s.startError.getMessage().contains("could not read"),
                String.valueOf(s.startError));
    }

    /** The real exit: a separate JVM, which must end with status 70 after N failed commits. */
    static void processExits() throws Exception {
        try (Jedis j = admin(a)) {
            j.flushAll();
            j.configSet("maxmemory-policy", "noeviction");
            j.configSet("maxmemory", "1");
        }
        Process p;
        StringBuilder out = new StringBuilder();
        int status;
        try {
            String java = System.getProperty("java.home") + File.separator + "bin" + File.separator + "java";
            p = new ProcessBuilder(java, "-cp", System.getProperty("java.class.path"),
                    "io.debezium.server.redis.EcdlOffsetStoreIT$ExitProbe", a).redirectErrorStream(true).start();
            try (BufferedReader r = new BufferedReader(new InputStreamReader(p.getInputStream(), StandardCharsets.UTF_8))) {
                for (String line; (line = r.readLine()) != null;) {
                    out.append(line).append('\n');
                }
            }
            if (!p.waitFor(120, TimeUnit.SECONDS)) {
                p.destroyForcibly();
            }
            status = p.exitValue();
        }
        finally {
            try (Jedis j = admin(a)) {
                j.configSet("maxmemory", "0");
            }
        }
        String o = out.toString();
        check("exit.status-70", status == EcdlOffsetBackingStore.EXIT_CODE, "status " + status + "\n" + o);
        check("exit.fatal-logged", o.contains("ERROR") && o.contains("FATAL ECDL offset store: 3 offset commits"), o);
        // Each failed commit is an ERROR line: the Debezium-errors metric filter counts them. Three, and
        // the final commit at shutdown.
        check("exit.each-failure-is-error", o.split("ERROR io.debezium.server.redis.EcdlOffsetBackingStore - ECDL offset store: offset commit to", -1).length - 1 >= 3, o);
        check("exit.orderly-shutdown", o.contains("probe: shutdown hook ran"), o);
        check("exit.final-commit-not-blocked", o.contains("probe: final commit failed"), o);
        // Commits 1 and 2 fail and the process goes on; the exit starts during commit 3.
        check("exit.not-early", o.contains("probe: commit 2 failed") && !o.contains("probe: commit 4"), o);
    }

    /** Child process for processExits: commits until the store ends the process. */
    public static final class ExitProbe {
        public static void main(String[] args) throws Exception {
            EcdlOffsetBackingStore store = new EcdlOffsetBackingStore();
            store.configure(props(args[0], EcdlOffsetBackingStore.MAX_FAILURES, "3",
                    EcdlOffsetBackingStore.ATTEMPTS, "2"));
            store.start();
            // As Debezium Server's shutdown does: one last commit, then stop the
            // store. The commit runs on the store's executor thread, so the exit
            // must not be holding that thread.
            Runtime.getRuntime().addShutdownHook(new Thread(() -> {
                System.out.println("probe: shutdown hook ran");
                Map<ByteBuffer, ByteBuffer> v = new HashMap<>();
                v.put(ByteBuffer.wrap(P.getBytes(StandardCharsets.UTF_8)), ByteBuffer.wrap("last".getBytes(StandardCharsets.UTF_8)));
                try {
                    store.set(v, null).get(10, TimeUnit.SECONDS);
                    System.out.println("probe: final commit ok");
                }
                catch (java.util.concurrent.TimeoutException e) {
                    System.out.println("probe: final commit timed out");
                }
                catch (Exception e) {
                    System.out.println("probe: final commit failed");
                }
                store.stop();
            }));
            for (int i = 1; i <= 20; i++) {
                Map<ByteBuffer, ByteBuffer> v = new HashMap<>();
                v.put(ByteBuffer.wrap(P.getBytes(StandardCharsets.UTF_8)), ByteBuffer.wrap(("o" + i).getBytes(StandardCharsets.UTF_8)));
                try {
                    store.set(v, null).get(30, TimeUnit.SECONDS);
                    System.out.println("probe: commit " + i + " ok");
                }
                catch (ExecutionException e) {
                    System.out.println("probe: commit " + i + " failed");
                }
                Thread.sleep(2000);   // the exit runs on its own thread; give it time before the next commit
            }
            System.out.println("probe: still running after 20 commits");
            System.exit(0);
        }
    }

    // ── PostgreSQL ───────────────────────────────────────────────────────────

    static Map<String, String> pgProps(String pg, String slot, String password) {
        HostAndPort hp = HostAndPort.from(pg);
        return props(a, "connector.class", "io.debezium.connector.postgresql.PostgresConnector",
                "database.hostname", hp.getHost(), "database.port", String.valueOf(hp.getPort()),
                "database.dbname", env("ECDL_TEST_PG_DB", "postgres"), "database.user", env("ECDL_TEST_PG_USER", "postgres"),
                "database.password", password, "database.sslmode", "disable", "slot.name", slot);
    }

    static String env(String name, String dflt) {
        String v = System.getenv(name);
        return v == null || v.isEmpty() ? dflt : v;
    }

    static void postgres(String pg) throws Exception {
        HostAndPort hp = HostAndPort.from(pg);
        String password = env("ECDL_TEST_PG_PASSWORD", "");
        String url = "jdbc:postgresql://" + hp.getHost() + ":" + hp.getPort() + "/" + env("ECDL_TEST_PG_DB", "postgres")
                + "?sslmode=disable";
        try (Connection c = DriverManager.getConnection(url, env("ECDL_TEST_PG_USER", "postgres"), password);
                Statement st = c.createStatement()) {
            st.execute("SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots WHERE slot_name = 'ecdl_it_slot'");
            st.execute("SELECT pg_create_logical_replication_slot('ecdl_it_slot', 'pgoutput')");
        }
        try {
            SlotResult present = new PostgresSlotCheck(pgProps(pg, "ecdl_it_slot", password)).check();
            check("pg.slot-present", present.state == SlotState.PRESENT && present.detail.contains("pgoutput"),
                    present.state + " " + present.detail);
            SlotResult absent = new PostgresSlotCheck(pgProps(pg, "ecdl_it_missing", password)).check();
            check("pg.slot-absent", absent.state == SlotState.ABSENT, absent.state + " " + absent.detail);
            SlotResult badPw = new PostgresSlotCheck(pgProps(pg, "ecdl_it_slot", "wrong-pw-9f3")).check();
            check("pg.bad-password-unknown", badPw.state == SlotState.UNKNOWN, badPw.state + " " + badPw.detail);
            check("pg.bad-password-not-echoed", !badPw.detail.contains("wrong-pw-9f3"), badPw.detail);
            Map<String, String> down = pgProps(pg, "ecdl_it_slot", password);
            down.put("database.port", "1");
            long t0 = System.currentTimeMillis();
            SlotResult unreachable = new PostgresSlotCheck(down).check();
            check("pg.unreachable-unknown", unreachable.state == SlotState.UNKNOWN, unreachable.state + " " + unreachable.detail);
            check("pg.unreachable-bounded", System.currentTimeMillis() - t0 < 15_000, (System.currentTimeMillis() - t0) + " ms");

            // The whole start, as the engine runs it.
            try (Jedis j = admin(a)) {
                j.flushAll();
                j.hset(KEY, P, "{\"lsn\":1}");
            }
            Store ok = new Store(Endpoint.broker(a), withExpect(pgProps(pg, "ecdl_it_slot", password))).start();
            check("pg.store-slot-present-starts", ok.startError == null, String.valueOf(ok.startError));
            ok.store.stop();
            Store gone = new Store(Endpoint.broker(a), withExpect(pgProps(pg, "ecdl_it_missing", password))).start();
            check("pg.store-slot-missing-refused", gone.startError != null
                    && gone.startError.getMessage().contains("ecdl_it_missing"), String.valueOf(gone.startError));
            Store cannot = new Store(Endpoint.broker(a), withExpect(pgProps(pg, "ecdl_it_slot", "wrong-pw-9f3"))).start();
            check("pg.store-check-cannot-run-starts", cannot.startError == null, String.valueOf(cannot.startError));
            cannot.store.stop();
            Store first = new Store(Endpoint.broker(a), pgProps(pg, "ecdl_it_missing", password)).start();
            check("pg.store-first-start-no-slot-starts", first.startError == null, String.valueOf(first.startError));
            first.store.stop();
        }
        finally {
            try (Connection c = DriverManager.getConnection(url, env("ECDL_TEST_PG_USER", "postgres"), password);
                    Statement st = c.createStatement()) {
                st.execute("SELECT pg_drop_replication_slot('ecdl_it_slot')");
            }
        }
    }

    static Map<String, String> withExpect(Map<String, String> m) {
        m.put(EcdlOffsetBackingStore.EXPECT_OFFSET, "true");
        return m;
    }

    public static void main(String[] args) throws Exception {
        a = System.getenv("ECDL_TEST_VALKEY");
        b = System.getenv("ECDL_TEST_VALKEY2");
        if (a == null || b == null) {
            System.out.println("EcdlOffsetStoreIT: set ECDL_TEST_VALKEY and ECDL_TEST_VALKEY2 to run it");
            return;
        }
        try {
            normalAndRestart();
            upstreamFormatCompatible();
            missingOffset();
            readonlyStaysReplica();
            failoverEndpointMoves();
            outOfMemory();
            connectionDrop();
            brokerDownAtStart();
            processExits();
            String pg = System.getenv("ECDL_TEST_PG");
            if (pg != null && !pg.isEmpty()) {
                postgres(pg);
            }
            else {
                System.out.println("SKIP PostgreSQL checks (ECDL_TEST_PG not set)");
            }
        }
        catch (Throwable t) {
            t.printStackTrace();
            failures.add("aborted: " + t);
        }
        finally {
            for (String s : new String[] { a, b }) {
                try (Jedis j = admin(s)) {
                    j.replicaofNoOne();
                    j.configSet("maxmemory", "0");
                }
            }
        }
        if (!failures.isEmpty()) {
            System.err.println("EcdlOffsetStoreIT: " + failures.size() + " FAILED, " + passed + " passed");
            failures.forEach(f -> System.err.println("  FAIL " + f));
            System.exit(1);
        }
        System.out.println("EcdlOffsetStoreIT: " + passed + " checks passed");
    }
}
