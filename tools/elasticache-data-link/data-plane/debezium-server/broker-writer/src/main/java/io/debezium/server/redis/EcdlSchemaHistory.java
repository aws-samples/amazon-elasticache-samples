/*
 * ECDL schema history: where MySQL (and SQL Server, Oracle) keep the DDL that
 * tells Debezium which columns a row event has, in the broker.
 *
 * Replaces io.debezium.storage.redis.history.RedisSchemaHistory (3.0.6),
 * selected by debezium.source.schema.history.internal in application.properties.
 * The stored data is exactly upstream's -- one stream, one entry per DDL record,
 * the record's JSON in the field "schema" -- so a link started with the upstream
 * class keeps its history.
 *
 * What upstream did wrong (review findings Jira stories F009, F010, F017):
 *
 *   1. No replica WAIT unless wait.enabled was set, and then a failed WAIT
 *      wrote the record AGAIN on every retry, indefinitely. Without the WAIT a DDL
 *      record the primary acknowledged but had not replicated was lost in a
 *      failover, and the next start decoded rows with the old columns.
 *   2. Any refusal (READONLY after a failover, OOM) was retried on the same
 *      connection, about a minute in all, logging WARN.
 *   3. The connection did not check the certificate's name; upstream's Redis
 *      connection has no setting for it.
 *   4. A record that could not be parsed ended the read early, without an
 *      error, so the schema was rebuilt from part of its history.
 *   5. The whole stream was read with one XRANGE, and nothing said how big it
 *      was, although every start reads all of it before capture resumes.
 *
 * This class:
 *   - writes each record once, then waits for one replica to hold it. A WAIT
 *     that times out is repeated (the record is not written again). If no
 *     replica acknowledges it within the attempts, the record is removed again
 *     (best effort) and the write fails, which stops the connector: ECS restarts
 *     the task, which reads the DDL from the source log again and records it
 *     then. Nothing is lost: the offset never moved past the DDL.
 *   - reconnects after a dropped connection or READONLY and writes the record
 *     again, because the new connection may reach a new primary that does not
 *     hold it. A duplicate DDL record is harmless; a missing one is not.
 *   - verifies the broker certificate's name (TLS), as the sink does.
 *   - fails the start on a record it cannot parse, naming it.
 *   - reads the stream in pages and logs how many records it holds, with a
 *     WARN above SCHEMA_HISTORY_WARN_RECORDS.
 */
package io.debezium.server.redis;

import java.io.IOException;
import java.util.AbstractMap;
import java.util.Collections;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.function.Consumer;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import io.debezium.config.Configuration;
import io.debezium.document.DocumentReader;
import io.debezium.document.DocumentWriter;
import io.debezium.relational.history.AbstractSchemaHistory;
import io.debezium.relational.history.HistoryRecord;
import io.debezium.relational.history.HistoryRecordComparator;
import io.debezium.relational.history.SchemaHistoryException;
import io.debezium.relational.history.SchemaHistoryListener;
import io.debezium.server.redis.EcdlStreamWriter.ConnectionException;
import io.debezium.server.redis.EcdlStreamWriter.ReplyException;
import io.debezium.server.redis.EcdlStreamWriter.Sleeper;
import io.debezium.storage.redis.history.RedisSchemaHistoryConfig;

public class EcdlSchemaHistory extends AbstractSchemaHistory {

    private static final Logger LOGGER = LoggerFactory.getLogger(EcdlSchemaHistory.class);

    /** Debezium passes this class its settings with the prefix kept. */
    static final String PREFIX = "schema.history.internal.";
    static final String HOSTNAME_VERIFICATION = PREFIX + "redis.ssl.hostname.verification.enabled";
    static final String WAIT_ATTEMPTS = PREFIX + "ecdl.wait.attempts";
    static final String WARN_RECORDS = PREFIX + "ecdl.warn.records";
    static final String CLIENT_NAME = "debezium:schema_history";
    /** The stream entry field upstream wrote the record into. */
    static final String FIELD = "schema";
    static final int PAGE = 1000;

