package com.example.elasticache_demo;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;
import org.springframework.cache.CacheManager;
import org.springframework.cache.Cache;

import org.springframework.boot.convert.DurationStyle;
import org.springframework.util.StringUtils;

import glide.api.models.configuration.ServerCredentials;
import glide.api.models.configuration.NodeAddress;
import glide.api.models.configuration.GlideClusterClientConfiguration;

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

    @Value("${spring.data.valkey.username}")
    private String username;

    @Value("${spring.data.valkey.cacheName}")
    private String cacheName;

    @Value("${spring.data.valkey.region}")
    private String region;

    private Map<String, SimpleValkeyGlideCache> caches = new HashMap<String, SimpleValkeyGlideCache>();
    private Map<String, Long> cacheReauthenticated = new HashMap<String, Long>();

    private GlideClusterClientConfiguration config;
    private Duration ttlDuration;
    private ElasticachePasswordGenerator generator;

    private static final long TEN_HOURS = 60 * 60 * 10 * 1000;

    public void setCacheConfiguration(GlideClusterClientConfiguration config) {
        this.config = config;
    }

    public Cache getCache(String name) {
        if (!caches.containsKey(name)) {
            if( generator == null ) {
                generator = ElasticachePasswordGenerator.create(cacheName, region, username, false);
            }
            
            if (config == null) {
                ServerCredentials credentials = ServerCredentials.builder()
                    .username(username)
                    .password(generator.generatePassword())
                    .build();
                
                config = GlideClusterClientConfiguration.builder()
                    .address(NodeAddress.builder().host(host).port(port).build())
                    .useTLS(useTLS)
                    .credentials(credentials)
                    .build();

                if (ttlDuration == null && !StringUtils.isEmpty(timeToLive)) {
                    ttlDuration = DurationStyle.detectAndParse(timeToLive);
                }
            }

            caches.put(name, new SimpleValkeyGlideCache(name, config, ttlDuration));
            cacheReauthenticated.put(name, System.currentTimeMillis());
        }

        if(cacheReauthenticated.get(name) > System.currentTimeMillis() + TEN_HOURS) {
            caches.get(name).updatePassword(generator.generatePassword(), true);
            cacheReauthenticated.put(name, System.currentTimeMillis());
        }   

        return caches.get(name);
    }

    public Collection<String> getCacheNames() {
        return caches.keySet();
    }
}