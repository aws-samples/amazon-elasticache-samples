/*
 * EcdlOffsetBackingStore.OffsetBroker over Jedis 4.1.1 (the version Debezium 3.0.6 ships).
 */
package io.debezium.server.redis;

import java.util.Map;

import io.debezium.server.redis.EcdlStreamWriter.ConnectionException;
import io.debezium.server.redis.EcdlStreamWriter.ReplyException;

import redis.clients.jedis.DefaultJedisClientConfig;
import redis.clients.jedis.HostAndPort;
import redis.clients.jedis.Jedis;
import redis.clients.jedis.exceptions.JedisConnectionException;
import redis.clients.jedis.exceptions.JedisDataException;

public class JedisOffsetBroker implements EcdlOffsetBackingStore.OffsetBroker {

    private final HostAndPort address;
    private final DefaultJedisClientConfig clientConfig;
    private Jedis jedis;

    public JedisOffsetBroker(String address, int dbIndex, String user, String password, int connectTimeoutMs,
                             int socketTimeoutMs, boolean ssl, boolean verifyHostname, String clientName) {
        this.address = HostAndPort.from(address);
        this.clientConfig = JedisBroker.clientConfig(dbIndex, user, password, connectTimeoutMs, socketTimeoutMs, ssl,
                verifyHostname, clientName);
    }

    @Override
    public void connect() {
        close();
        try {
            call(() -> {
                jedis = new Jedis(address, clientConfig);
                jedis.ping();
                return null;
            });
        }
        catch (RuntimeException e) {
            close();   // a refused AUTH leaves an open socket behind
            throw e;
        }
    }

    @Override
    public Map<byte[], byte[]> readAll(byte[] key) {
        return call(() -> connected().hgetAll(key));
    }

    @Override
    public void writeAll(byte[] key, Map<byte[], byte[]> fields) {
        call(() -> connected().hset(key, fields));
    }

    @Override
    public long waitReplicas(int numReplicas, long timeoutMs) {
        return call(() -> connected().waitReplicas(numReplicas, timeoutMs));
    }

    @Override
    public void close() {
        if (jedis != null) {
            try {
                jedis.close();
            }
            catch (RuntimeException ignored) {
                // already broken; a new connection replaces it
            }
            jedis = null;
        }
    }

    private Jedis connected() {
        if (jedis == null) {
            throw new ConnectionException("not connected", null);
        }
        return jedis;
    }

    private interface Call<T> {
        T run();
    }

    /** Jedis exceptions as the store's two kinds: the connection failed, or the server said no. */
    private <T> T call(Call<T> c) {
        try {
            return c.run();
        }
        catch (JedisConnectionException e) {
            close();
            throw new ConnectionException(e.getMessage(), e);
        }
        catch (JedisDataException e) {
            // READONLY, OOM, LOADING, NOAUTH, WRONGTYPE ...: the server's own words.
            throw new ReplyException(e.getMessage());
        }
    }

    @Override
    public String toString() {
        return "JedisOffsetBroker[" + address + "]";
    }
}
