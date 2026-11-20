/*
 * Tests for EcdlOffsetBackingStore. Plain Java, like EcdlStreamWriterTest: run
 * main(); a failure exits non-zero, which fails the image build.
 *
 * Commits go through MemoryOffsetBackingStore.set(), on its executor thread, the
 * way Debezium's engine flushes them, so a failed commit is seen as the engine
 * sees it: a failed future. The fake broker behaves like Valkey: a replica
 * answers READONLY to writes, a full server OOM, a dropped connection stays
 * dropped until the next connect.
 *
 * EcdlOffsetStoreIT runs the same store against a real Valkey (and PostgreSQL).
 */
package io.debezium.server.redis;

import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.TimeUnit;

import io.debezium.config.Configuration;
import io.debezium.server.redis.EcdlOffsetBackingStore.Settings;
import io.debezium.server.redis.EcdlOffsetBackingStore.SlotResult;
import io.debezium.server.redis.EcdlOffsetBackingStore.SlotState;
import io.debezium.server.redis.EcdlStreamWriter.ConnectionException;
import io.debezium.server.redis.EcdlStreamWriter.ReplyException;
import io.debezium.storage.redis.offset.RedisOffsetBackingStoreConfig;

public final class EcdlOffsetBackingStoreTest {

    private static int passed;
    private static final List<String> failures = new ArrayList<>();

    static final String KEY = "ecdl:t:offsets";

    /** What the fake does on the next calls: null = succeed. */
    static final class FakeBroker implements EcdlOffsetBackingStore.OffsetBroker {
        final Map<String, String> hash = new HashMap<>();
        final LinkedList<String> writeErrors = new LinkedList<>();     // "drop" = connection lost
        final LinkedList<String> readErrors = new LinkedList<>();
        final LinkedList<String> connectErrors = new LinkedList<>();
        final LinkedList<Object> waitReplies = new LinkedList<>();     // Long, or "drop"
        boolean connected;
        int connects;
        int writes;
        int waits;
        int closes;

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
        public Map<byte[], byte[]> readAll(byte[] key) {
            String err = readErrors.isEmpty() ? null : readErrors.removeFirst();
            fail(err);
            Map<byte[], byte[]> out = new HashMap<>();
            if (KEY.equals(new String(key, StandardCharsets.UTF_8))) {
                hash.forEach((k, v) -> out.put(k.getBytes(StandardCharsets.UTF_8), v.getBytes(StandardCharsets.UTF_8)));
            }
            return out;
        }

        @Override
        public void writeAll(byte[] key, Map<byte[], byte[]> fields) {
            String err = writeErrors.isEmpty() ? null : writeErrors.removeFirst();
            fail(err);
            writes++;
            fields.forEach((k, v) -> hash.put(new String(k, StandardCharsets.UTF_8), new String(v, StandardCharsets.UTF_8)));
        }

        @Override
        public long waitReplicas(int n, long timeoutMs) {
            waits++;
            Object r = waitReplies.isEmpty() ? (Object) 1L : waitReplies.removeFirst();
            if ("drop".equals(r)) {
                connected = false;
                throw new ConnectionException("connection reset during WAIT", null);
            }
            return (Long) r;
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
            closes++;
            connected = false;
        }
    }

    static final class FakeSlot implements EcdlOffsetBackingStore.SlotCheck {
        final SlotResult result;
        final RuntimeException throwing;
        int calls;

        FakeSlot(SlotState state) {
            result = new SlotResult(state, "fake " + state);
            throwing = null;
        }

        FakeSlot(RuntimeException e) {
            result = null;
            throwing = e;
        }

        @Override
        public SlotResult check() {
            calls++;
            if (throwing != null) {
                throw throwing;
            }
            return result;
        }

        @Override
        public String describe() {
            return "'ecdl_t' on db:5432/shop";
        }
    }

