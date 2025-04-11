package com.example.elasticache_demo;

import org.springframework.boot.CommandLineRunner;
import org.springframework.stereotype.Component;

@Component
public class AppRunner implements CommandLineRunner {

    private CacheableComponent cacheableComponent;

    public AppRunner(CacheableComponent cacheableComponent) {
        this.cacheableComponent = cacheableComponent;
    }

    @Override
    public void run(String... args) throws Exception {
        int cacheHits = 0;
        int cacheMisses = 0;

        for( int i=0 ; i < 100 ; i++ ) {
            long started = System.currentTimeMillis();
            String newValue = cacheableComponent.getCacheableValue("test-key");
            long completed = System.currentTimeMillis();
            if ( completed - started < 4000 ) {
                cacheHits++;
            }
            else {
                cacheMisses++;               
            }
        }

        System.out.println("");
        System.out.println("Cache hits: " + cacheHits);
        System.out.println("Cache misses: " + cacheMisses);
        System.out.println("");         
    }
}
