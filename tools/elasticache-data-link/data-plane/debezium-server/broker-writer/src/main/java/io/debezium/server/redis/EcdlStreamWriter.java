/*
 * ECDL broker writer: how Debezium Server writes change events to the broker.
 *
 * Replaces the delivery logic of Debezium 3.0.6's Redis sink, which had three
 * ways to lose or reorder changes (design review 1-capture C1/C2, 2-broker C1/C2):
 *
 *   1. It treated every reply except "OOM ..." as success. READONLY (after a
 *      failover), NOPERM or WRONGTYPE replies marked the record processed and
 *      its offset was committed: the change was lost with no log line.
 *   2. When the broker refused some records of a batch (OOM), it retried only
 *      those, later, after records behind them had already been written. A row's
 *      changes then reached the stream out of order, and the sink's version
 *      guard (stream id order) kept the older value for good.
 *   3. Offsets were committed once the primary answered. A failover could lose
 *      entries the primary had not replicated, and after reconnecting Debezium
 *      carried on from its in-memory position, so the committed offset ended up
 *      ahead of what the broker held.
 *
 * This writer:
 *   - checks every reply. At the first refused write, everything before it is
 *     kept and everything from it onward is written again, in order. So the
 *     newest change for every row is always the last one in the stream: nothing
 *     is reordered or skipped, at worst a few entries appear twice;
 *   - retries a refusal that clears by itself (full, loading, busy) for as long
 *     as it lasts, logging ERROR once a minute, and stops the task on anything
 *     else (permissions, wrong type);
 *   - marks records processed, so their offset can be committed, only after a
 *     replica acknowledges them (WAIT 1), when the broker has replicas;
 *   - after a lost connection carries on only if the reconnect reaches the same
 *     primary (same run_id). A different primary, or a replica, means a failover:
 *     it stops the task so ECS restarts it from the replicated offset.
 *
 * Stopping is safe: nothing past the last replicated write was marked processed,
 * so the restart re-reads from there.
 */
package io.debezium.server.redis;

import java.util.AbstractMap.SimpleEntry;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.function.LongSupplier;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import io.debezium.DebeziumException;

public class EcdlStreamWriter {

    private static final Logger LOGGER = LoggerFactory.getLogger(EcdlStreamWriter.class);

    /** One stream entry: the stream name and its field/value pairs. */
    public static final class Entry extends SimpleEntry<String, Map<String, String>> {
        private static final long serialVersionUID = 1L;

        public Entry(String stream, Map<String, String> fields) {
            super(stream, fields);
        }
    }

    /** The broker, as this writer needs it. JedisBroker is the real one. */
    public interface Broker {
        /** Connect (or reconnect); returns the server's run_id and whether it is a primary. */
        ServerIdentity connect();

        /**
         * XADD every entry, pipelined, in order. Returns one reply per entry: the new
         * stream id, or the error the server answered for that command.
         *
         * @throws ConnectionException when the connection failed (which entries were
         *                             written is then unknown)
         */
        List<Object> addAll(List<Entry> entries);

        /** WAIT numReplicas timeoutMs: how many replicas acknowledged every write so far. */
        long waitReplicas(int numReplicas, long timeoutMs);

        void close();
    }

    public static final class ServerIdentity {
        final String runId;
        final boolean primary;

        public ServerIdentity(String runId, boolean primary) {
            this.runId = runId;
            this.primary = primary;
        }
    }

    public static class ConnectionException extends RuntimeException {
        private static final long serialVersionUID = 1L;

        public ConnectionException(String message, Throwable cause) {
            super(message, cause);
        }
    }

    /** A refusal of the connection itself (NOAUTH, WRONGPASS): not retried. */
    public static class ReplyException extends RuntimeException {
        private static final long serialVersionUID = 1L;

        public ReplyException(String message) {
            super(message);
        }
    }

    /** What the engine is told; RecordCommitter in production, a recorder in tests. */
    public interface Committer<R> {
        void markProcessed(R record) throws InterruptedException;
    }

    /** Pauses between attempts; Thread.sleep in production, recorded in tests. */
    public interface Sleeper {
        void sleep(long millis) throws InterruptedException;
    }

