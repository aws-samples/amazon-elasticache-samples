/*
 * Tests for EcdlSchemaHistory. Plain Java, like EcdlOffsetBackingStoreTest: run
 * main(); a failure exits non-zero, which fails the image build.
 *
 * Records go through AbstractSchemaHistory.record(), the way a connector
 * stores a DDL, and come back through recoverRecords(). The fake broker behaves
 * like Valkey: a replica answers READONLY, a full server OOM, a dropped
 * connection stays dropped until the next connect, WAIT answers how many
 * replicas acknowledged.
 *
 * EcdlSchemaHistoryIT runs the same class against a real Valkey.
 */
package io.debezium.server.redis;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.LinkedList;
import java.util.List;
import java.util.Map;

import io.debezium.config.Configuration;
import io.debezium.relational.history.HistoryRecord;
import io.debezium.relational.history.HistoryRecordComparator;
import io.debezium.relational.history.SchemaHistoryException;
import io.debezium.relational.history.SchemaHistoryListener;
import io.debezium.server.redis.EcdlSchemaHistory.Settings;
import io.debezium.server.redis.EcdlStreamWriter.ConnectionException;
import io.debezium.server.redis.EcdlStreamWriter.ReplyException;
import io.debezium.storage.redis.history.RedisSchemaHistoryConfig;

public final class EcdlSchemaHistoryTest {

    private static int passed;
    private static final List<String> failures = new ArrayList<>();

    static final String KEY = "ecdl:t:schema_history";

    /** What the fake does on the next calls: null = succeed, "drop" = connection lost. */
    static final class FakeBroker implements EcdlSchemaHistory.HistoryBroker {
        final LinkedHashMap<String, Map<String, String>> stream = new LinkedHashMap<>();
        final LinkedList<String> addErrors = new LinkedList<>();
        final LinkedList<String> connectErrors = new LinkedList<>();
        final LinkedList<String> readErrors = new LinkedList<>();
        final LinkedList<Object> waitReplies = new LinkedList<>();   // Long, or "drop"
        long defaultWait = 1;
        boolean deleteFails;
        boolean connected;
        int connects;
        int adds;
        int waits;
        int deletes;
        int ranges;
        int seq;

        @Override
        public void connect() {
            connects++;
            String err = connectErrors.isEmpty() ? null : connectErrors.removeFirst();
            if ("drop".equals(err)) {
                throw new ConnectionException("Connection refused", null);
            }
            if (err != null) {
                throw new ReplyException(err);
            }
            connected = true;
        }

        @Override
        public String add(String key, Map<String, String> fields) {
            adds++;
            fail(addErrors.isEmpty() ? null : addErrors.removeFirst());
            String id = "1700000000000-" + (seq++);
            stream.put(id, new HashMap<>(fields));
            return id;
        }

        @Override
        public long waitReplicas(int n, long timeoutMs) {
            waits++;
            fail(null);
            Object r = waitReplies.isEmpty() ? (Object) defaultWait : waitReplies.removeFirst();
            if ("drop".equals(r)) {
                connected = false;
                throw new ConnectionException("connection reset during WAIT", null);
            }
            if (r instanceof String) {
                throw new ReplyException((String) r);
            }
            return (Long) r;
        }

        @Override
        public long length(String key) {
            fail(readErrors.isEmpty() ? null : readErrors.removeFirst());
            return stream.size();
        }

        @Override
        public List<Map.Entry<String, Map<String, String>>> range(String key, String afterId, int count) {
            ranges++;
            fail(readErrors.isEmpty() ? null : readErrors.removeFirst());
            List<Map.Entry<String, Map<String, String>>> out = new ArrayList<>();
            boolean after = afterId == null;
            for (Map.Entry<String, Map<String, String>> e : stream.entrySet()) {
                if (after && out.size() < count) {
                    out.add(EcdlSchemaHistory.entry(e.getKey(), e.getValue()));
                }
                if (e.getKey().equals(afterId)) {
                    after = true;
                }
            }
            return out;
        }

