/*
 * ECDL offset store: where Debezium keeps its log position, in the broker.
 *
 * Replaces io.debezium.storage.redis.offset.RedisOffsetBackingStore (3.0.6),
 * selected by debezium.source.offset.storage in application.properties. The
 * stored data is exactly upstream's -- one hash, one field per source partition
 * -- so a link started with the upstream store keeps its position.
 *
 * What upstream did wrong (review findings Jira stories F004, F005, F008):
 *
 *   1. A commit was retried forever, on the same connection, logging WARN.
 *      READONLY (after a failover the old connection reaches a replica) and OOM
 *      never clear on that connection, so the offset stopped moving for good
 *      while the task looked healthy: no ERROR, no restart. Every later commit
 *      queued behind the stuck one.
 *   2. A missing offset key (broker wiped or replaced, FLUSHALL, OffsetKey
 *      changed) looked exactly like a first start: Debezium started at the
 *      source's current log position and every change since the last committed
 *      position was skipped without a word.
 *   3. On PostgreSQL, a missing replication slot (writer changed by blue/green
 *      or a major upgrade, slot dropped) made Debezium create a new slot at the
 *      current WAL position: the same silent skip.
 *
 * This store:
 *   - tries each commit a few times with back-off, reconnecting after a dropped
 *     connection or READONLY (so a failover reaches the new primary). A commit
 *     that still fails is logged as ERROR (the Debezium-errors metric filter
 *     counts it) and fails the flush. After OFFSET_COMMIT_MAX_FAILURES failed
 *     commits in a row the process exits non-zero, so ECS restarts the task
 *     instead of it running on with a frozen offset. Nothing is lost by exiting:
 *     the restart replays from the last committed position;
 *   - with EXPECT_OFFSET=true (the deployer sets it once it has seen this link
 *     commit an offset) refuses to start when the offset key is missing or
 *     empty, and on PostgreSQL when the replication slot is missing. It logs
 *     what happened and how to recover, and the task fails instead of skipping.
 */
package io.debezium.server.redis;

import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;
import java.util.Set;

import org.apache.kafka.connect.errors.ConnectException;
import org.apache.kafka.connect.runtime.WorkerConfig;
import org.apache.kafka.connect.storage.MemoryOffsetBackingStore;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import io.debezium.DebeziumException;
import io.debezium.config.Configuration;
import io.debezium.server.redis.EcdlStreamWriter.ConnectionException;
import io.debezium.server.redis.EcdlStreamWriter.ReplyException;
import io.debezium.server.redis.EcdlStreamWriter.Sleeper;
import io.debezium.storage.redis.offset.RedisOffsetBackingStoreConfig;

public class EcdlOffsetBackingStore extends MemoryOffsetBackingStore {

    private static final Logger LOGGER = LoggerFactory.getLogger(EcdlOffsetBackingStore.class);

    static final String EXPECT_OFFSET = "offset.storage.ecdl.expect.offset";
    static final String MAX_FAILURES = "offset.storage.ecdl.commit.max.failures";
    static final String ATTEMPTS = "offset.storage.ecdl.commit.attempts";
    static final String HOSTNAME_VERIFICATION = "offset.storage.redis.ssl.hostname.verification.enabled";
    static final String CLIENT_NAME = "debezium:offsets";

    /** EX_SOFTWARE: the offset store stopped the process on purpose. */
    static final int EXIT_CODE = 70;
    /** If the orderly shutdown hangs, the process is halted this long after the exit starts. */
    static final long HALT_AFTER_MS = 60_000;

    /** The broker, as this store needs it. JedisOffsetBroker is the real one. */
    public interface OffsetBroker {
        /** Connect (or reconnect). Throws ConnectionException, or ReplyException (NOAUTH ...). */
        void connect();

        /** HGETALL. */
        Map<byte[], byte[]> readAll(byte[] key);

        /** HSET of every field at once. Throws ReplyException with the server's error (READONLY, OOM ...). */
        void writeAll(byte[] key, Map<byte[], byte[]> fields);

        /** WAIT numReplicas timeoutMs. */
        long waitReplicas(int numReplicas, long timeoutMs);

        void close();
    }

    /** Ends the process. Production: System.exit on its own thread, halt if that hangs. */
    public interface Exit {
        void exit(int status);
    }

    /** Whether the PostgreSQL replication slot exists; see PostgresSlotCheck. */
    public interface SlotCheck {
        SlotResult check();

        /** For log lines: which slot on which database. */
        String describe();
    }

    public enum SlotState {
        PRESENT,
        ABSENT,
        /** The check itself could not run (no driver, no connection, no permission). */
        UNKNOWN
    }

    public static final class SlotResult {
        final SlotState state;
        final String detail;

        public SlotResult(SlotState state, String detail) {
            this.state = state;
            this.detail = detail;
        }
    }

