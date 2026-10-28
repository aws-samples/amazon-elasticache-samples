package com.amazon.shopmind.metrics;

import org.springframework.stereotype.Component;

import jakarta.annotation.PostConstruct;
import java.util.concurrent.atomic.AtomicReference;
import java.util.logging.Handler;
import java.util.logging.Level;
import java.util.logging.LogRecord;
import java.util.logging.Logger;

@Component
public class CacheHitTracker {

    private final AtomicReference<Boolean> lastCacheResult = new AtomicReference<>(null);

    @PostConstruct
    public void init() {
        // Disable the JUL-to-SLF4J bridge for the cache plugin logger
        // so our handler receives the messages directly
        Logger cacheLogger = Logger.getLogger("software.amazon.jdbc.plugin.cache.RemoteQueryCachePlugin");
        cacheLogger.setUseParentHandlers(false); // Stop SLF4J bridge from intercepting
        cacheLogger.setLevel(Level.ALL);

        Handler handler = new Handler() {
            { setLevel(Level.ALL); }

            @Override
            public void publish(LogRecord record) {
                if (record == null || record.getMessage() == null) return;
                String msg = record.getMessage().toLowerCase();
                if (msg.contains("hit") && !msg.contains("miss")) {
                    lastCacheResult.set(true);
                    System.out.println("[CACHE-TRACKER] HIT: " + record.getMessage().substring(0, Math.min(record.getMessage().length(), 80)));
                } else if (msg.contains("miss")) {
                    lastCacheResult.set(false);
                    System.out.println("[CACHE-TRACKER] MISS: " + record.getMessage().substring(0, Math.min(record.getMessage().length(), 80)));
                }
            }

            @Override public void flush() {}
            @Override public void close() {}
        };

        cacheLogger.addHandler(handler);

        // Also attach to parent loggers in case the plugin uses a different logger name
        for (String name : new String[]{
                "software.amazon.jdbc.plugin.cache",
                "software.amazon.jdbc.plugin",
                "software.amazon.jdbc"}) {
            Logger l = Logger.getLogger(name);
            l.setLevel(Level.ALL);
        }

        System.out.println("[CacheHitTracker] Initialized — JUL handler attached, parent handlers disabled");
    }

    public void reset() {
        lastCacheResult.set(null);
    }

    public boolean wasLastQueryCacheHit() {
        Boolean result = lastCacheResult.getAndSet(null);
        return result != null && result;
    }
}