        @Override
        public void delete(String key, String id) {
            deletes++;
            if (deleteFails) {
                throw new ConnectionException("connection reset during XDEL", null);
            }
            stream.remove(id);
        }

        private void fail(String err) {
            if (!connected) {
                throw new ConnectionException("not connected", null);
            }
            if ("drop".equals(err)) {
                connected = false;
                throw new ConnectionException("Connection reset", null);
            }
            if (err != null) {
                throw new ReplyException(err);
            }
        }

        @Override
        public void close() {
            connected = false;
        }
    }

    static final class Run {
        final FakeBroker broker = new FakeBroker();
        final List<Long> sleeps = new ArrayList<>();
        final EcdlSchemaHistory history;
        boolean interrupt;

        Run(Settings s) {
            history = new EcdlSchemaHistory(broker, s, ms -> {
                sleeps.add(ms);
                if (interrupt) {
                    throw new InterruptedException("stopping");
                }
            });
            history.configure(Configuration.create().build(), HistoryRecordComparator.INSTANCE,
                    SchemaHistoryListener.NOOP, false);
        }

        Run() {
            this(defaults());
        }

        /** Stores one DDL the way a connector does; returns the error, or null. */
        RuntimeException record(String ddl) {
            try {
                Map<String, Object> source = new HashMap<>();
                source.put("server", "t");
                Map<String, Object> position = new HashMap<>();
                position.put("file", "mysql-bin.000003");
                position.put("pos", 4L);
                history.record(source, position, "shop", ddl);
                return null;
            }
            catch (RuntimeException e) {
                return e;
            }
        }

        List<HistoryRecord> recover() {
            List<HistoryRecord> out = new ArrayList<>();
            history.recoverRecords(out::add);
            return out;
        }
    }

    static Settings defaults() {
        return new Settings().key(KEY).attempts(4).retryDelays(300, 1000).waitForReplica(true, 1000, 3);
    }

    static void check(String name, boolean cond, String detail) {
        if (cond) {
            passed++;
        }
        else {
            failures.add(name + ": " + detail);
        }
    }

    static String ddlOf(Map<String, String> fields) {
        String json = fields.get(EcdlSchemaHistory.FIELD);
        return json == null ? null : json.replaceAll(".*\"ddl\"\\s*:\\s*\"([^\"]*)\".*", "$1");
    }

    // ── writes ──────────────────────────────────────────────────────────────

    static void writesOnceAndWaits() {
        Run r = new Run();
        RuntimeException err = r.record("ALTER TABLE items ADD COLUMN c INT");
        check("store.ok", err == null, String.valueOf(err));
        check("store.one-entry", r.broker.stream.size() == 1, "" + r.broker.stream);
        check("store.field", r.broker.stream.size() == 1
                && "ALTER TABLE items ADD COLUMN c INT".equals(ddlOf(r.broker.stream.values().iterator().next())),
                "" + r.broker.stream);
        check("store.waited", r.broker.waits == 1, "waits " + r.broker.waits);
        check("store.no-sleep", r.sleeps.isEmpty(), "" + r.sleeps);
    }

    static void noWaitWhenDisabled() {
        Run r = new Run(defaults().waitForReplica(false, 1000, 3));
        check("nowait.ok", r.record("CREATE TABLE a (id INT)") == null, "failed");
        check("nowait.no-wait", r.broker.waits == 0, "waits " + r.broker.waits);
    }