    public static final class Settings {
        boolean waitForReplica = true;
        long waitTimeoutMs = 1000;
        int waitAttempts = 30;
        long initialRetryMs = 300;
        long maxRetryMs = 10_000;
        long errorLogIntervalMs = 60_000;

        public Settings waitForReplica(boolean v) {
            waitForReplica = v;
            return this;
        }

        public Settings waitTimeoutMs(long v) {
            waitTimeoutMs = v;
            return this;
        }

        public Settings waitAttempts(int v) {
            waitAttempts = v;
            return this;
        }

        public Settings retryDelays(long initial, long max) {
            initialRetryMs = initial;
            maxRetryMs = max;
            return this;
        }

        public Settings errorLogIntervalMs(long v) {
            errorLogIntervalMs = v;
            return this;
        }
    }

    private final Broker broker;
    private final Settings settings;
    private final Sleeper sleeper;
    private final LongSupplier clock;
    private ServerIdentity identity;
    private boolean connected;

    public EcdlStreamWriter(Broker broker, Settings settings, Sleeper sleeper, LongSupplier clock) {
        this.broker = broker;
        this.settings = settings;
        this.sleeper = sleeper;
        this.clock = clock;
    }

    /** Opens the first connection. Refuses to start on a replica. */
    public void start() {
        identity = broker.connect();
        if (!identity.primary) {
            throw new DebeziumException("ECDL broker writer: the broker endpoint answers as a replica, "
                    + "not a primary. Use the replication group's primary endpoint.");
        }
        connected = true;
        LOGGER.info("ECDL broker writer: connected to primary run_id={} (wait for replica: {})",
                identity.runId, settings.waitForReplica);
    }

    /**
     * Writes one batch in order and marks each record processed only once it is
     * durable. Returns when every record is marked; throws (stopping the task)
     * when continuing could lose or reorder a change.
     */
    public <R> void write(List<R> records, List<Entry> entries, Committer<R> committer)
            throws InterruptedException {
        if (records.size() != entries.size()) {
            throw new IllegalArgumentException("records and entries differ in length");
        }
        int done = 0;                      // records[0..done) are marked processed
        long delay = settings.initialRetryMs;
        long stalledSince = -1;
        long lastLog = -1;
        int attempts = 0;
        while (done < entries.size()) {
            attempts++;
            String retryReason;
            try {
                ensureConnected();
                List<Entry> rest = entries.subList(done, entries.size());
                List<Object> replies = broker.addAll(rest);
                if (replies == null || replies.size() != rest.size()) {
                    throw new DebeziumException("ECDL broker writer: expected " + rest.size() + " replies, got "
                            + (replies == null ? "none" : replies.size()) + "; stopping rather than guessing "
                            + "which entries were written.");
                }
                int ok = 0;
                while (ok < replies.size() && isStreamId(replies.get(ok))) {
                    ok++;
                }
                if (ok > 0) {
                    // Everything before the first refusal is in the stream, in order.
                    awaitReplica(ok);
                    for (int i = done; i < done + ok; i++) {
                        committer.markProcessed(records.get(i));
                    }
                    done += ok;
                }
                if (ok == replies.size()) {
                    break;
                }
                // Entries after the refused one may have been written too; they are
                // written again after it, so each row's newest change stays last.
                String reply = text(replies.get(ok));
                if (isFailover(reply)) {
                    throw stopAfterFailover("the broker answered " + firstWord(reply));
                }
                if (!isTransient(reply)) {
                    throw new DebeziumException("ECDL broker writer: the broker refused a write (" + reply
                            + "). Writes after it were not marked processed; the task stops so the offset "
                            + "does not move past it.");
                }
                retryReason = reply;
            }
            catch (ConnectionException e) {
                connected = false;
                retryReason = "connection lost: " + e.getMessage();
            }
            long now = clock.getAsLong();
            if (stalledSince < 0) {
                stalledSince = now;
            }
            if (lastLog < 0 || now - lastLog >= settings.errorLogIntervalMs) {
                lastLog = now;
                // ERROR, so the Debezium-errors metric filter counts a stall that lasts.
                LOGGER.error("ECDL broker writer: {} of {} change(s) not written yet ({}); attempt {}, stalled "
                        + "{} ms, retrying. Nothing after them is marked processed until they are.",
                        entries.size() - done, entries.size(), retryReason, attempts, now - stalledSince);
            }
            sleeper.sleep(delay);
            delay = Math.min(settings.maxRetryMs, delay * 2);
        }
        if (stalledSince >= 0) {
            LOGGER.info("ECDL broker writer: batch of {} written after {} attempt(s), {} ms stalled",
                    entries.size(), attempts, clock.getAsLong() - stalledSince);
        }
    }

