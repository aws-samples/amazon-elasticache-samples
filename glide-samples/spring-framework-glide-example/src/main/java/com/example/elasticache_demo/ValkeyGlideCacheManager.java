package com.example.elasticache_demo;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;
import org.springframework.cache.CacheManager;
import org.springframework.cache.Cache;

import org.springframework.boot.convert.DurationStyle;
import org.springframework.util.StringUtils;

import glide.api.models.configuration.NodeAddress;
import glide.api.models.configuration.GlideClientConfiguration;

import java.util.Map;
import java.util.HashMap;
import java.util.Collection;
import java.time.Duration;

@Component
public class ValkeyGlideCacheManager implements CacheManager {

    @Value( "${spring.cache.valkey.time-to-live:}" )
    private String timeToLive;

    @Value( "${spring.data.valkey.ssl.enabled:false}" )
    private Boolean useTLS;

    @Value( "${spring.data.valkey.host:127.0.0.1}" )
    private String host;

    @Value( "${spring.cache.valkey.port:6379}" )
    private Integer port;

    private Map<String, SimpleValkeyGlideCache> caches = new HashMap<String, SimpleValkeyGlideCache>();

    private GlideClientConfiguration config;
    private Duration ttlDuration;

    public void setCacheConfiguration(GlideClientConfiguration config) {
        this.config = config;
    }

    public Cache getCache(String name) {
        if (!caches.containsKey(name)) {
            if (config == null) {
                config = GlideClientConfiguration.builder()
                    .address(NodeAddress.builder().host(host).port(port).build())
                    .useTLS(useTLS)
                    .build();

                if (ttlDuration == null && !StringUtils.isEmpty(timeToLive)) {
                    ttlDuration = DurationStyle.detectAndParse(timeToLive);
                }
            }

            caches.put(name, new SimpleValkeyGlideCache(name, config, ttlDuration));
        }

        return caches.get(name);
    }

    public Collection<String> getCacheNames() {
        return caches.keySet();
    }
}