    static void waitRepeatedNotTheWrite() {
        Run r = new Run();
        r.broker.waitReplies.add(0L);
        r.broker.waitReplies.add(0L);
        r.broker.waitReplies.add(1L);
        RuntimeException err = r.record("DROP TABLE a");
        check("wait-late.ok", err == null, String.valueOf(err));
        check("wait-late.written-once", r.broker.adds == 1 && r.broker.stream.size() == 1,
                "adds " + r.broker.adds + " stream " + r.broker.stream.size());
        check("wait-late.waits", r.broker.waits == 3, "waits " + r.broker.waits);

        // A refusal of the WAIT itself that clears (BUSY): the same connection
        // still holds the record, so it is waited for again, not written again.
        Run busy = new Run();
        busy.broker.waitReplies.add("BUSY Valkey is busy running a script");
        RuntimeException berr = busy.record("DROP TABLE b");
        check("wait-busy.ok", berr == null, String.valueOf(berr));
        check("wait-busy.written-once", busy.broker.adds == 1 && busy.broker.stream.size() == 1,
                "adds " + busy.broker.adds);
        check("wait-busy.waited-again", busy.broker.waits == 2 && busy.broker.connects == 1,
                "waits " + busy.broker.waits + " connects " + busy.broker.connects);
    }

    static void noReplicaStopsAndRemovesTheEntry() {
        Run r = new Run();
        r.broker.defaultWait = 0;
        RuntimeException err = r.record("ALTER TABLE items DROP COLUMN c");
        check("no-replica.refused", err instanceof SchemaHistoryException, String.valueOf(err));
        check("no-replica.no-cause", err != null && err.getCause() == null, String.valueOf(err == null ? null : err.getCause()));
        check("no-replica.says-why", err != null && err.getMessage().contains("no replica acknowledged")
                && err.getMessage().contains("BROKER_WAIT=false"), String.valueOf(err));
        check("no-replica.written-once", r.broker.adds == 1, "adds " + r.broker.adds);
        check("no-replica.waited-all", r.broker.waits == 3, "waits " + r.broker.waits);
        check("no-replica.removed", r.broker.stream.isEmpty() && r.broker.deletes == 1,
                "stream " + r.broker.stream + " deletes " + r.broker.deletes);

        Run d = new Run();
        d.broker.defaultWait = 0;
        d.broker.deleteFails = true;
        RuntimeException derr = d.record("ALTER TABLE items DROP COLUMN c");
        check("no-replica.delete-fails-still-refused", derr instanceof SchemaHistoryException
                && derr.getMessage().contains("no replica"), String.valueOf(derr));
    }

    static void droppedConnectionWritesAgain() {
        Run r = new Run();
        r.broker.addErrors.add("drop");
        RuntimeException err = r.record("CREATE TABLE b (id INT)");
        check("drop-add.ok", err == null, String.valueOf(err));
        check("drop-add.reconnected", r.broker.connects == 2, "connects " + r.broker.connects);
        check("drop-add.one-entry", r.broker.stream.size() == 1, "" + r.broker.stream.size());
        check("drop-add.backoff", r.sleeps.size() == 1 && r.sleeps.get(0) == 300, "" + r.sleeps);

        // Dropped during WAIT: the new connection may reach a primary without it.
        Run w = new Run();
        w.broker.waitReplies.add("drop");
        RuntimeException werr = w.record("CREATE TABLE c (id INT)");
        check("drop-wait.ok", werr == null, String.valueOf(werr));
        check("drop-wait.written-again", w.broker.adds == 2, "adds " + w.broker.adds);
    }

    static void failoverReconnects() {
        Run r = new Run();
        r.broker.addErrors.add("READONLY You can't write against a read only replica.");
        RuntimeException err = r.record("CREATE TABLE d (id INT)");
        check("readonly.ok", err == null, String.valueOf(err));
        check("readonly.reconnected", r.broker.connects == 2, "connects " + r.broker.connects);
    }

    static void transientRetriedOnSameConnection() {
        Run r = new Run();
        r.broker.addErrors.add("OOM command not allowed when used memory > 'maxmemory'.");
        r.broker.addErrors.add("LOADING Valkey is loading the dataset in memory");
        RuntimeException err = r.record("CREATE TABLE e (id INT)");
        check("transient.ok", err == null, String.valueOf(err));
        check("transient.same-connection", r.broker.connects == 1, "connects " + r.broker.connects);
        check("transient.backoff-doubles", r.sleeps.equals(List.of(300L, 600L)), "" + r.sleeps);
    }

