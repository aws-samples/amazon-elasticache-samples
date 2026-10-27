package com.amazon.shopmind.metrics;

import com.amazon.shopmind.model.QueryMetric;
import java.util.List;

public class MetricsSnapshot {
    private long dbQueries;
    private long cacheHits;
    private double hitRate;
    private List<QueryMetric> recentQueries;

    public MetricsSnapshot() {}

    public MetricsSnapshot(long dbQueries, long cacheHits, double hitRate, List<QueryMetric> recentQueries) {
        this.dbQueries = dbQueries; this.cacheHits = cacheHits;
        this.hitRate = hitRate; this.recentQueries = recentQueries;
    }

    public long getDbQueries() { return dbQueries; }
    public long getCacheHits() { return cacheHits; }
    public double getHitRate() { return hitRate; }
    public List<QueryMetric> getRecentQueries() { return recentQueries; }
}