    /** The broker, as this class needs it. JedisHistoryBroker is the real one. */
    public interface HistoryBroker {
        /** Connect (or reconnect). Throws ConnectionException, or ReplyException (NOAUTH ...). */
        void connect();

        /** XADD key * fields; returns the entry id. ReplyException carries the server's error. */
        String add(String key, Map<String, String> fields);

        /** WAIT numReplicas timeoutMs. */
        long waitReplicas(int numReplicas, long timeoutMs);

        /** XLEN. */
        long length(String key);

        /** Up to count entries after afterId (null: from the first), oldest first, as (id, fields). */
        List<Map.Entry<String, Map<String, String>>> range(String key, String afterId, int count);

        /** XDEL of one entry. */
        void delete(String key, String id);

        void close();
    }

    public static final class Settings {
        String key = "metadata:debezium:schema_history";
        int attempts = 10;
        long initialRetryMs = 300;
        long maxRetryMs = 10_000;
        boolean waitForReplica;
        long waitTimeoutMs = 1000;
        int waitAttempts = 30;
        long warnRecords = 10_000;

        public Settings key(String v) {
            key = v;
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

        public Settings waitForReplica(boolean v, long timeoutMs, int attemptsAtMost) {
            waitForReplica = v;
            waitTimeoutMs = timeoutMs;
            waitAttempts = attemptsAtMost;
            return this;
        }

        public Settings warnRecords(long v) {
            warnRecords = v;
            return this;
        }
    }

    private final DocumentWriter writer = DocumentWriter.defaultWriter();
    private final DocumentReader reader = DocumentReader.defaultReader();
    private HistoryBroker broker;
    private Settings settings;
    private final Sleeper sleeper;
    private boolean connected;

    /** Used by the connector, which instantiates the class named in schema.history.internal. */
    public EcdlSchemaHistory() {
        this(null, null, Thread::sleep);
    }

    /** For tests: a broker and settings are given; configure() keeps them. */
    EcdlSchemaHistory(HistoryBroker broker, Settings settings, Sleeper sleeper) {
        this.broker = broker;
        this.settings = settings;
        this.sleeper = sleeper;
    }

    @Override
    public void configure(Configuration config, HistoryRecordComparator comparator, SchemaHistoryListener listener,
                          boolean useCatalogBeforeSchema) {
        if (settings == null) {
            settings = settings(config, new RedisSchemaHistoryConfig(config));
        }
        if (broker == null) {
            RedisSchemaHistoryConfig redis = new RedisSchemaHistoryConfig(config);
            broker = new JedisHistoryBroker(redis.getAddress(), redis.getDbIndex(), redis.getUser(),
                    redis.getPassword(), redis.getConnectionTimeout(), redis.getSocketTimeout(),
                    redis.isSslEnabled(), bool(config, HOSTNAME_VERIFICATION, true), CLIENT_NAME);
        }
        super.configure(config, comparator, listener, useCatalogBeforeSchema);
    }

    static Settings settings(Configuration config, RedisSchemaHistoryConfig redis) {
        return new Settings()
                .key(redis.getRedisKeyName())
                .attempts(Math.max(1, redis.getMaxRetryCount()))
                .retryDelays(redis.getInitialRetryDelay(), redis.getMaxRetryDelay())
                .waitForReplica(redis.isWaitEnabled(), redis.getWaitTimeout(), positive(config, WAIT_ATTEMPTS, 30))
                .warnRecords(positive(config, WARN_RECORDS, 10_000));
    }

