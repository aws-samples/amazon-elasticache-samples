/*
 * Copyright Debezium Authors.
 *
 * Licensed under the Apache Software License version 2.0, available at http://www.apache.org/licenses/LICENSE-2.0
 *
 * ECDL REPLACEMENT of io.debezium.server.redis.RedisStreamChangeConsumer from
 * debezium-server-redis 3.0.6.Final (sha256 ec5187d5...cc2d39). Same class name,
 * package, annotations and package-private lifecycle methods, so the bean that
 * Debezium Server generated at build time instantiates this one: it is placed
 * ahead of lib/ on the classpath (config/lib). Record mapping (extended/compact
 * message formats, headers) and the optional memory threshold are unchanged.
 * Delivery is EcdlStreamWriter's; see that class for what changed and why.
 *
 * The image build refuses to build if the upstream jar is not the exact one this
 * was written against (data-plane/debezium-server/Dockerfile).
 */
package io.debezium.server.redis;

import static io.debezium.server.redis.RedisStreamChangeConsumerConfig.MESSAGE_FORMAT_COMPACT;
import static io.debezium.server.redis.RedisStreamChangeConsumerConfig.MESSAGE_FORMAT_EXTENDED;

import java.time.Duration;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.function.Function;

import jakarta.annotation.PostConstruct;
import jakarta.annotation.PreDestroy;
import jakarta.enterprise.context.Dependent;
import jakarta.inject.Named;

import org.eclipse.microprofile.config.Config;
import org.eclipse.microprofile.config.ConfigProvider;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import io.debezium.config.Configuration;
import io.debezium.engine.ChangeEvent;
import io.debezium.engine.DebeziumEngine;
import io.debezium.engine.DebeziumEngine.RecordCommitter;
import io.debezium.server.BaseChangeConsumer;
import io.debezium.storage.redis.RedisClient;
import io.debezium.storage.redis.RedisConnection;
import io.debezium.util.DelayStrategy;