    static final class Run {
        final FakeBroker broker = new FakeBroker();
        final List<Long> sleeps = new ArrayList<>();
        final List<Integer> exits = new ArrayList<>();
        Settings settings = new Settings().key(KEY).retryDelays(300, 1000).attempts(3).maxFailures(4);
        EcdlOffsetBackingStore store;
        Throwable startError;

        Run start() {
            store = new EcdlOffsetBackingStore(broker, settings, sleeps::add, exits::add);
            try {
                store.start();
            }
            catch (Throwable t) {
                startError = t;
            }
            return this;
        }

        /** One engine flush; null when it succeeded, else the failure's message. */
        String commit(String partition, String offset) {
            Map<ByteBuffer, ByteBuffer> values = new HashMap<>();
            values.put(buf(partition), buf(offset));
            try {
                store.set(values, null).get(10, TimeUnit.SECONDS);
                return null;
            }
            catch (ExecutionException e) {
                return String.valueOf(e.getCause().getMessage());
            }
            catch (Exception e) {
                return "unexpected " + e;
            }
        }

        void stop() {
            if (store != null) {
                store.stop();
            }
        }
    }

    static ByteBuffer buf(String s) {
        return ByteBuffer.wrap(s.getBytes(StandardCharsets.UTF_8));
    }

    static void check(String name, boolean cond, String detail) {
        if (cond) {
            passed++;
        }
        else {
            failures.add(name + ": " + detail);
        }
    }

    static final String P = "[\"engine\",{\"server\":\"t\"}]";

    // ── commits (Jira story F004) ───────────────────────────────────────────────────────

    static void normalCommit() {
        Run r = new Run().start();
        String err = r.commit(P, "{\"pos\":1}");
        check("normal.ok", err == null, err);
        check("normal.stored", "{\"pos\":1}".equals(r.broker.hash.get(P)), "" + r.broker.hash);
        check("normal.one-connect", r.broker.connects == 1, "connects " + r.broker.connects);
        check("normal.no-sleep", r.sleeps.isEmpty(), "" + r.sleeps);
        check("normal.no-wait-when-off", r.broker.waits == 0, "waits " + r.broker.waits);
        err = r.commit(P, "{\"pos\":2}");
        check("normal.second", err == null && "{\"pos\":2}".equals(r.broker.hash.get(P)), err + " " + r.broker.hash);
        r.stop();
        check("normal.stop-closes", r.broker.closes >= 1, "closes " + r.broker.closes);
    }

    static void allPartitionsInOneWrite() {
        Run r = new Run().start();
        Map<ByteBuffer, ByteBuffer> values = new HashMap<>();
        values.put(buf("p1"), buf("o1"));
        values.put(buf("p2"), buf("o2"));
        try {
            r.store.set(values, null).get(10, TimeUnit.SECONDS);
        }
        catch (Exception e) {
            check("partitions.ok", false, String.valueOf(e));
        }
        check("partitions.one-hset", r.broker.writes == 1, "writes " + r.broker.writes);
        check("partitions.both", "o1".equals(r.broker.hash.get("p1")) && "o2".equals(r.broker.hash.get("p2")),
                "" + r.broker.hash);
        r.stop();
    }

    static void readonlyReconnects() {
        Run r = new Run().start();
        r.broker.writeErrors.add("READONLY You can't write against a read only replica.");
        String err = r.commit(P, "{\"pos\":1}");
        check("readonly.committed", err == null, err);
        check("readonly.reconnected", r.broker.connects == 2, "connects " + r.broker.connects);
        check("readonly.backoff", r.sleeps.equals(List.of(300L)), "" + r.sleeps);
        check("readonly.no-failure", r.store.failuresInARow() == 0, "" + r.store.failuresInARow());
        r.stop();
    }

    static void dropReconnects() {
        Run r = new Run().start();
        r.broker.writeErrors.add("drop");
        String err = r.commit(P, "{\"pos\":1}");
        check("drop.committed", err == null, err);
        check("drop.reconnected", r.broker.connects == 2, "connects " + r.broker.connects);
        r.stop();
    }