    @Override
    public synchronized void start() {
        super.start();
        LOGGER.info("ECDL schema history: key '{}', wait for a replica: {}", settings.key, settings.waitForReplica);
        withRetry("connect to the broker", () -> null);
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
    public boolean storageExists() {
        return true;   // as upstream: a stream needs no creating
    }

    @Override
    public synchronized boolean exists() {
        return withRetry("check for stored history", () -> broker.length(settings.key)) > 0;
    }

    /**
     * One DDL record: written once, then held until a replica acknowledges it.
     * Throws SchemaHistoryException, which stops the connector, when that
     * cannot be done.
     */
    @Override
    protected synchronized void storeRecord(HistoryRecord record) throws SchemaHistoryException {
        if (record == null) {
            return;
        }
        String json;
        try {
            json = writer.write(record.document());
        }
        catch (IOException e) {
            throw new SchemaHistoryException("ECDL schema history: could not serialise a history record", e);
        }
        Map<String, String> fields = Collections.singletonMap(FIELD, json);
        long delay = settings.initialRetryMs;
        String lastError = null;
        String written = null;   // the entry id, once this connection holds it
        for (int attempt = 1; attempt <= settings.attempts; attempt++) {
            try {
                ensureConnected();
                if (written == null) {
                    written = broker.add(settings.key, fields);
                }
                lastError = awaitReplica();
                if (lastError == null) {
                    if (attempt > 1) {
                        LOGGER.info("ECDL schema history: record written after {} attempt(s)", attempt);
                    }
                    return;
                }
                // No replica acknowledged it: the write stops here (no retry, as
                // the WAIT has been repeated already). See below.
                break;
            }
            catch (ConnectionException e) {
                // Whether the record reached a replica is unknown, and the next
                // connection may reach a new primary: write it again.
                dropConnection();
                written = null;
                lastError = "connection lost: " + e.getMessage();
            }
            catch (ReplyException e) {
                lastError = e.getMessage();
                if (EcdlStreamWriter.isFailover(lastError)) {
                    dropConnection();
                    written = null;
                }
                else if (!EcdlStreamWriter.isTransient(lastError)) {
                    break;   // NOPERM, WRONGTYPE, NOAUTH: retrying does not help
                }
            }
            LOGGER.error("ECDL schema history: could not write a record to '{}' ({}); attempt {}/{}",
                    settings.key, lastError, attempt, settings.attempts);
            if (attempt < settings.attempts && !pause(delay)) {
                break;
            }
            delay = Math.min(settings.maxRetryMs, delay * 2);
        }
        if (written != null) {
            // Only the primary holds it. Removed, so the restart's own copy of
            // this DDL is the only one.
            try {
                broker.delete(settings.key, written);
            }
            catch (RuntimeException e) {
                LOGGER.warn("ECDL schema history: could not remove unreplicated entry {} ({}); the DDL may be "
                        + "recorded twice, which is harmless", written, e.getMessage());
            }
        }
        LOGGER.error("FATAL ECDL schema history: a DDL record could not be stored durably in '{}' ({}). Stopping "
                + "the connector: a schema change missing from the history makes a later start decode rows "
                + "with the wrong columns. The restart reads the DDL from the source log again.",
                settings.key, lastError);
        // No cause attached: a wrapped I/O error would make Debezium retry in
        // place instead of stopping.
        throw new SchemaHistoryException("ECDL schema history: could not store a DDL record durably in '"
                + settings.key + "' (" + lastError + ")");
    }

    /** Null once a replica holds the writes so far; otherwise why not. */
    private String awaitReplica() {
        if (!settings.waitForReplica) {
            return null;
        }
        for (int i = 1; i <= settings.waitAttempts; i++) {
            if (broker.waitReplicas(1, settings.waitTimeoutMs) >= 1) {
                return null;
            }
            LOGGER.warn("ECDL schema history: no replica acknowledged the record within {} ms (attempt {}/{})",
                    settings.waitTimeoutMs, i, settings.waitAttempts);
        }
        return "no replica acknowledged it after " + settings.waitAttempts + " WAIT(s) of "
                + settings.waitTimeoutMs + " ms; with a single-node broker set BROKER_WAIT=false";
    }

    @Override
    protected synchronized void recoverRecords(Consumer<HistoryRecord> records) {
        long started = System.currentTimeMillis();
        long length = withRetry("read the history length", () -> broker.length(settings.key));
        if (length >= settings.warnRecords) {
            LOGGER.warn("ECDL schema history: '{}' holds {} records (warning at {}). Every start reads all of "
                    + "them before capture resumes. See 'Schema history size' in docs/operations.md.",
                    settings.key, length, settings.warnRecords);
        }
        else {
            LOGGER.info("ECDL schema history: reading {} record(s) from '{}'", length, settings.key);
        }
        String after = null;
        long read = 0;
        while (true) {
            final String from = after;
            List<Map.Entry<String, Map<String, String>>> page = withRetry("read the history",
                    () -> broker.range(settings.key, from, PAGE));
            for (Map.Entry<String, Map<String, String>> entry : page) {
                String json = entry.getValue() == null ? null : entry.getValue().get(FIELD);
                if (json == null) {
                    throw unreadable(entry.getKey(), "it has no '" + FIELD + "' field");
                }
                HistoryRecord record;
                try {
                    record = new HistoryRecord(reader.read(json));
                }
                catch (IOException | RuntimeException e) {
                    throw unreadable(entry.getKey(), e.getMessage());
                }
                records.accept(record);
                read++;
                after = entry.getKey();
            }
            if (page.size() < PAGE) {
                break;
            }
        }
        LOGGER.info("ECDL schema history: {} record(s) read from '{}' in {} ms", read, settings.key,
                System.currentTimeMillis() - started);
    }

    private SchemaHistoryException unreadable(String id, String why) {
        String m = "ECDL schema history: entry " + id + " of '" + settings.key + "' cannot be read (" + why
                + "). Not starting: rebuilding the schema from part of its history decodes rows with the "
                + "wrong columns. Remove or repair that entry, or reload the link with a position reset.";
        LOGGER.error(m);
        return new SchemaHistoryException(m);
    }

    private interface Call<T> {
        T run();
    }

    /** A read (or the first connect): retried with back-off; the last error is thrown. */
    private <T> T withRetry(String what, Call<T> call) {
        long delay = settings.initialRetryMs;
        String lastError = null;
        for (int attempt = 1; attempt <= settings.attempts; attempt++) {
            try {
                ensureConnected();
                return call.run();
            }
            catch (ConnectionException e) {
                dropConnection();
                lastError = "connection failed: " + e.getMessage();
            }
            catch (ReplyException e) {
                lastError = e.getMessage();
                if (!EcdlStreamWriter.isTransient(lastError) && !EcdlStreamWriter.isFailover(lastError)) {
                    break;   // NOAUTH, NOPERM, WRONGTYPE
                }
                dropConnection();
            }
            LOGGER.error("ECDL schema history: could not {} '{}' ({}); attempt {}/{}", what, settings.key,
                    lastError, attempt, settings.attempts);
            if (attempt < settings.attempts && !pause(delay)) {
                break;
            }
            delay = Math.min(settings.maxRetryMs, delay * 2);
        }
        throw new SchemaHistoryException("ECDL schema history: could not " + what + " '" + settings.key + "' ("
                + lastError + ")");
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

    /** False when interrupted (the connector is stopping). */
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

    Settings settings() {
        return settings;
    }

    HistoryBroker broker() {
        return broker;
    }

    static Map.Entry<String, Map<String, String>> entry(String id, Map<String, String> fields) {
        return new AbstractMap.SimpleImmutableEntry<>(id, fields);
    }

    static boolean bool(Configuration config, String name, boolean dflt) {
        String v = config.getString(name);
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
                throw new SchemaHistoryException("ECDL schema history: " + name + "='" + v + "' is not true or false");
        }
    }

    static int positive(Configuration config, String name, int dflt) {
        String v = config.getString(name);
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
        throw new SchemaHistoryException("ECDL schema history: " + name + "='" + v
                + "' must be a whole number of 1 or more");
    }
}