    static void permanentRefusalNotRetried() {
        Run r = new Run();
        r.broker.addErrors.add("NOPERM this user has no permissions to run the 'xadd' command");
        RuntimeException err = r.record("CREATE TABLE f (id INT)");
        check("noperm.refused", err instanceof SchemaHistoryException && err.getMessage().contains("NOPERM"),
                String.valueOf(err));
        check("noperm.once", r.broker.adds == 1 && r.sleeps.isEmpty(), "adds " + r.broker.adds + " " + r.sleeps);
        check("noperm.nothing-to-remove", r.broker.deletes == 0, "deletes " + r.broker.deletes);
    }

    static void brokerDownGivesUp() {
        Run r = new Run();
        r.broker.addErrors.add("drop");
        for (int i = 0; i < 10; i++) {
            r.broker.connectErrors.add("drop");
        }
        RuntimeException err = r.record("CREATE TABLE g (id INT)");
        check("down.refused", err instanceof SchemaHistoryException, String.valueOf(err));
        check("down.attempts", r.broker.connects == 4, "connects " + r.broker.connects);
        check("down.backoff-capped", r.sleeps.equals(List.of(300L, 600L, 1000L)), "" + r.sleeps);
    }

    static void interruptedStops() {
        Run r = new Run();
        r.interrupt = true;
        r.broker.addErrors.add("drop");
        RuntimeException err = r.record("CREATE TABLE h (id INT)");
        check("interrupt.refused", err instanceof SchemaHistoryException, String.valueOf(err));
        check("interrupt.one-sleep", r.sleeps.size() == 1, "" + r.sleeps);
        Thread.interrupted();   // clear the flag the class set again
    }

    // ── reads ───────────────────────────────────────────────────────────────

    static void roundTrip() {
        Run r = new Run();
        r.record("CREATE TABLE items (id INT PRIMARY KEY)");
        r.record("ALTER TABLE items ADD COLUMN v INT");
        List<HistoryRecord> back = r.recover();
        check("round-trip.count", back.size() == 2, "" + back.size());
        check("round-trip.order", back.size() == 2
                && back.get(0).document().getString("ddl").startsWith("CREATE")
                && back.get(1).document().getString("ddl").startsWith("ALTER"), "" + back);
        check("round-trip.position", back.size() == 2
                && "mysql-bin.000003".equals(back.get(0).document().getDocument("position").getString("file")),
                "" + back);
    }

    static void readsInPages() {
        Run r = new Run();
        r.broker.connected = true;
        for (int i = 0; i < 2500; i++) {
            r.broker.stream.put("1-" + i, Map.of(EcdlSchemaHistory.FIELD,
                    "{\"source\":{},\"position\":{\"pos\":" + i + "},\"ddl\":\"d" + i + "\"}"));
        }
        List<HistoryRecord> back = r.recover();
        check("pages.all", back.size() == 2500, "" + back.size());
        check("pages.calls", r.broker.ranges == 3, "ranges " + r.broker.ranges);
        check("pages.order", back.size() == 2500 && "d1000".equals(back.get(1000).document().getString("ddl"))
                && "d2499".equals(back.get(2499).document().getString("ddl")), "");

        Run exact = new Run();
        exact.broker.connected = true;
        for (int i = 0; i < 1000; i++) {
            exact.broker.stream.put("1-" + i, Map.of(EcdlSchemaHistory.FIELD, "{\"ddl\":\"d" + i + "\"}"));
        }
        check("pages.exact", exact.recover().size() == 1000 && exact.broker.ranges == 2, "ranges " + exact.broker.ranges);

        Run empty = new Run();
        check("pages.empty", empty.recover().isEmpty(), "");
    }