    static void reconnectRefusedThenWorks() {
        Run r = new Run().start();
        r.broker.writeErrors.add("drop");
        r.broker.connectErrors.add("drop");     // first reconnect: still down
        String err = r.commit(P, "{\"pos\":1}");
        check("reconnect-refused.committed", err == null, err);
        check("reconnect-refused.connects", r.broker.connects == 3, "connects " + r.broker.connects);
        check("reconnect-refused.backoff", r.sleeps.equals(List.of(300L, 600L)), "" + r.sleeps);
        r.stop();
    }

    static void oomRetriesSameConnection() {
        Run r = new Run().start();
        r.broker.writeErrors.add("OOM command not allowed when used memory > 'maxmemory'.");
        String err = r.commit(P, "{\"pos\":1}");
        check("oom-short.committed", err == null, err);
        check("oom-short.same-connection", r.broker.connects == 1, "connects " + r.broker.connects);
        r.stop();
    }

    static void oomLastingFailsTheCommit() {
        Run r = new Run().start();
        for (int i = 0; i < 3; i++) {
            r.broker.writeErrors.add("OOM command not allowed");
        }
        String err = r.commit(P, "{\"pos\":1}");
        check("oom-lasting.fails", err != null && err.contains("OOM"), err);
        check("oom-lasting.bounded", r.sleeps.equals(List.of(300L, 600L)), "" + r.sleeps);
        check("oom-lasting.counted", r.store.failuresInARow() == 1, "" + r.store.failuresInARow());
        check("oom-lasting.no-exit-yet", r.exits.isEmpty(), "" + r.exits);
        // The next flush (memory freed) writes the newest offset and resets the count.
        err = r.commit(P, "{\"pos\":2}");
        check("oom-lasting.recovers", err == null && "{\"pos\":2}".equals(r.broker.hash.get(P)),
                err + " " + r.broker.hash);
        check("oom-lasting.reset", r.store.failuresInARow() == 0, "" + r.store.failuresInARow());
        r.stop();
    }

    static void backoffCapped() {
        Run r = new Run();
        r.settings.attempts(5).retryDelays(300, 1000);
        r.start();
        for (int i = 0; i < 5; i++) {
            r.broker.writeErrors.add("LOADING Valkey is loading the dataset in memory");
        }
        r.commit(P, "x");
        check("backoff.capped", r.sleeps.equals(List.of(300L, 600L, 1000L, 1000L)), "" + r.sleeps);
        r.stop();
    }

    static void permanentRefusalNotRetried() {
        for (String reply : List.of("NOPERM this user has no permissions to run the 'hset' command",
                "WRONGTYPE Operation against a key holding the wrong kind of value", "NOAUTH Authentication required.")) {
            Run r = new Run().start();
            r.broker.writeErrors.add(reply);
            String err = r.commit(P, "x");
            String w = EcdlStreamWriter.firstWord(reply);
            check("permanent." + w + ".fails", err != null && err.contains(w), err);
            check("permanent." + w + ".no-retry", r.sleeps.isEmpty(), "" + r.sleeps);
            check("permanent." + w + ".counted", r.store.failuresInARow() == 1, "" + r.store.failuresInARow());
            r.stop();
        }
    }

    static void exitAfterNFailuresInARow() {
        Run r = new Run();
        r.settings.maxFailures(4).attempts(1);
        r.start();
        for (int i = 0; i < 20; i++) {
            r.broker.writeErrors.add("READONLY You can't write against a read only replica.");
        }
        for (int i = 1; i <= 3; i++) {
            r.commit(P, "x" + i);
        }
        check("exit.not-before-n", r.exits.isEmpty(), "" + r.exits);
        String err = r.commit(P, "x4");
        check("exit.at-n", r.exits.equals(List.of(EcdlOffsetBackingStore.EXIT_CODE)), "" + r.exits);
        check("exit.commit-still-fails", err != null, "" + err);
        r.commit(P, "x5");
        check("exit.only-once", r.exits.size() == 1, "" + r.exits);
        r.stop();
    }

