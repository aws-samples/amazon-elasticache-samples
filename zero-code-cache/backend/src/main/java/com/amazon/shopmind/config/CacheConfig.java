package com.amazon.shopmind.config;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import java.util.concurrent.atomic.AtomicBoolean;

@Component
public class CacheConfig {

    private final AtomicBoolean enabled;
    private final String endpoint;
    private final boolean useSsl;
    private final int ttlSeconds;

    public CacheConfig(
            @Value("${cache.enabled:false}") boolean enabled,
            @Value("${cache.endpoint:localhost:6379}") String endpoint,
            @Value("${cache.use-ssl:false}") boolean useSsl,
            @Value("${cache.ttl-seconds:300}") int ttlSeconds) {
        this.enabled = new AtomicBoolean(enabled);
        this.endpoint = endpoint;
        this.useSsl = useSsl;
        this.ttlSeconds = ttlSeconds;
    }

    public boolean isEnabled() { return enabled.get(); }
    public void toggle() { enabled.set(!enabled.get()); }
    public String getEndpoint() { return endpoint; }
    public boolean isUseSsl() { return useSsl; }
    public int getTtlSeconds() { return ttlSeconds; }
}