    public void close() {
        connected = false;
        broker.close();
    }

    private void ensureConnected() {
        if (connected) {
            return;
        }
        ServerIdentity now = broker.connect();
        if (!now.primary) {
            throw stopAfterFailover("the reconnect reached a replica");
        }
        if (identity != null && !identity.runId.equals(now.runId)) {
            throw stopAfterFailover("the reconnect reached a different primary (run_id "
                    + identity.runId + " -> " + now.runId + ")");
        }
        identity = now;
        connected = true;
        LOGGER.info("ECDL broker writer: reconnected to the same primary run_id={}", now.runId);
    }

    private void awaitReplica(int count) throws InterruptedException {
        if (!settings.waitForReplica) {
            return;
        }
        for (int i = 1; i <= settings.waitAttempts; i++) {
            long acked;
            try {
                acked = broker.waitReplicas(1, settings.waitTimeoutMs);
            }
            catch (ConnectionException e) {
                // Whether the replica holds them is unknown; a reconnect decides.
                connected = false;
                ensureConnected();
                continue;
            }
            if (acked >= 1) {
                return;
            }
            LOGGER.warn("ECDL broker writer: no replica acknowledged {} change(s) within {} ms (attempt {}/{})",
                    count, settings.waitTimeoutMs, i, settings.waitAttempts);
        }
        throw new DebeziumException("ECDL broker writer: no replica acknowledged the writes after "
                + settings.waitAttempts + " attempts. The offset is not committed past entries a failover "
                + "could lose, so the task stops. If the broker has no replica, set BROKER_WAIT=false.");
    }

    private DebeziumException stopAfterFailover(String what) {
        connected = false;
        return new DebeziumException("ECDL broker writer: " + what + ", so the broker failed over. Entries "
                + "acknowledged by the old primary may not have been replicated, and Debezium's in-memory "
                + "position may be ahead of what the broker holds. Stopping so the task restarts from the "
                + "replicated offset; changes after it are written again, none are skipped.");
    }

    static boolean isStreamId(Object reply) {
        if (reply == null || reply instanceof Throwable) {
            return false;
        }
        String s = reply.toString();
        int dash = s.indexOf('-');
        if (dash <= 0 || dash == s.length() - 1) {
            return false;
        }
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (i != dash && (c < '0' || c > '9')) {
                return false;
            }
        }
        return true;
    }

    static String text(Object reply) {
        if (reply instanceof Throwable) {
            String m = ((Throwable) reply).getMessage();
            return m == null ? reply.getClass().getSimpleName() : m;
        }
        return reply == null ? "(no reply)" : reply.toString();
    }

    /** Refusals that clear by themselves, so waiting and writing again is right. */
    static boolean isTransient(String reply) {
        String w = firstWord(reply).toUpperCase(Locale.ROOT);
        return w.equals("OOM") || w.equals("LOADING") || w.equals("BUSY") || w.equals("TRYAGAIN")
                || w.equals("MASTERDOWN") || w.equals("CLUSTERDOWN");
    }

    /** Replies that mean the connection reaches something other than the primary it had. */
    static boolean isFailover(String reply) {
        return firstWord(reply).toUpperCase(Locale.ROOT).equals("READONLY");
    }

    static String firstWord(String s) {
        String t = s == null ? "" : s.trim();
        int sp = t.indexOf(' ');
        return sp < 0 ? t : t.substring(0, sp);
    }
}