    static void successResetsTheCount() {
        Run r = new Run();
        r.settings.maxFailures(3).attempts(1);
        r.start();
        r.broker.writeErrors.addAll(List.of("OOM", "OOM"));
        r.commit(P, "a");
        r.commit(P, "b");
        String ok = r.commit(P, "c");
        r.broker.writeErrors.addAll(List.of("OOM", "OOM"));
        r.commit(P, "d");
        r.commit(P, "e");
        check("reset.ok-between", ok == null, ok);
        check("reset.no-exit", r.exits.isEmpty(), "" + r.exits);
        check("reset.count", r.store.failuresInARow() == 2, "" + r.store.failuresInARow());
        r.stop();
    }

    static void waitForReplica() {
        Run r = new Run();
        r.settings.waitForReplica(true, 500);
        r.start();
        r.broker.waitReplies.add(0L);
        String err = r.commit(P, "a");
        check("wait.no-ack-still-commits", err == null, err);
        check("wait.asked", r.broker.waits == 1, "waits " + r.broker.waits);
        r.broker.waitReplies.add("drop");
        err = r.commit(P, "b");
        check("wait.drop-retried", err == null && r.broker.connects == 2 && r.broker.writes == 3,
                err + " connects " + r.broker.connects + " writes " + r.broker.writes);
        r.stop();
    }

    static void nothingToCommit() {
        Run r = new Run().start();
        try {
            r.store.set(new HashMap<>(), null).get(10, TimeUnit.SECONDS);
        }
        catch (Exception e) {
            check("empty.ok", false, String.valueOf(e));
        }
        check("empty.no-write", r.broker.writes == 0, "writes " + r.broker.writes);
        r.stop();
    }

    // ── start (load, Jira stories F005, F008) ─────────────────────────────────────────────

    static void loadsStoredOffsets() {
        Run r = new Run();
        r.broker.hash.put(P, "{\"lsn\":42}");
        r.start();
        check("load.ok", r.startError == null, String.valueOf(r.startError));
        ByteBuffer v = r.store.data().get(buf(P));
        check("load.value", v != null && "{\"lsn\":42}".equals(StandardCharsets.UTF_8.decode(v.duplicate()).toString()),
                "" + r.store.data());
        r.stop();
    }

    static void loadRetriesThenGivesUp() {
        Run r = new Run();
        r.settings.maxFailures(3);
        r.broker.connectErrors.addAll(List.of("drop", "drop", "drop"));
        r.start();
        check("load-down.refused", r.startError != null && r.startError.getMessage().contains("could not read"),
                String.valueOf(r.startError));
        check("load-down.attempts", r.broker.connects == 3, "connects " + r.broker.connects);
        check("load-down.no-exit-hook", r.exits.isEmpty(), "" + r.exits);

        Run late = new Run();
        late.broker.connectErrors.addAll(List.of("drop", "drop"));
        late.broker.hash.put(P, "x");
        late.start();
        check("load-late.ok", late.startError == null, String.valueOf(late.startError));
        late.stop();

        Run auth = new Run();
        auth.broker.connectErrors.add("WRONGPASS invalid username-password pair");
        auth.start();
        check("load-auth.refused-at-once", auth.startError != null && auth.broker.connects == 1,
                auth.startError + " connects " + auth.broker.connects);
    }

    static void missingOffsetRefusedWhenExpected() {
        Run r = new Run();
        r.settings.expectOffset(true);
        r.start();
        String m = r.startError == null ? "" : r.startError.getMessage();
        check("expect-missing.refused", r.startError != null, "started");
        check("expect-missing.names-key", m.contains(KEY), m);
        check("expect-missing.recovery", m.contains("ecdl apply --reload"), m);
        check("expect-missing.write-nothing", r.broker.writes == 0, "writes " + r.broker.writes);
    }

    static void emptyOffsetRefusedWhenExpected() {
        Run r = new Run();
        r.settings.expectOffset(true);
        r.broker.hash.put(P, "");
        r.start();
        check("expect-empty.refused", r.startError != null, "started");
    }

