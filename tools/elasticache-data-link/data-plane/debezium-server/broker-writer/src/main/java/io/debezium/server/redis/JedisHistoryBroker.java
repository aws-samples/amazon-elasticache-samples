/*
 * EcdlSchemaHistory.HistoryBroker over Jedis 4.1.1 (the version Debezium 3.0.6 ships).
 */
package io.debezium.server.redis;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import io.debezium.server.redis.EcdlStreamWriter.ConnectionException;
import io.debezium.server.redis.EcdlStreamWriter.ReplyException;
import redis.clients.jedis.DefaultJedisClientConfig;
import redis.clients.jedis.HostAndPort;
import redis.clients.jedis.Jedis;
import redis.clients.jedis.StreamEntryID;
import redis.clients.jedis.exceptions.JedisConnectionException;
import redis.clients.jedis.exceptions.JedisDataException;
import redis.clients.jedis.resps.StreamEntry;

public class JedisHistoryBroker implements EcdlSchemaHistory.HistoryBroker {

    private final HostAndPort address;
    private final DefaultJedisClientConfig clientConfig;
    final boolean verifyHostname;
    private Jedis jedis;

    public JedisHistoryBroker(String address, int dbIndex, String user, String password, int connectTimeoutMs,
                              int socketTimeoutMs, boolean ssl, boolean verifyHostname, String clientName) {
        this.address = HostAndPort.from(address);
        this.clientConfig = JedisBroker.clientConfig(dbIndex, user, password, connectTimeoutMs, socketTimeoutMs, ssl,
                verifyHostname, clientName);
        this.verifyHostname = verifyHostname;
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
    public String add(String key, Map<String, String> fields) {
        return call(() -> connected().xadd(key, StreamEntryID.NEW_ENTRY, fields).toString());
    }

    @Override
    public long waitReplicas(int numReplicas, long timeoutMs) {
        return call(() -> connected().waitReplicas(numReplicas, timeoutMs));
    }

    @Override
    public long length(String key) {
        return call(() -> connected().xlen(key));
    }

    @Override
    public List<Map.Entry<String, Map<String, String>>> range(String key, String afterId, int count) {
        // Exclusive start by the next possible id, which works on every server
        // version (the "(" prefix needs Redis 6.2).
        String start = afterId == null ? "-" : next(afterId);
        List<StreamEntry> entries = call(() -> connected().xrange(key, start, "+", count));
        List<Map.Entry<String, Map<String, String>>> out = new ArrayList<>(entries.size());
        for (StreamEntry e : entries) {
            out.add(EcdlSchemaHistory.entry(e.getID().toString(), e.getFields()));
        }
        return out;
    }

    @Override
    public void delete(String key, String id) {
        call(() -> connected().xdel(key, new StreamEntryID(id)));
    }

    static String next(String id) {
        StreamEntryID e = new StreamEntryID(id);
        return new StreamEntryID(e.getTime(), e.getSequence() + 1).toString();
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

    /** Jedis exceptions as the history's two kinds: the connection failed, or the server said no. */
    private <T> T call(Call<T> c) {
        try {
            return c.run();
        }
        catch (JedisConnectionException e) {
            close();
            throw new ConnectionException(e.getMessage(), e);
        }
        catch (JedisDataException e) {
            throw new ReplyException(e.getMessage());
        }
    }

    @Override
    public String toString() {
        return "JedisHistoryBroker[" + address + "]";
    }
}
