package com.amazon.shopmind.config;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.jdbc.core.JdbcTemplate;

import javax.sql.DataSource;

@Configuration
public class DataSourceConfig {

    @Value("${DB_HOST:localhost}")
    private String dbHost;

    @Value("${DB_PORT:5432}")
    private String dbPort;

    @Value("${DB_NAME:shopmind}")
    private String dbName;

    @Value("${spring.datasource.username}")
    private String username;

    @Value("${spring.datasource.password}")
    private String password;

    @Value("${cache.enabled:false}")
    private boolean cacheEnabled;

    @Value("${cache.endpoint:localhost:6379}")
    private String cacheEndpoint;

    @Value("${cache.use-ssl:false}")
    private boolean cacheUseSsl;

    // Strict allowlist patterns for the components that make up the JDBC URL.
    // Validating these before building the connection string prevents JDBC
    // connection-string injection: no scheme separators, query delimiters, or
    // whitespace can slip in to alter the resolved URL or append extra
    // properties. Hostnames allow DNS/host chars; the DB name is a plain
    // identifier; the port must be numeric and in range.
    private static final java.util.regex.Pattern HOST_PATTERN =
            java.util.regex.Pattern.compile("^[A-Za-z0-9._-]{1,253}$");
    private static final java.util.regex.Pattern DB_NAME_PATTERN =
            java.util.regex.Pattern.compile("^[A-Za-z0-9_]{1,63}$");

    private static String requireValidHost(String value) {
        if (value == null || !HOST_PATTERN.matcher(value).matches()) {
            throw new IllegalStateException("Invalid database host configuration");
        }
        return value;
    }

    private static String requireValidDbName(String value) {
        if (value == null || !DB_NAME_PATTERN.matcher(value).matches()) {
            throw new IllegalStateException("Invalid database name configuration");
        }
        return value;
    }

    private static int requireValidPort(String value) {
        final int port;
        try {
            port = Integer.parseInt(value == null ? "" : value.trim());
        } catch (NumberFormatException e) {
            throw new IllegalStateException("Invalid database port configuration");
        }
        if (port < 1 || port > 65535) {
            throw new IllegalStateException("Database port out of range");
        }
        return port;
    }

    @Bean
    public DataSource dataSource() {
        // Validate every component against a strict allowlist before it is used
        // to compose the JDBC URL.
        final String host = requireValidHost(dbHost);
        final int port = requireValidPort(dbPort);
        final String database = requireValidDbName(dbName);

        HikariConfig config = new HikariConfig();
        config.setUsername(username);
        config.setPassword(password);
        config.setMaximumPoolSize(10);
        config.setMinimumIdle(2);

        if (cacheEnabled) {
            // --- WITH CACHE: AWS Advanced JDBC Wrapper ---
            // Change 1: Switch driver + URL prefix
            // Change 2: Add 3 wrapper properties
            config.setDriverClassName("software.amazon.jdbc.Driver");
            config.setJdbcUrl(buildJdbcUrl("jdbc:aws-wrapper:postgresql", host, port, database));
            config.addDataSourceProperty("wrapperPlugins", "remoteQueryCache");
            config.addDataSourceProperty("cacheEndpointAddrRw", cacheEndpoint);
            config.addDataSourceProperty("cacheUseSSL", String.valueOf(cacheUseSsl));

            System.out.println("=== CACHE MODE: JDBC Wrapper + Valkey ===");
            System.out.println("    Endpoint: " + cacheEndpoint);
        } else {
            // --- WITHOUT CACHE: Plain PostgreSQL driver ---
            config.setDriverClassName("org.postgresql.Driver");
            config.setJdbcUrl(buildJdbcUrl("jdbc:postgresql", host, port, database));

            System.out.println("=== NO CACHE: Direct PostgreSQL ===");
        }

        return new HikariDataSource(config);
    }

    // Composes the JDBC URL only from pre-validated, allowlisted components.
    private static String buildJdbcUrl(String scheme, String host, int port, String database) {
        return scheme + "://" + host + ":" + port + "/" + database;
    }

    @Bean
    public JdbcTemplate jdbcTemplate(DataSource dataSource) {
        return new JdbcTemplate(dataSource);
    }
}