    public static final class Settings {
        String key = "metadata:debezium:offsets";
        boolean expectOffset;
        int maxFailures = 10;
        int attempts = 3;
        long initialRetryMs = 300;
        long maxRetryMs = 10_000;
        boolean waitForReplica;
        long waitTimeoutMs = 1000;
        SlotCheck slotCheck;

        public Settings key(String v) {
            key = v;
            return this;
        }

        public Settings expectOffset(boolean v) {
            expectOffset = v;
            return this;
        }

        public Settings maxFailures(int v) {
            maxFailures = v;
            return this;
        }

        public Settings attempts(int v) {
            attempts = v;
            return this;
        }

        public Settings retryDelays(long initial, long max) {
            initialRetryMs = initial;
            maxRetryMs = max;
            return this;
        }

        public Settings waitForReplica(boolean v, long timeoutMs) {
            waitForReplica = v;
            waitTimeoutMs = timeoutMs;
            return this;
        }

        public Settings slotCheck(SlotCheck v) {
            slotCheck = v;
            return this;
        }
    }

    private OffsetBroker broker;
    private Settings settings;
    private final Sleeper sleeper;
    private final Exit exit;
    private boolean connected;
    private int failuresInARow;
    private boolean exiting;

    /** Used by the engine, which instantiates the class named in offset.storage. */
    public EcdlOffsetBackingStore() {
        this(null, null, Thread::sleep, EcdlOffsetBackingStore::exitProcess);
    }

    /** For tests: a broker and settings are given, configure() is then not needed. */
    EcdlOffsetBackingStore(OffsetBroker broker, Settings settings, Sleeper sleeper, Exit exit) {
        this.broker = broker;
        this.settings = settings;
        this.sleeper = sleeper;
        this.exit = exit;
    }

    @Override
    public void configure(WorkerConfig config) {
        super.configure(config);
        configure(config.originalsStrings());
    }

    /** The engine passes the whole source config: offset.storage.* and the connector's own. */
    void configure(Map<String, String> props) {
        RedisOffsetBackingStoreConfig redis = new RedisOffsetBackingStoreConfig(Configuration.from(props));
        settings = settings(props, redis);
        if (broker == null) {
            broker = new JedisOffsetBroker(redis.getAddress(), redis.getDbIndex(), redis.getUser(),
                    redis.getPassword(), redis.getConnectionTimeout(), redis.getSocketTimeout(),
                    redis.isSslEnabled(), bool(props, HOSTNAME_VERIFICATION, false), CLIENT_NAME);
        }
    }

    static Settings settings(Map<String, String> props, RedisOffsetBackingStoreConfig redis) {
        Settings s = new Settings()
                .key(redis.getRedisKeyName())
                .expectOffset(bool(props, EXPECT_OFFSET, false))
                .maxFailures(positive(props, MAX_FAILURES, 10))
                .attempts(positive(props, ATTEMPTS, 3))
                .retryDelays(redis.getInitialRetryDelay(), redis.getMaxRetryDelay())
                .waitForReplica(redis.isWaitEnabled(), redis.getWaitTimeout());
        if (PostgresSlotCheck.applies(props)) {
            s.slotCheck(new PostgresSlotCheck(props));
        }
        return s;
    }

    @Override
    public synchronized void start() {
        super.start();
        LOGGER.info("ECDL offset store: key '{}', expect a committed offset: {}, exit after {} failed "
                + "commit(s) in a row", settings.key, settings.expectOffset, settings.maxFailures);
        try {
            load();
            checkStartPosition();
        }
        catch (RuntimeException e) {
            stop();
            throw e;
        }
    }

    @Override
    public synchronized void stop() {
        connected = false;
        if (broker != null) {
            broker.close();
        }
        super.stop();
    }

    @Override
    public Set<Map<String, Object>> connectorPartitions(String connectorName) {
        return null;   // as upstream: Debezium Server never asks
    }

    /** Reads the stored offsets, retrying a broker that is not reachable yet. */
    void load() {
        byte[] key = settings.key.getBytes(StandardCharsets.UTF_8);
        long delay = settings.initialRetryMs;
        String lastError = null;
        for (int attempt = 1; attempt <= settings.maxFailures; attempt++) {
            try {
                ensureConnected();
                Map<byte[], byte[]> stored = broker.readAll(key);
                data = new HashMap<>();
                for (Map.Entry<byte[], byte[]> e : stored.entrySet()) {
                    data.put(ByteBuffer.wrap(e.getKey()), e.getValue() == null ? null : ByteBuffer.wrap(e.getValue()));
                }
                return;
            }
            catch (ConnectionException e) {
                dropConnection();
                lastError = "connection failed: " + e.getMessage();
            }
            catch (ReplyException e) {
                lastError = e.getMessage();
                if (!EcdlStreamWriter.isTransient(lastError)) {
                    break;   // NOAUTH, NOPERM, WRONGTYPE: waiting does not fix these
                }
            }
            LOGGER.error("ECDL offset store: could not read the offsets from '{}' ({}); attempt {}/{}",
                    settings.key, lastError, attempt, settings.maxFailures);
            if (attempt < settings.maxFailures) {
                pause(delay);
                delay = Math.min(settings.maxRetryMs, delay * 2);
            }
        }
        throw new DebeziumException("ECDL offset store: could not read the stored offsets from broker key '"
                + settings.key + "' (" + lastError + "). Not starting: without them Debezium cannot know "
                + "where to resume.");
    }

