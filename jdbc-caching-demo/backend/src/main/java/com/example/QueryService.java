package com.example;

import com.example.model.QueryResult;
import jakarta.annotation.PreDestroy;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import java.sql.*;
import java.util.*;
import java.util.logging.Logger;

@Service
public class QueryService {

    private static final Logger log = Logger.getLogger(QueryService.class.getName());

    static final String PRODUCT_CATALOG_QUERY =
        "SELECT " +
        "location, " +
        "COUNT(*) AS total_cinemas, " +
        "AVG(capacity) AS avg_capacity, " +
        "MAX(capacity) AS largest, " +
        "MIN(capacity) AS smallest, " +
        "SUM(capacity) AS total_seats " +
        "FROM cinemas " +
        "GROUP BY location " +
        "ORDER BY total_seats DESC";

    @Value("${db.host}")                    private String dbHost;
    @Value("${db.port}")                    private String dbPort;
    @Value("${db.name}")                    private String dbName;
    @Value("${db.user}")                    private String dbUser;
    @Value("${db.password}")                private String dbPassword;
    @Value("${cache.endpoint}")             private String cacheEndpoint;
    @Value("${cache.use-ssl}")              private String cacheUseSsl;
    @Value("${cache.name:}")               private String cacheName;
    @Value("${cache.iam-region:us-east-1}") private String cacheIamRegion;

    private Connection directConn;
    private Connection cachedConn;

    // ── Connection lifecycle ──────────────────────────────────────────────────

    public Map<String, Object> connect() {
        Map<String, Object> status = new LinkedHashMap<>();
        // Direct
        try {
            closeQuietly(directConn);
            directConn = openConnection(false);
            status.put("direct", "connected");
            log.info("✓ Direct connection ready");
        } catch (SQLException e) {
            status.put("direct", "error: " + e.getMessage());
            log.warning("✗ Direct connection failed: " + e.getMessage());
        }
        // Cached
        try {
            closeQuietly(cachedConn);
            cachedConn = openConnection(true);
            status.put("cached", "connected");
            log.info("✓ Cached connection ready");
        } catch (SQLException e) {
            status.put("cached", "error: " + e.getMessage());
            log.warning("✗ Cached connection failed: " + e.getMessage());
        }
        return status;
    }

    public Map<String, Object> disconnect() {
        closeQuietly(directConn);  directConn = null;
        closeQuietly(cachedConn);  cachedConn = null;
        log.info("Connections closed");
        return Map.of("direct", "disconnected", "cached", "disconnected");
    }

    public Map<String, Object> status() {
        return Map.of(
            "direct", connStatus(directConn),
            "cached", connStatus(cachedConn)
        );
    }

    private String connStatus(Connection c) {
        try { return (c != null && !c.isClosed()) ? "connected" : "disconnected"; }
        catch (SQLException e) { return "error"; }
    }

    // ── Query execution ───────────────────────────────────────────────────────

    public QueryResult execute(boolean useCache, boolean includePlan, String customSql, String ttl) throws SQLException {
        Connection conn = useCache ? cachedConn : directConn;
        if (conn == null || conn.isClosed()) {
            throw new SQLException((useCache ? "Cached" : "Direct") +
                " connection is not open. Use the Connect button first.");
        }

        String baseSql = (customSql != null && !customSql.isBlank()) ? customSql : PRODUCT_CATALOG_QUERY;
        String effectiveTtl = (ttl != null && !ttl.isBlank()) ? ttl : "300s";
        String hint = "/* CACHE_PARAM(ttl=" + effectiveTtl + ") */";
        String sql = useCache ? hint + " " + baseSql : baseSql;

        QueryResult result = new QueryResult();
        result.setMode(useCache ? "cached" : "direct");
        result.setCacheEnabled(useCache);
        result.setQuery(sql);
        result.setCacheHint(useCache ? hint : null);

        long t1 = System.currentTimeMillis();
        try (PreparedStatement stmt = conn.prepareStatement(sql);
             ResultSet rs = stmt.executeQuery()) {

            long queryMs = System.currentTimeMillis() - t1;

            long t2 = System.currentTimeMillis();
            ResultSetMetaData meta = rs.getMetaData();
            int colCount = meta.getColumnCount();
            List<String> columns = new ArrayList<>();
            for (int i = 1; i <= colCount; i++) columns.add(meta.getColumnLabel(i));
            result.setColumns(columns);

            List<Map<String, Object>> rows = new ArrayList<>();
            while (rs.next()) {
                Map<String, Object> row = new LinkedHashMap<>();
                for (String col : columns) {
                    Object val = rs.getObject(col);
                    if (val instanceof Double d) val = Math.round(d * 100.0) / 100.0;
                    row.put(col, val);
                }
                rows.add(row);
            }
            long fetchMs = System.currentTimeMillis() - t2;

            result.setRows(rows);
            result.setRowCount(rows.size());
            result.setQueryMs(queryMs);
            result.setFetchMs(fetchMs);
            result.setLatencyMs(queryMs + fetchMs);

            log.info(String.format("[%s] query=%dms fetch=%dms rows=%d",
                useCache ? "CACHED" : "DIRECT", queryMs, fetchMs, rows.size()));
        }

        if (includePlan) result.setQueryPlan(fetchQueryPlan(baseSql));
        return result;
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    private Connection openConnection(boolean useCache) throws SQLException {
        String url = useCache
            ? "jdbc:aws-wrapper:postgresql://" + dbHost + ":" + dbPort + "/" + dbName + "?sslmode=require"
            : "jdbc:postgresql://" + dbHost + ":" + dbPort + "/" + dbName + "?sslmode=require";

        Properties props = new Properties();
        props.setProperty("user", dbUser);
        props.setProperty("password", dbPassword);

        if (useCache) {
            props.setProperty("wrapperPlugins", "remoteQueryCache");
            props.setProperty("cacheEndpointAddrRw", cacheEndpoint);
            props.setProperty("cacheEndpointAddrRo", cacheEndpoint);
            props.setProperty("cacheUseSSL", cacheUseSsl);
            props.setProperty("cacheConnectionTimeoutMs", "5000");
            props.setProperty("cacheConnectionPoolSize", "10");
            props.setProperty("failWhenCacheDown", "false");
            if (cacheName != null && !cacheName.isBlank()) {
                props.setProperty("cacheName", cacheName);
                props.setProperty("cacheUsername", "default");
                props.setProperty("cacheIamRegion", cacheIamRegion);
            }
        }
        return DriverManager.getConnection(url, props);
    }

    private String fetchQueryPlan(String sql) {
        try (Connection c = openConnection(false);
             Statement stmt = c.createStatement();
             ResultSet rs = stmt.executeQuery("EXPLAIN ANALYZE " + sql)) {
            StringBuilder plan = new StringBuilder();
            while (rs.next()) plan.append(rs.getString(1)).append("\n");
            return plan.toString();
        } catch (SQLException e) {
            return "Could not fetch query plan: " + e.getMessage();
        }
    }

    private void closeQuietly(Connection c) {
        try { if (c != null && !c.isClosed()) c.close(); } catch (SQLException ignored) {}
    }

    @PreDestroy
    public void destroy() { disconnect(); }
}
