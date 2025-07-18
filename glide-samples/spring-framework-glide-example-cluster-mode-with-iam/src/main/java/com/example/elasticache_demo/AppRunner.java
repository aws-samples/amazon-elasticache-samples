package com.example.elasticache_demo;

import org.springframework.boot.CommandLineRunner;
import org.springframework.stereotype.Component;

@Component
public class AppRunner implements CommandLineRunner {

    private static final long TEN_MINUTES = 60 * 10 * 1000;    
    private static final long ONE_HOUR_MILLIS = TEN_MINUTES * 6;
    private static final long TWENTY_HOUR_MILLIS = ONE_HOUR_MILLIS * 20;
    
    private CacheableComponent cacheableComponent;

    public AppRunner(CacheableComponent cacheableComponent) {
        this.cacheableComponent = cacheableComponent;
    }

    @Override
    public void run(String... args) throws Exception {
        long started = System.currentTimeMillis();
        while ( System.currentTimeMillis() < started + TWENTY_HOUR_MILLIS ) {
            String newValue = cacheableComponent.getCacheableValue("test-key" + System.currentTimeMillis());
            System.out.println("READ " + newValue);
            Thread.sleep(TEN_MINUTES);
        }
    }
}