    static void missingOffsetOnFirstStartAllowed() {
        Run r = new Run().start();
        check("first-start.ok", r.startError == null, String.valueOf(r.startError));
        r.stop();
    }

    static void presentOffsetStartsWhenExpected() {
        Run r = new Run();
        r.settings.expectOffset(true);
        r.broker.hash.put(P, "{\"file\":\"mysql-bin.000003\",\"pos\":154}");
        r.start();
        check("expect-present.ok", r.startError == null, String.valueOf(r.startError));
        r.stop();
    }

    static void slotChecks() {
        Object[][] cases = {
            { "absent", new FakeSlot(SlotState.ABSENT), true, false },
            { "present", new FakeSlot(SlotState.PRESENT), true, true },
            { "unknown", new FakeSlot(SlotState.UNKNOWN), true, true },
            { "throws", new FakeSlot(new RuntimeException("boom")), true, true },
            { "not-expected", new FakeSlot(SlotState.ABSENT), false, true },
        };
        for (Object[] c : cases) {
            Run r = new Run();
            FakeSlot slot = (FakeSlot) c[1];
            r.settings.expectOffset((Boolean) c[2]).slotCheck(slot);
            r.broker.hash.put(P, "{\"lsn\":1}");
            r.start();
            boolean starts = (Boolean) c[3];
            check("slot." + c[0] + (starts ? ".starts" : ".refused"), (r.startError == null) == starts,
                    String.valueOf(r.startError));
            if (!starts && r.startError != null) {
                String m = r.startError.getMessage();
                check("slot." + c[0] + ".explains", m.contains("ecdl_t") && m.contains("blue/green")
                        && m.contains("ecdl apply --reload"), m);
            }
            check("slot." + c[0] + ".asked", slot.calls == ((Boolean) c[2] ? 1 : 0), "calls " + slot.calls);
            r.stop();
        }
        // A missing offset is refused before the slot is even looked at.
        Run r = new Run();
        FakeSlot slot = new FakeSlot(SlotState.PRESENT);
        r.settings.expectOffset(true).slotCheck(slot);
        r.start();
        check("slot.offset-first", r.startError != null && slot.calls == 0, r.startError + " calls " + slot.calls);
    }

    // ── configuration ────────────────────────────────────────────────────────

    static Map<String, String> props(String... kv) {
        Map<String, String> m = new HashMap<>();
        m.put("offset.storage.redis.address", "broker:6379");
        m.put("offset.storage.redis.key", KEY);
        for (int i = 0; i < kv.length; i += 2) {
            m.put(kv[i], kv[i + 1]);
        }
        return m;
    }

    static Settings parse(Map<String, String> m) {
        return EcdlOffsetBackingStore.settings(m, new RedisOffsetBackingStoreConfig(Configuration.from(m)));
    }

