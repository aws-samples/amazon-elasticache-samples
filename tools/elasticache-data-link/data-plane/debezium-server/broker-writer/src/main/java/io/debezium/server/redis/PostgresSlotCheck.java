/*
 * Does the PostgreSQL replication slot the connector reads still exist?
 *
 * Asked by EcdlOffsetBackingStore before Debezium starts, only when the link
 * has committed an offset before (EXPECT_OFFSET=true). A slot that is gone
 * then means the writer changed without it or it was dropped, and Debezium
 * would quietly create a new one at the current WAL position (review finding Jira story F008).
 *
 * Plain JDBC with the connector's own settings and credentials. The PostgreSQL
 * driver is in the Debezium image (the connector uses it), so nothing is added;
 * it is loaded by name, so this compiles without it. pg_replication_slots is
 * readable by any user.
 */
package io.debezium.server.redis;

import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.Map;
import java.util.Properties;

import io.debezium.server.redis.EcdlOffsetBackingStore.SlotResult;
import io.debezium.server.redis.EcdlOffsetBackingStore.SlotState;

public class PostgresSlotCheck implements EcdlOffsetBackingStore.SlotCheck {

    static final String DRIVER = "org.postgresql.Driver";
    static final String QUERY = "SELECT database, plugin FROM pg_replication_slots WHERE slot_name = ?";
    /** Debezium's default slot.name. */
    static final String DEFAULT_SLOT = "debezium";
    private static final int TIMEOUT_SECONDS = 10;
    // Connection settings passed through when the connector has them.
    private static final String[] SSL_SETTINGS = { "sslmode", "sslrootcert", "sslcert", "sslkey", "sslpassword" };

    private final String host;
    private final String port;
    private final String database;
    private final String slot;
    private final Properties connection = new Properties();

    PostgresSlotCheck(Map<String, String> props) {
        host = props.getOrDefault("database.hostname", "");
        port = props.getOrDefault("database.port", "5432");
        database = props.getOrDefault("database.dbname", "");
        slot = props.getOrDefault("slot.name", DEFAULT_SLOT);
        put("user", props.get("database.user"));
        put("password", props.get("database.password"));
        // Debezium's own default; the driver's would be the same.
        put("sslmode", props.getOrDefault("database.sslmode", "prefer"));
        for (String s : SSL_SETTINGS) {
            if (!s.equals("sslmode")) {
                put(s, props.get("database." + s));
            }
        }
        put("connectTimeout", String.valueOf(TIMEOUT_SECONDS));
        put("socketTimeout", String.valueOf(TIMEOUT_SECONDS));
        put("loginTimeout", String.valueOf(TIMEOUT_SECONDS));
        put("ApplicationName", "ecdl-slot-check");
    }

    /** Only the PostgreSQL connector reads a slot. */
    static boolean applies(Map<String, String> props) {
        return String.valueOf(props.get("connector.class")).endsWith(".PostgresConnector");
    }

    private void put(String name, String value) {
        if (value != null && !value.isEmpty()) {
            connection.setProperty(name, value);
        }
    }

    String url() {
        return "jdbc:postgresql://" + host + ":" + port + "/" + URLEncoder.encode(database, StandardCharsets.UTF_8);
    }

    @Override
    public String describe() {
        return "'" + slot + "' on " + host + ":" + port + "/" + database;
    }

    @Override
    public SlotResult check() {
        try {
            Class.forName(DRIVER);
        }
        catch (ClassNotFoundException e) {
            return new SlotResult(SlotState.UNKNOWN, "the PostgreSQL JDBC driver is not on the classpath");
        }
        try (Connection c = DriverManager.getConnection(url(), connection);
                PreparedStatement q = c.prepareStatement(QUERY)) {
            q.setQueryTimeout(TIMEOUT_SECONDS);
            q.setString(1, slot);
            try (ResultSet rs = q.executeQuery()) {
                if (!rs.next()) {
                    return new SlotResult(SlotState.ABSENT, "not in pg_replication_slots");
                }
                return new SlotResult(SlotState.PRESENT, "database " + rs.getString(1) + ", plugin " + rs.getString(2));
            }
        }
        catch (SQLException e) {
            // The password never reaches the message: the driver does not echo it.
            return new SlotResult(SlotState.UNKNOWN, "SQLState " + e.getSQLState() + ": " + e.getMessage());
        }
    }
}