    static void unreadableEntryStopsTheStart() {
        String[][] bad = {
                {"no-field", null},
                {"bad-json", "{\"ddl\": "},
        };
        for (String[] c : bad) {
            Run r = new Run();
            r.broker.connected = true;
            r.broker.stream.put("1-0", Map.of(EcdlSchemaHistory.FIELD, "{\"ddl\":\"ok\"}"));
            r.broker.stream.put("1-1", c[1] == null ? Map.of("other", "x") : Map.of(EcdlSchemaHistory.FIELD, c[1]));
            r.broker.stream.put("1-2", Map.of(EcdlSchemaHistory.FIELD, "{\"ddl\":\"after\"}"));
            List<HistoryRecord> got = new ArrayList<>();
            RuntimeException err = null;
            try {
                r.history.recoverRecords(got::add);
            }
            catch (RuntimeException e) {
                err = e;
            }
            check("unreadable." + c[0] + ".refused", err instanceof SchemaHistoryException
                    && err.getMessage().contains("1-1") && err.getMessage().contains(KEY), String.valueOf(err));
            check("unreadable." + c[0] + ".stopped-there", got.size() == 1, "" + got.size());
        }
    }

    static void readRetries() {
        Run r = new Run();
        r.broker.readErrors.add("drop");
        r.broker.stream.put("1-0", Map.of(EcdlSchemaHistory.FIELD, "{\"ddl\":\"x\"}"));
        check("read-retry.ok", r.recover().size() == 1, "");
        check("read-retry.reconnected", r.broker.connects == 2, "connects " + r.broker.connects);

        Run auth = new Run();
        auth.broker.connectErrors.add("WRONGPASS invalid username-password pair");
        RuntimeException err = null;
        try {
            auth.recover();
        }
        catch (RuntimeException e) {
            err = e;
        }
        check("read-auth.refused-at-once", err instanceof SchemaHistoryException && auth.broker.connects == 1
                && err.getMessage().contains("WRONGPASS"), err + " connects " + auth.broker.connects);
    }

    static void existsAndStart() {
        Run r = new Run();
        r.broker.connectErrors.add("drop");
        r.broker.connectErrors.add("drop");
        RuntimeException err = null;
        try {
            r.history.start();
        }
        catch (RuntimeException e) {
            err = e;
        }
        check("start.late-broker", err == null && r.broker.connects == 3, err + " connects " + r.broker.connects);
        check("exists.empty", !r.history.exists(), "");
        r.record("CREATE TABLE x (id INT)");
        check("exists.one", r.history.exists(), "");
        check("storage-exists", r.history.storageExists(), "");

        Run down = new Run();
        for (int i = 0; i < 10; i++) {
            down.broker.connectErrors.add("drop");
        }
        RuntimeException derr = null;
        try {
            down.history.start();
        }
        catch (RuntimeException e) {
            derr = e;
        }
        check("start.down-refused", derr instanceof SchemaHistoryException && down.broker.connects == 4,
                derr + " connects " + down.broker.connects);
    }

    // ── configuration ───────────────────────────────────────────────────────

    static Configuration conf(String... kv) {
        Configuration.Builder b = Configuration.create()
                .with("schema.history.internal.redis.address", "broker:6379")
                .with("schema.history.internal.redis.key", KEY);
        for (int i = 0; i < kv.length; i += 2) {
            b.with(kv[i], kv[i + 1]);
        }
        return b.build();
    }

    static Settings parse(Configuration c) {
        return EcdlSchemaHistory.settings(c, new RedisSchemaHistoryConfig(c));
    }

