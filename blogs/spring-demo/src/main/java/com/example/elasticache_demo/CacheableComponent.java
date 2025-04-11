package com.example.elasticache_demo;

import org.springframework.stereotype.Component;
import org.springframework.cache.annotation.Cacheable;

@Component
public class CacheableComponent {

    @Cacheable("test")
    public String getCacheableValue(String key) {
        return key + System.currentTimeMillis();
    }
}