    static void configuration() {
        Settings d = parse(props());
        check("config.defaults", d.key.equals(KEY) && !d.expectOffset && d.maxFailures == 10 && d.attempts == 3
                && d.slotCheck == null, "key " + d.key + " expect " + d.expectOffset + " max " + d.maxFailures);
        Settings s = parse(props(EcdlOffsetBackingStore.EXPECT_OFFSET, "true",
                EcdlOffsetBackingStore.MAX_FAILURES, "7", EcdlOffsetBackingStore.ATTEMPTS, "2",
                "offset.storage.redis.wait.enabled", "true", "offset.storage.redis.wait.timeout.ms", "1500"));
        check("config.set", s.expectOffset && s.maxFailures == 7 && s.attempts == 2 && s.waitForReplica
                && s.waitTimeoutMs == 1500, "expect " + s.expectOffset + " max " + s.maxFailures);
        check("config.empty-expect-is-false", !parse(props(EcdlOffsetBackingStore.EXPECT_OFFSET, "")).expectOffset, "");
        check("config.case", parse(props(EcdlOffsetBackingStore.EXPECT_OFFSET, "TRUE")).expectOffset, "");
        for (String[] bad : new String[][] { { EcdlOffsetBackingStore.EXPECT_OFFSET, "yes" },
                { EcdlOffsetBackingStore.MAX_FAILURES, "0" }, { EcdlOffsetBackingStore.MAX_FAILURES, "ten" },
                { EcdlOffsetBackingStore.ATTEMPTS, "-1" } }) {
            boolean refused;
            try {
                parse(props(bad));
                refused = false;
            }
            catch (RuntimeException e) {
                refused = e.getMessage().contains(bad[0]);
            }
            check("config.refuses " + bad[0] + "=" + bad[1], refused, "accepted");
        }
        Settings pg = parse(props("connector.class", "io.debezium.connector.postgresql.PostgresConnector",
                "database.hostname", "db.example", "database.port", "5433", "database.dbname", "shop db",
                "slot.name", "ecdl_t", "database.user", "u", "database.password", "secret"));
        check("config.pg-slot-check", pg.slotCheck instanceof PostgresSlotCheck, "" + pg.slotCheck);
        if (pg.slotCheck instanceof PostgresSlotCheck) {
            PostgresSlotCheck c = (PostgresSlotCheck) pg.slotCheck;
            check("config.pg-url", c.url().equals("jdbc:postgresql://db.example:5433/shop+db"), c.url());
            check("config.pg-describe", c.describe().equals("'ecdl_t' on db.example:5433/shop db"), c.describe());
            check("config.pg-no-password-in-describe", !c.describe().contains("secret"), c.describe());
        }
        Settings my = parse(props("connector.class", "io.debezium.connector.mysql.MySqlConnector"));
        check("config.mysql-no-slot-check", my.slotCheck == null, "" + my.slotCheck);
        Settings dflt = parse(props("connector.class", "io.debezium.connector.postgresql.PostgresConnector"));
        check("config.pg-default-slot", dflt.slotCheck != null && dflt.slotCheck.describe().startsWith("'debezium'"),
                dflt.slotCheck == null ? "null" : dflt.slotCheck.describe());
    }

    static void slotCheckWithoutDriver() {
        // The image has the driver; this build stage does not. A check that
        // cannot run says so instead of answering ABSENT.
        boolean haveDriver;
        try {
            Class.forName(PostgresSlotCheck.DRIVER);
            haveDriver = true;
        }
        catch (ClassNotFoundException e) {
            haveDriver = false;
        }
        if (haveDriver) {
            return;
        }
        PostgresSlotCheck c = new PostgresSlotCheck(props("database.hostname", "127.0.0.1"));
        SlotResult r = c.check();
        check("slot-driver.unknown", r.state == SlotState.UNKNOWN && r.detail.contains("driver"), r.state + " " + r.detail);
    }

    public static void main(String[] args) {
        normalCommit();
        allPartitionsInOneWrite();
        readonlyReconnects();
        dropReconnects();
        reconnectRefusedThenWorks();
        oomRetriesSameConnection();
        oomLastingFailsTheCommit();
        backoffCapped();
        permanentRefusalNotRetried();
        exitAfterNFailuresInARow();
        successResetsTheCount();
        waitForReplica();
        nothingToCommit();
        loadsStoredOffsets();
        loadRetriesThenGivesUp();
        missingOffsetRefusedWhenExpected();
        emptyOffsetRefusedWhenExpected();
        missingOffsetOnFirstStartAllowed();
        presentOffsetStartsWhenExpected();
        slotChecks();
        configuration();
        slotCheckWithoutDriver();
        if (!failures.isEmpty()) {
            System.err.println("EcdlOffsetBackingStoreTest: " + failures.size() + " FAILED, " + passed + " passed");
            failures.forEach(f -> System.err.println("  FAIL " + f));
            System.exit(1);
        }
        System.out.println("EcdlOffsetBackingStoreTest: " + passed + " checks passed");
    }
}