    static void configuration() {
        Settings d = parse(conf());
        check("config.defaults", d.key.equals(KEY) && d.attempts == 10 && !d.waitForReplica && d.waitAttempts == 30
                && d.warnRecords == 10_000 && d.initialRetryMs == 300 && d.maxRetryMs == 10_000,
                d.key + " " + d.attempts + " " + d.waitForReplica + " " + d.waitAttempts + " " + d.warnRecords);
        Settings s = parse(conf("schema.history.internal.redis.retry.max.attempts", "30",
                "schema.history.internal.redis.wait.enabled", "true",
                "schema.history.internal.redis.wait.timeout.ms", "1500",
                EcdlSchemaHistory.WAIT_ATTEMPTS, "7",
                EcdlSchemaHistory.WARN_RECORDS, "50"));
        check("config.set", s.attempts == 30 && s.waitForReplica && s.waitTimeoutMs == 1500 && s.waitAttempts == 7
                && s.warnRecords == 50, s.attempts + " " + s.waitForReplica + " " + s.waitTimeoutMs + " "
                + s.waitAttempts + " " + s.warnRecords);
        check("config.verify-default-on", EcdlSchemaHistory.bool(conf(), EcdlSchemaHistory.HOSTNAME_VERIFICATION, true), "");
        check("config.verify-off", !EcdlSchemaHistory.bool(conf(EcdlSchemaHistory.HOSTNAME_VERIFICATION, "FALSE"),
                EcdlSchemaHistory.HOSTNAME_VERIFICATION, true), "");
        String[][] bad = {
                {EcdlSchemaHistory.HOSTNAME_VERIFICATION, "maybe"},
                {EcdlSchemaHistory.WAIT_ATTEMPTS, "0"},
                {EcdlSchemaHistory.WARN_RECORDS, "lots"},
        };
        for (String[] b : bad) {
            boolean refused = false;
            try {
                Configuration c = conf(b[0], b[1]);
                if (b[0].equals(EcdlSchemaHistory.HOSTNAME_VERIFICATION)) {
                    EcdlSchemaHistory.bool(c, b[0], true);
                }
                else {
                    parse(c);
                }
            }
            catch (SchemaHistoryException e) {
                refused = true;
            }
            check("config.refuses " + b[0] + "=" + b[1], refused, "accepted");
        }
        // The real constructor path: configure() builds a Jedis broker without connecting.
        EcdlSchemaHistory real = new EcdlSchemaHistory();
        real.configure(conf(), HistoryRecordComparator.INSTANCE, SchemaHistoryListener.NOOP, false);
        check("config.real-configure", real.settings() != null && KEY.equals(real.settings().key), "");
        check("config.real-verifies-hostname", real.broker() instanceof JedisHistoryBroker
                && ((JedisHistoryBroker) real.broker()).verifyHostname, "" + real.broker());
        EcdlSchemaHistory off = new EcdlSchemaHistory();
        off.configure(conf(EcdlSchemaHistory.HOSTNAME_VERIFICATION, "false"), HistoryRecordComparator.INSTANCE,
                SchemaHistoryListener.NOOP, false);
        check("config.real-verify-can-be-off", !((JedisHistoryBroker) off.broker()).verifyHostname, "");
        check("config.next-id", JedisHistoryBroker.next("1700000000000-41").equals("1700000000000-42"),
                JedisHistoryBroker.next("1700000000000-41"));
    }

    public static void main(String[] args) {
        writesOnceAndWaits();
        noWaitWhenDisabled();
        waitRepeatedNotTheWrite();
        noReplicaStopsAndRemovesTheEntry();
        droppedConnectionWritesAgain();
        failoverReconnects();
        transientRetriedOnSameConnection();
        permanentRefusalNotRetried();
        brokerDownGivesUp();
        interruptedStops();
        roundTrip();
        readsInPages();
        unreadableEntryStopsTheStart();
        readRetries();
        existsAndStart();
        configuration();
        if (!failures.isEmpty()) {
            for (String f : failures) {
                System.out.println("FAILED " + f);
            }
            System.out.println("EcdlSchemaHistoryTest: " + failures.size() + " failed, " + passed + " passed");
            System.exit(1);
        }
        System.out.println("EcdlSchemaHistoryTest: " + passed + " checks passed");
    }
}
