/*
 * A small, regular write to an idle SQL Server source, so its change capture
 * always has a recent transaction to read up to.
 *
 * Found in the end-to-end test of SQL Server 2022 (CU28) on EC2: deployed on a
 * source with no writes since its tables were enabled for CDC, Debezium resumed
 * from the offset its no_data snapshot stored and then failed every poll with
 * error 313 ("An insufficient number of arguments were supplied for the
 * procedure or function cdc.fn_cdc_get_all_changes_..."), which SQL Server
 * returns when the requested LSN range is outside what the capture instance
 * holds. It retried every 10 s for hours, wrote no heartbeat, and the deploy
 * failed. The SQL Server Agent capture job records empty scans (tran_id 0x00) in
 * cdc.lsn_time_mapping, so the stored LSN was past the last real transaction;
 * one real write on the source cleared it at once.
 *
 * So, for the SQL Server connector only, this writes one row to the link's
 * signalling table (CDC-enabled already, which Debezium requires) and deletes it
 * again: soon after Debezium starts, and then every KEEPALIVE seconds (default
 * 300, 0 turns it off). The row is a Debezium "log" signal, so if Debezium reads
 * it, it only logs a line. It never stops or delays capture: a failed write is
 * logged once and retried at the next interval.
 *
 * Plain JDBC with the connector's own settings and credentials. The SQL Server
 * driver is in the Debezium image (the connector uses it); it is loaded by name,
 * so this compiles without it.
 */
package io.debezium.server.redis;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.SQLException;
import java.util.Map;
import java.util.Properties;
import java.util.UUID;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.regex.Pattern;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

public class SqlServerKeepAlive {
    private static final Logger LOGGER = LoggerFactory.getLogger(SqlServerKeepAlive.class);

    static final String DRIVER = "com.microsoft.sqlserver.jdbc.SQLServerDriver";
    static final String INTERVAL = "offset.storage.ecdl.sqlserver.keepalive.seconds";
    static final long DEFAULT_SECONDS = 300;
    static final long FIRST_DELAY_SECONDS = 20;
    private static final int TIMEOUT_SECONDS = 10;
    /** The names go into SQL text (names cannot be bound), so only plain identifiers. */
    private static final Pattern NAME = Pattern.compile("[A-Za-z_][A-Za-z0-9_]{0,127}");

    final String host;
    final String port;
    final String database;
    final String schema;
    final String table;
    final long seconds;
    final boolean encrypt;
    final boolean trustCertificate;
    private final Properties connection = new Properties();
    private ScheduledExecutorService timer;
    private volatile boolean warned;

    SqlServerKeepAlive(Map<String, String> props) {
        host = props.getOrDefault("database.hostname", "");
        port = props.getOrDefault("database.port", "1433");
        String names = props.getOrDefault("database.names", "");
        database = names.split(",")[0].trim();
        String[] parts = props.getOrDefault("signal.data.collection", "").split("\\.");
        table = parts.length > 0 ? parts[parts.length - 1] : "";
        schema = parts.length > 1 ? parts[parts.length - 2] : "dbo";
        seconds = parseSeconds(props.get(INTERVAL));
        encrypt = Boolean.parseBoolean(props.getOrDefault("database.encrypt", "false"));
        trustCertificate = Boolean.parseBoolean(props.getOrDefault("database.trustServerCertificate", "true"));
        put("user", props.get("database.user"));
        put("password", props.get("database.password"));
        // Verified like the connector's own connection (application-sqlserver.properties).
        put("trustStore", props.get("database.trustStore"));
        put("trustStorePassword", props.get("database.trustStorePassword"));
    }

    /** Only the SQL Server connector polls change tables. */
    static boolean applies(Map<String, String> props) {
        return String.valueOf(props.get("connector.class")).endsWith(".SqlServerConnector");
    }

    static long parseSeconds(String raw) {
        if (raw == null || raw.trim().isEmpty()) {
            return DEFAULT_SECONDS;
        }
        try {
            long v = Long.parseLong(raw.trim());
            return v < 0 ? DEFAULT_SECONDS : v;
        }
        catch (NumberFormatException e) {
            return DEFAULT_SECONDS;
        }
    }