@Named("redis")
@Dependent
public class RedisStreamChangeConsumer extends BaseChangeConsumer
        implements DebeziumEngine.ChangeConsumer<ChangeEvent<Object, Object>> {

    private static final Logger LOGGER = LoggerFactory.getLogger(RedisStreamChangeConsumer.class);

    private static final String DEBEZIUM_REDIS_SINK_CLIENT_NAME = "debezium:redis:sink";
    private static final String MEMORY_CHECK_CLIENT_NAME = "debezium:redis:sink:memory";

    private static final String EXTENDED_MESSAGE_KEY_KEY = "key";
    private static final String EXTENDED_MESSAGE_VALUE_KEY = "value";

    private RedisClient client;              // memory checks only
    private Function<ChangeEvent<Object, Object>, Map<String, String>> recordMapFunction;
    private RedisMemoryThreshold redisMemoryThreshold;
    private RedisStreamChangeConsumerConfig config;
    private EcdlStreamWriter writer;

    @PostConstruct
    void connect() {
        Config mp = ConfigProvider.getConfig();
        Configuration configuration = Configuration.from(getConfigSubset(mp, ""));
        config = new RedisStreamChangeConsumerConfig(configuration);

        String messageFormat = config.getMessageFormat();
        if (MESSAGE_FORMAT_EXTENDED.equals(messageFormat)) {
            recordMapFunction = record -> {
                Map<String, String> recordMap = new LinkedHashMap<>();
                String key = (record.key() != null) ? getString(record.key()) : config.getNullKey();
                String value = (record.value() != null) ? getString(record.value()) : config.getNullValue();
                Map<String, String> headers = convertHeaders(record);

                recordMap.put(EXTENDED_MESSAGE_KEY_KEY, key);
                recordMap.put(EXTENDED_MESSAGE_VALUE_KEY, value);
                for (Map.Entry<String, String> entry : headers.entrySet()) {
                    recordMap.put(entry.getKey().toUpperCase(Locale.ROOT), entry.getValue());
                }
                return recordMap;
            };
        }
        else if (MESSAGE_FORMAT_COMPACT.equals(messageFormat)) {
            recordMapFunction = record -> {
                String key = (record.key() != null) ? getString(record.key()) : config.getNullKey();
                String value = (record.value() != null) ? getString(record.value()) : config.getNullValue();
                return Map.of(key, value);
            };
        }

        // Upstream had no hostname check for this connection; ElastiCache
        // certificates do cover the primary endpoint, so it is on by default.
        boolean verifyHostname = mp.getOptionalValue(
                "debezium.sink.redis.ssl.hostname.verification.enabled", Boolean.class).orElse(true);
        JedisBroker broker = new JedisBroker(config.getAddress(), config.getDbIndex(), config.getUser(),
                config.getPassword(), config.getConnectionTimeout(), config.getSocketTimeout(),
                config.isSslEnabled(), verifyHostname, DEBEZIUM_REDIS_SINK_CLIENT_NAME);
        EcdlStreamWriter.Settings settings = new EcdlStreamWriter.Settings()
                .waitForReplica(config.isWaitEnabled())
                .waitTimeoutMs(config.getWaitTimeout())
                .retryDelays(config.getInitialRetryDelay(), config.getMaxRetryDelay());
        writer = new EcdlStreamWriter(broker, settings, Thread::sleep, System::currentTimeMillis);
        writer.start();

        // The memory threshold reads INFO memory on its own plain connection, as
        // upstream did. Wait-for-replica belongs to the writes, not to INFO.
        RedisConnection memoryConnection = new RedisConnection(config.getAddress(), config.getDbIndex(),
                config.getUser(), config.getPassword(), config.getConnectionTimeout(), config.getSocketTimeout(),
                config.isSslEnabled());
        client = memoryConnection.getRedisClient(MEMORY_CHECK_CLIENT_NAME, false, 0, false, 0);
        redisMemoryThreshold = new RedisMemoryThreshold(client, config);
        LOGGER.info("ECDL broker writer in use (batch size {}, wait for replica {})",
                config.getBatchSize(), config.isWaitEnabled());
    }

    @PreDestroy
    void close() {
        try {
            if (writer != null) {
                writer.close();
            }
            if (client != null) {
                client.close();
            }
        }
        catch (Exception e) {
            LOGGER.warn("Exception while closing the broker connections", e);
        }
        finally {
            writer = null;
            client = null;
        }
    }

    @Override
    public void handleBatch(List<ChangeEvent<Object, Object>> records,
                            RecordCommitter<ChangeEvent<Object, Object>> committer)
            throws InterruptedException {
        DelayStrategy memoryWait = DelayStrategy.constant(Duration.ofMillis(config.getWaitRetryDelay()));
        int size = config.getBatchSize();
        for (int from = 0; from < records.size(); from += size) {
            List<ChangeEvent<Object, Object>> batch = records.subList(from, Math.min(records.size(), from + size));
            List<EcdlStreamWriter.Entry> entries = new ArrayList<>(batch.size());
            for (ChangeEvent<Object, Object> record : batch) {
                entries.add(new EcdlStreamWriter.Entry(streamNameMapper.map(record.destination()),
                        recordMapFunction.apply(record)));
            }
            long stalledSince = -1;
            long lastLog = -1;
            while (!redisMemoryThreshold.checkMemory(approximateSize(entries.get(0)), entries.size(),
                    config.getBufferFillRate())) {
                long now = System.currentTimeMillis();
                if (stalledSince < 0) {
                    stalledSince = now;
                }
                // ERROR once a minute, as the writer does, so the Debezium-errors
                // alarm sees a broker that stays full. Upstream logged INFO.
                if (lastLog < 0 || now - lastLog >= 60_000) {
                    lastLog = now;
                    LOGGER.error("ECDL broker writer: broker memory is full; {} change(s) waiting {} ms for the sink "
                            + "to drain it. Nothing is lost; capture is paused until there is room.",
                            entries.size(), now - stalledSince);
                }
                memoryWait.sleepWhen(true);
            }
            if (stalledSince >= 0) {
                LOGGER.info("ECDL broker writer: broker memory available again after {} ms",
                        System.currentTimeMillis() - stalledSince);
            }
            writer.write(batch, entries, committer::markProcessed);
        }
        committer.markBatchFinished();
    }

    private static long approximateSize(EcdlStreamWriter.Entry record) {
        long size = record.getKey().getBytes().length;
        for (Map.Entry<String, String> e : record.getValue().entrySet()) {
            size += e.getKey().getBytes().length + e.getValue().getBytes().length;
        }
        return size;
    }
}
