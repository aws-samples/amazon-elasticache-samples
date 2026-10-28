package com.amazon.shopmind.repository;

import com.amazon.shopmind.config.CacheConfig;
import com.amazon.shopmind.metrics.CacheHitTracker;
import com.amazon.shopmind.metrics.MetricsTracker;
import com.amazon.shopmind.model.Product;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.RowMapper;
import org.springframework.stereotype.Repository;

import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

@Repository
public class ProductRepository {

    private final JdbcTemplate jdbcTemplate;
    private final MetricsTracker metricsTracker;
    private final CacheConfig cacheConfig;
    private final CacheHitTracker cacheHitTracker;

    public ProductRepository(JdbcTemplate jdbcTemplate, MetricsTracker metricsTracker,
                             CacheConfig cacheConfig, CacheHitTracker cacheHitTracker) {
        this.jdbcTemplate = jdbcTemplate;
        this.metricsTracker = metricsTracker;
        this.cacheConfig = cacheConfig;
        this.cacheHitTracker = cacheHitTracker;
    }

    public List<Product> searchProducts(String query) {
        String sql = "SELECT p.* FROM products p "
                + "LEFT JOIN product_relations pr ON p.id = pr.related_product_id "
                + "WHERE LOWER(p.name) LIKE LOWER(?) "
                + "OR LOWER(p.description) LIKE LOWER(?) "
                + "OR LOWER(p.category) LIKE LOWER(?) "
                + "OR LOWER(p.brand) LIKE LOWER(?) "
                + "OR LOWER(p.tags) LIKE LOWER(?) "
                + "GROUP BY p.id "
                + "ORDER BY p.rating DESC, p.review_count DESC "
                + "LIMIT 20";
        String param = "%" + query + "%";
        return executeQuery(sql, "search:" + query, param, param, param, param, param);
    }

    public List<Product> getByCategory(String category) {
        String sql = "SELECT p.* FROM products p "
                + "WHERE LOWER(p.category) = LOWER(?) "
                + "ORDER BY p.rating DESC, p.review_count DESC "
                + "LIMIT 20";
        return executeQuery(sql, "category:" + category, category);
    }

    public Product getById(Long id) {
        String sql = "SELECT * FROM products WHERE id = ?";
        List<Product> results = executeQuery(sql, "product:" + id, id);
        return results.isEmpty() ? null : results.get(0);
    }

    public List<Product> getRecommendations(Long userId, String currentCategory) {
        String prefSql = "SELECT preferred_categories FROM user_preferences WHERE user_id = ?";
        String categoriesCsv = executeScalarQuery(prefSql, "agent:read_profile:" + userId, String.class, userId);

        String contextSql = "SELECT DISTINCT category FROM products WHERE LOWER(category) = LOWER(?) LIMIT 1";
        String resolved = executeScalarQuery(contextSql, "agent:resolve_context:" + currentCategory, String.class, currentCategory);
        if (resolved == null) resolved = currentCategory;

        String[] categories = categoriesCsv != null ? categoriesCsv.split(",") : new String[]{resolved};
        List<Product> candidates = new ArrayList<>();
        for (String cat : categories) {
            String sql = "SELECT * FROM products WHERE LOWER(category) = LOWER(?) AND in_stock = true "
                    + "ORDER BY review_count DESC LIMIT 4";
            candidates.addAll(executeQuery(sql, "agent:query_catalog:" + cat.trim(), cat.trim()));
        }

        String relatedSql = "SELECT p.* FROM products p "
                + "JOIN product_relations pr ON p.id = pr.related_product_id "
                + "WHERE pr.category = ? AND p.in_stock = true LIMIT 5";
        candidates.addAll(executeQuery(relatedSql, "agent:find_related:" + resolved, resolved));

        return candidates.stream().distinct().limit(10).toList();
    }

    public List<String> getCategories() {
        String sql = "SELECT DISTINCT category FROM products ORDER BY category";
        return jdbcTemplate.queryForList(withCacheHint(sql), String.class);
    }

    // -- Query execution with real cache hit detection from the JDBC wrapper plugin --

    private List<Product> executeQuery(String sql, String label, Object... params) {
        String finalSql = withCacheHint(sql);
        cacheHitTracker.reset();
        long start = System.nanoTime();
        List<Product> results = jdbcTemplate.query(finalSql, new ProductRowMapper(), params);
        long durationMs = (System.nanoTime() - start) / 1_000_000;
        boolean cacheHit = cacheHitTracker.wasLastQueryCacheHit();
        metricsTracker.recordQuery(label, durationMs, cacheHit);
        return results;
    }

    private <T> T executeScalarQuery(String sql, String label, Class<T> type, Object... params) {
        String finalSql = withCacheHint(sql);
        cacheHitTracker.reset();
        long start = System.nanoTime();
        List<T> results = jdbcTemplate.queryForList(finalSql, type, params);
        long durationMs = (System.nanoTime() - start) / 1_000_000;
        boolean cacheHit = cacheHitTracker.wasLastQueryCacheHit();
        metricsTracker.recordQuery(label, durationMs, cacheHit);
        return results.isEmpty() ? null : results.get(0);
    }

    // THE ONLY PER-QUERY CODE CHANGE:
    // Prefix cacheable queries with the CACHE_PARAM hint.
    // The JDBC wrapper plugin sees this and caches the result in Valkey.
    private String withCacheHint(String sql) {
        if (!cacheConfig.isEnabled()) {
            return sql;
        }
        return String.format("/* CACHE_PARAM(ttl=%ds) */ %s", cacheConfig.getTtlSeconds(), sql);
    }

    private static class ProductRowMapper implements RowMapper<Product> {
        @Override
        public Product mapRow(ResultSet rs, int rowNum) throws SQLException {
            return new Product(
                    rs.getLong("id"), rs.getString("name"), rs.getString("description"),
                    rs.getString("category"), rs.getString("brand"), rs.getBigDecimal("price"),
                    rs.getDouble("rating"), rs.getInt("review_count"), rs.getString("image_url"),
                    rs.getBoolean("in_stock"), Arrays.asList(rs.getString("tags").split(","))
            );
        }
    }
}
