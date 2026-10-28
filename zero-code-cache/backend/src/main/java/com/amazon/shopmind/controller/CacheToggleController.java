package com.amazon.shopmind.controller;

import com.amazon.shopmind.config.CacheConfig;
import org.springframework.web.bind.annotation.*;

import java.util.Map;

@RestController
@RequestMapping("/api/cache")
@CrossOrigin(origins = "*")
public class CacheToggleController {

    private final CacheConfig cacheConfig;

    public CacheToggleController(CacheConfig cacheConfig) {
        this.cacheConfig = cacheConfig;
    }

    @GetMapping("/status")
    public Map<String, Object> getStatus() {
        return Map.of(
                "enabled", cacheConfig.isEnabled(),
                "endpoint", cacheConfig.getEndpoint(),
                "ttlSeconds", cacheConfig.getTtlSeconds()
        );
    }

    @PostMapping("/toggle")
    public Map<String, Boolean> toggle() {
        cacheConfig.toggle();
        return Map.of("enabled", cacheConfig.isEnabled());
    }
}