    /** Why it cannot run, or null when it can. */
    String unusable() {
        if (seconds == 0) {
            return "turned off (" + INTERVAL + "=0)";
        }
        if (host.isEmpty() || database.isEmpty()) {
            return "no database.hostname or database.names";
        }
        if (!NAME.matcher(schema).matches() || !NAME.matcher(table).matches()) {
            return "signal.data.collection is not schema.table of plain names";
        }
        return null;
    }

    private void put(String name, String value) {
        if (value != null && !value.isEmpty()) {
            connection.setProperty(name, value);
        }
    }

    String url() {
        return "jdbc:sqlserver://" + host + ":" + port + ";databaseName=" + database
                + ";encrypt=" + encrypt + ";trustServerCertificate=" + trustCertificate
                + ";loginTimeout=" + TIMEOUT_SECONDS + ";applicationName=ecdl-keepalive";
    }

    String insertSql() {
        return "INSERT INTO [" + schema + "].[" + table + "] (id, type, data) VALUES (?, 'log', "
                + "'{\"message\":\"ECDL keep-alive: a write so an idle source has a recent transaction\"}')";
    }

    String deleteSql() {
        return "DELETE FROM [" + schema + "].[" + table + "] WHERE id = ?";
    }

    synchronized void start() {
        String why = unusable();
        if (why != null) {
            LOGGER.info("ECDL SQL Server keep-alive not running: {}", why);
            return;
        }
        timer = Executors.newSingleThreadScheduledExecutor(r -> {
            Thread t = new Thread(r, "ecdl-sqlserver-keepalive");
            t.setDaemon(true);
            return t;
        });
        timer.scheduleWithFixedDelay(this::touch, FIRST_DELAY_SECONDS, seconds, TimeUnit.SECONDS);
        LOGGER.info("ECDL SQL Server keep-alive: a write to [{}].[{}] in {} after {}s, then every {}s",
                schema, table, database, FIRST_DELAY_SECONDS, seconds);
    }

    synchronized void stop() {
        if (timer != null) {
            timer.shutdownNow();
            timer = null;
        }
    }

    /** One write. Never throws: the timer must keep running. */
    void touch() {
        try {
            Class.forName(DRIVER);
        }
        catch (ClassNotFoundException e) {
            warnOnce("the SQL Server JDBC driver is not on the classpath");
            return;
        }
        String id = "ecdl-ka-" + UUID.randomUUID().toString().substring(0, 30);
        try (Connection c = DriverManager.getConnection(url(), connection)) {
            try (PreparedStatement insert = c.prepareStatement(insertSql())) {
                insert.setQueryTimeout(TIMEOUT_SECONDS);
                insert.setString(1, id);
                insert.executeUpdate();
            }
            // Separate from the insert, so a user with INSERT but not DELETE on
            // the signalling table still gets the keep-alive (the row stays).
            try (PreparedStatement delete = c.prepareStatement(deleteSql())) {
                delete.setQueryTimeout(TIMEOUT_SECONDS);
                delete.setString(1, id);
                delete.executeUpdate();
            }
            catch (SQLException e) {
                warnOnce("the keep-alive row was written but could not be deleted (SQLState "
                        + e.getSQLState() + "): " + e.getMessage());
                return;
            }
            if (warned) {
                LOGGER.info("ECDL SQL Server keep-alive: writing again");
                warned = false;
            }
        }
        catch (SQLException e) {
            // The password never reaches the message: the driver does not echo it.
            warnOnce("SQLState " + e.getSQLState() + ": " + e.getMessage());
        }
        catch (RuntimeException e) {
            warnOnce(String.valueOf(e));
        }
    }

    private void warnOnce(String why) {
        if (!warned) {
            LOGGER.warn("ECDL SQL Server keep-alive: could not write to [{}].[{}] ({}). On an idle source "
                    + "Debezium may then fail with error 313 until something writes; grant the CDC user "
                    + "INSERT and DELETE on the signalling table.", schema, table, why);
            warned = true;
        }
    }
}
