package com.amazon.shopmind.controller;

import com.amazon.shopmind.metrics.MetricsSnapshot;
import com.amazon.shopmind.metrics.MetricsTracker;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/metrics")
@CrossOrigin(origins = "*")
public class MetricsController {

    private final MetricsTracker metricsTracker;

    @Value("${cache.endpoint:localhost:6379}")
    private String cacheEndpoint;

    @Value("${cache.use-ssl:false}")
    private boolean cacheUseSsl;

    public MetricsController(MetricsTracker metricsTracker) {
        this.metricsTracker = metricsTracker;
    }

    @GetMapping
    public MetricsSnapshot getMetrics() {
        return metricsTracker.getSnapshot();
    }

    @PostMapping("/reset")
    public void resetMetrics() {
        metricsTracker.reset();
        flushValkey();
    }

    private void flushValkey() {
        try {
            String[] parts = cacheEndpoint.split(":");
            String host = parts[0];
            int port = parts.length > 1 ? Integer.parseInt(parts[1]) : 6379;

            if (cacheUseSsl) {
                // TLS connection for ElastiCache
                javax.net.ssl.SSLSocketFactory factory =
                    (javax.net.ssl.SSLSocketFactory) javax.net.ssl.SSLSocketFactory.getDefault();
                try (java.net.Socket socket = factory.createSocket(host, port)) {
                    socket.setSoTimeout(3000);
                    java.io.OutputStream out = socket.getOutputStream();
                    out.write("*1\r\n$8\r\nFLUSHALL\r\n".getBytes());
                    out.flush();
                    byte[] buf = new byte[64];
                    socket.getInputStream().read(buf);
                }
            } else {
                // Plain connection for local Docker
                try (java.net.Socket socket = new java.net.Socket(host, port)) {
                    socket.setSoTimeout(3000);
                    java.io.OutputStream out = socket.getOutputStream();
                    out.write("*1\r\n$8\r\nFLUSHALL\r\n".getBytes());
                    out.flush();
                    byte[] buf = new byte[64];
                    socket.getInputStream().read(buf);
                }
            }
            System.out.println("[CACHE] Valkey flushed");
        } catch (Exception e) {
            System.out.println("[CACHE] Flush failed: " + e.getMessage());
        }
    }
}