    /** Jira stories F005 / F008: refuse a start that would silently skip changes. */
    void checkStartPosition() {
        boolean present = false;
        for (ByteBuffer v : data.values()) {
            present |= v != null && v.remaining() > 0;
        }
        if (!present) {
            if (settings.expectOffset) {
                throw refuse("there is no committed offset in broker key '" + settings.key + "', but this link "
                        + "has committed one before (EXPECT_OFFSET=true). The broker was emptied or replaced "
                        + "(FLUSHALL, a new or restored broker) or the OffsetKey changed. Starting would begin "
                        + "at the source's CURRENT log position and silently skip every change since the last "
                        + "committed one.",
                        "If the old broker or the old key still holds the offset, point the link back at it. "
                                + "Otherwise reload the link with a position reset: `ecdl apply --reload "
                                + "<link file>` (control plane: 'Update and reload all tables'). That re-reads every "
                                + "table and starts capture at the current position with EXPECT_OFFSET=false; the "
                                + "deployer sets it back once a new offset is committed.");
            }
            LOGGER.warn("ECDL offset store: no committed offset in broker key '{}'. Expected on a link's first "
                    + "start only: Debezium starts as its snapshot.mode says, from the current log position. "
                    + "EXPECT_OFFSET is false, so this is not refused.", settings.key);
            return;
        }
        LOGGER.info("ECDL offset store: {} committed offset(s) loaded from '{}'", data.size(), settings.key);
        if (!settings.expectOffset || settings.slotCheck == null) {
            return;
        }
        SlotCheck check = settings.slotCheck;
        SlotResult r;
        try {
            r = check.check();
        }
        catch (RuntimeException e) {
            r = new SlotResult(SlotState.UNKNOWN, String.valueOf(e));
        }
        if (r.state == SlotState.PRESENT) {
            LOGGER.info("ECDL offset store: replication slot {} exists ({})", check.describe(), r.detail);
        }
        else if (r.state == SlotState.ABSENT) {
            throw refuse("replication slot " + check.describe() + " does not exist, but this link has a "
                    + "committed offset, so it has captured through that slot before. The slot was dropped, or "
                    + "the writer changed without it (blue/green switchover, major version upgrade, restore "
                    + "from a snapshot). Debezium would create a NEW slot at the current WAL position and "
                    + "silently skip every change since the last committed one.",
                    "Reload the link with a position reset: `ecdl apply --reload <link file>` (control plane: "
                            + "'Update and reload all tables'). See 'Switchovers, blue/green and major "
                            + "upgrades' in docs/operations.md.");
        }
        else {
            // Not refused: a check that cannot run must not stop capture on its own.
            LOGGER.error("ECDL offset store: could not check that replication slot {} exists ({}). Starting "
                    + "anyway. If the slot is missing, Debezium creates a new one at the current WAL position "
                    + "and changes since the last committed offset are skipped: check pg_replication_slots.",
                    check.describe(), r.detail);
        }
    }

    private DebeziumException refuse(String what, String recovery) {
        LOGGER.error("ECDL offset store: refusing to start: {}", what);
        LOGGER.error("FATAL ECDL offset store: not starting Debezium. To recover: {}", recovery);
        return new DebeziumException("ECDL offset store: refusing to start: " + what + " To recover: " + recovery);
    }

