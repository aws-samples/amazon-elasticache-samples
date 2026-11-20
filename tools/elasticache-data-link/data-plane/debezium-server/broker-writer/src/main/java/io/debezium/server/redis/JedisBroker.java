/*
 * EcdlStreamWriter.Broker over Jedis 4.1.1 (the version Debezium 3.0.6 ships).
 */
package io.debezium.server.redis;

import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

import javax.net.ssl.SSLParameters;

import io.debezium.server.redis.EcdlStreamWriter.ConnectionException;
import io.debezium.server.redis.EcdlStreamWriter.Entry;
import io.debezium.server.redis.EcdlStreamWriter.ReplyException;
import io.debezium.server.redis.EcdlStreamWriter.ServerIdentity;

import redis.clients.jedis.DefaultJedisClientConfig;
import redis.clients.jedis.HostAndPort;
import redis.clients.jedis.Jedis;
import redis.clients.jedis.Pipeline;
import redis.clients.jedis.StreamEntryID;
import redis.clients.jedis.exceptions.JedisConnectionException;
import redis.clients.jedis.exceptions.JedisDataException;

public class JedisBroker implements EcdlStreamWriter.Broker {

    private final HostAndPort address;
    private final DefaultJedisClientConfig clientConfig;
    private final String clientName;
    private Jedis jedis;

    /**
     * @param verifyHostname check the broker certificate's name against the endpoint
     *                       (TLS only). ElastiCache certificates cover the primary
     *                       endpoint name.
     */
    public JedisBroker(String address, int dbIndex, String user, String password, int connectTimeoutMs,
                       int socketTimeoutMs, boolean ssl, boolean verifyHostname, String clientName) {
        this.address = HostAndPort.from(address);
        this.clientConfig = clientConfig(dbIndex, user, password, connectTimeoutMs, socketTimeoutMs, ssl,
                verifyHostname, clientName);
        this.clientName = clientName;
    }

    /** The connection settings, shared with JedisOffsetBroker so both connect the same way. */
    static DefaultJedisClientConfig clientConfig(int dbIndex, String user, String password, int connectTimeoutMs,
                                                 int socketTimeoutMs, boolean ssl, boolean verifyHostname,
                                                 String clientName) {
        DefaultJedisClientConfig.Builder b = DefaultJedisClientConfig.builder()
                .database(dbIndex)
                .connectionTimeoutMillis(connectTimeoutMs)
                .socketTimeoutMillis(socketTimeoutMs)
                .ssl(ssl)
                .clientName(clientName);
        if (ssl && verifyHostname) {
            SSLParameters params = new SSLParameters();
            params.setEndpointIdentificationAlgorithm("HTTPS");
            b = b.sslParameters(params);
        }
        if (user != null && !user.isEmpty()) {
            b = b.user(user);
        }
        if (password != null && !password.isEmpty()) {
            b = b.password(password);
        }
        return b.build();
    }

    @Override
    public ServerIdentity connect() {
        close();
        try {
            jedis = new Jedis(address, clientConfig);
            jedis.ping();
            String server = jedis.info("server");
            String replication = jedis.info("replication");
            return new ServerIdentity(field(server, "run_id"), "master".equals(field(replication, "role")));
        }
        catch (JedisConnectionException e) {
            close();
            throw new ConnectionException(e.getMessage(), e);
        }
        catch (JedisDataException e) {
            close();
            // NOAUTH / WRONGPASS / NOPERM on connect: a retry does not fix these.
            throw new ReplyException(e.getMessage());
        }
    }

    @Override
    public List<Object> addAll(List<Entry> entries) {
        if (jedis == null) {
            throw new ConnectionException("not connected", null);
        }
        try {
            Pipeline p = jedis.pipelined();
            for (Entry e : entries) {
                p.xadd(e.getKey(), StreamEntryID.NEW_ENTRY, e.getValue());
            }
            // One reply per command, in order; a refused command's reply is the
            // JedisDataException itself, not a thrown one.
            List<Object> raw = p.syncAndReturnAll();
            List<Object> out = new ArrayList<>(raw.size());
            for (Object o : raw) {
                out.add(o instanceof byte[] ? new String((byte[]) o, StandardCharsets.UTF_8) : o);
            }
            return out;
        }
        catch (JedisConnectionException e) {
            close();
            throw new ConnectionException(e.getMessage(), e);
        }
    }

    @Override
    public long waitReplicas(int numReplicas, long timeoutMs) {
        if (jedis == null) {
            throw new ConnectionException("not connected", null);
        }
        try {
            return jedis.waitReplicas(numReplicas, timeoutMs);
        }
        catch (JedisConnectionException e) {
            close();
            throw new ConnectionException(e.getMessage(), e);
        }
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

    static String field(String info, String name) {
        for (String line : info.split("\r?\n")) {
            if (line.startsWith(name + ":")) {
                return line.substring(name.length() + 1).trim();
            }
        }
        return "";
    }

    @Override
    public String toString() {
        return "JedisBroker[" + address + ", " + clientName + "]";
    }
}
