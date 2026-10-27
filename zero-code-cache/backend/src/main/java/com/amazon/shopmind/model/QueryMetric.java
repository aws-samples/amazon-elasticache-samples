package com.amazon.shopmind.model;

public class QueryMetric {
    private String query;
    private long durationMs;
    private boolean cacheHit;
    private long timestamp;

    public QueryMetric() {}

    public QueryMetric(String query, long durationMs, boolean cacheHit, long timestamp) {
        this.query = query; this.durationMs = durationMs;
        this.cacheHit = cacheHit; this.timestamp = timestamp;
    }

    public String getQuery() { return query; }
    public long getDurationMs() { return durationMs; }
    public boolean isCacheHit() { return cacheHit; }
    public long getTimestamp() { return timestamp; }
}
