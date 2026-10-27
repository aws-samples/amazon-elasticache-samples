package com.amazon.shopmind.metrics;

import com.amazon.shopmind.model.QueryMetric;
import org.springframework.messaging.simp.SimpMessagingTemplate;
import org.springframework.stereotype.Component;

import java.util.concurrent.ConcurrentLinkedDeque;
import java.util.concurrent.atomic.AtomicLong;

@Component
public class MetricsTracker {

    private final SimpMessagingTemplate messagingTemplate;
    private final ConcurrentLinkedDeque<QueryMetric> recentQueries = new ConcurrentLinkedDeque<>();
    private final AtomicLong totalDbQueries = new AtomicLong(0);
    private final AtomicLong totalCacheHits = new AtomicLong(0);

    public MetricsTracker(SimpMessagingTemplate messagingTemplate) {
        this.messagingTemplate = messagingTemplate;
    }

    public void recordQuery(String query, long durationMs, boolean cacheHit) {
        if (cacheHit) {
            totalCacheHits.incrementAndGet();
        } else {
            totalDbQueries.incrementAndGet();
        }

        QueryMetric metric = new QueryMetric(
                truncateQuery(query), durationMs, cacheHit, System.currentTimeMillis());

        recentQueries.addFirst(metric);
        while (recentQueries.size() > 50) {
            recentQueries.removeLast();
        }
    }

    // Record query without cache hit info — just track the time
    public void recordQuery(String query, long durationMs) {
        totalDbQueries.incrementAndGet();

        QueryMetric metric = new QueryMetric(
                truncateQuery(query), durationMs, false, System.currentTimeMillis());

        recentQueries.addFirst(metric);
        while (recentQueries.size() > 50) {
            recentQueries.removeLast();
        }
    }

    public MetricsSnapshot getSnapshot() {
        long db = totalDbQueries.get();
        long cache = totalCacheHits.get();
        long total = db + cache;
        double hitRate = total > 0 ? (double) cache / total * 100 : 0;

        return new MetricsSnapshot(db, cache,
                Math.round(hitRate * 10) / 10.0,
                recentQueries.stream().limit(20).toList());
    }

    public void reset() {
        totalDbQueries.set(0);
        totalCacheHits.set(0);
        recentQueries.clear();
        messagingTemplate.convertAndSend("/topic/metrics", getSnapshot());
    }

    private String truncateQuery(String query) {
        return query.length() > 80 ? query.substring(0, 80) + "..." : query;
    }

    /** Returns the duration of the most recently recorded query. */
    public long getLastQueryDurationMs() {
        QueryMetric first = recentQueries.peekFirst();
        return first != null ? first.getDurationMs() : 0;
    }
}