    /**
     * One commit: every partition's offset in one HSET. Tried settings.attempts
     * times; a commit that still fails throws, which fails the engine's flush.
     */
    @Override
    protected void save() {
        Map<byte[], byte[]> fields = new LinkedHashMap<>();
        for (Map.Entry<ByteBuffer, ByteBuffer> e : data.entrySet()) {
            if (e.getKey() != null && e.getValue() != null) {
                fields.put(bytes(e.getKey()), bytes(e.getValue()));
            }
        }
        if (fields.isEmpty()) {
            return;
        }
        byte[] key = settings.key.getBytes(StandardCharsets.UTF_8);
        long delay = settings.initialRetryMs;
        String lastError = null;
        for (int attempt = 1; attempt <= settings.attempts; attempt++) {
            try {
                ensureConnected();
                broker.writeAll(key, fields);
                if (settings.waitForReplica) {
                    long acked = broker.waitReplicas(1, settings.waitTimeoutMs);
                    if (acked < 1) {
                        // As upstream: the primary holds it; a failover now could
                        // lose this commit and replay a little from the previous one.
                        LOGGER.warn("ECDL offset store: no replica acknowledged the offset commit within {} ms",
                                settings.waitTimeoutMs);
                    }
                }
                if (failuresInARow > 0) {
                    LOGGER.info("ECDL offset store: offset committed again after {} failed commit(s)",
                            failuresInARow);
                }
                failuresInARow = 0;
                return;
            }
            catch (ConnectionException e) {
                dropConnection();
                lastError = "connection lost: " + e.getMessage();
            }
            catch (ReplyException e) {
                lastError = e.getMessage();
                if (EcdlStreamWriter.isFailover(lastError)) {
                    // This connection reaches a replica now: reconnect, so the
                    // endpoint's DNS can lead to the new primary.
                    dropConnection();
                }
                else if (!EcdlStreamWriter.isTransient(lastError)) {
                    break;   // NOPERM, WRONGTYPE, NOAUTH: retrying now does not help
                }
            }
            if (attempt < settings.attempts) {
                if (!pause(delay)) {
                    break;
                }
                delay = Math.min(settings.maxRetryMs, delay * 2);
            }
        }
        failed(lastError);
    }

    private void failed(String reason) {
        failuresInARow++;
        LOGGER.error("ECDL offset store: offset commit to '{}' failed ({}); {} failed commit(s) in a row, the "
                + "process exits at {}. Capture continues, but until a commit succeeds a restart resumes from "
                + "the last committed position and writes those changes again.",
                settings.key, reason, failuresInARow, settings.maxFailures);
        if (failuresInARow >= settings.maxFailures && !exiting) {
            exiting = true;
            LOGGER.error("FATAL ECDL offset store: {} offset commits in a row failed (last: {}). Exiting with "
                    + "status {} so ECS restarts the task with a fresh connection, instead of capturing on with "
                    + "an offset that no longer moves. Nothing is lost: the restart resumes from the last "
                    + "committed position.", failuresInARow, reason, EXIT_CODE);
            exit.exit(EXIT_CODE);
        }
        throw new ConnectException("ECDL offset store: offset commit failed: " + reason);
    }

    private void ensureConnected() {
        if (!connected) {
            broker.connect();
            connected = true;
        }
    }

    private void dropConnection() {
        connected = false;
        broker.close();
    }

    /** False when interrupted (the engine is stopping). */
    private boolean pause(long ms) {
        try {
            sleeper.sleep(ms);
            return true;
        }
        catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return false;
        }
    }

    int failuresInARow() {
        return failuresInARow;
    }

    Map<ByteBuffer, ByteBuffer> data() {
        return data;
    }

    private static byte[] bytes(ByteBuffer b) {
        ByteBuffer d = b.duplicate();
        byte[] out = new byte[d.remaining()];
        d.get(out);
        return out;
    }

    static boolean bool(Map<String, String> props, String name, boolean dflt) {
        String v = props.get(name);
        if (v == null || v.trim().isEmpty()) {
            return dflt;
        }
        switch (v.trim().toLowerCase(Locale.ROOT)) {
            case "true":
                return true;
            case "false":
                return false;
            default:
                // A typo must not quietly switch a safety check off.
                throw new DebeziumException("ECDL offset store: " + name + "='" + v + "' is not true or false");
        }
    }

    static int positive(Map<String, String> props, String name, int dflt) {
        String v = props.get(name);
        if (v == null || v.trim().isEmpty()) {
            return dflt;
        }
        try {
            int n = Integer.parseInt(v.trim());
            if (n >= 1) {
                return n;
            }
        }
        catch (NumberFormatException ignored) {
            // reported below
        }
        throw new DebeziumException("ECDL offset store: " + name + "='" + v + "' must be a whole number of 1 or more");
    }

    /**
     * System.exit runs the shutdown hooks (Debezium stops the engine, which
     * commits once more and stops this store), so it is called on its own
     * thread: the offset executor thread, which is calling this, has to stay
     * free for that last commit. If the shutdown hangs, the process is halted.
     */
    static void exitProcess(int status) {
        Thread halt = new Thread(() -> {
            try {
                Thread.sleep(HALT_AFTER_MS);
            }
            catch (InterruptedException ignored) {
                // halting anyway
            }
            Runtime.getRuntime().halt(status);
        }, "ecdl-offset-halt");
        halt.setDaemon(true);
        halt.start();
        new Thread(() -> System.exit(status), "ecdl-offset-exit").start();
    }
}
