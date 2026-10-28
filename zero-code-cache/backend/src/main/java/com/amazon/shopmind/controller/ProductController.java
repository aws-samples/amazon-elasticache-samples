package com.amazon.shopmind.controller;

import com.amazon.shopmind.metrics.MetricsTracker;
import com.amazon.shopmind.model.Product;
import com.amazon.shopmind.model.TimedResponse;
import com.amazon.shopmind.repository.ProductRepository;
import org.springframework.web.bind.annotation.*;

import java.util.List;

@RestController
@RequestMapping("/api/products")
@CrossOrigin(origins = "*")
public class ProductController {

    private final ProductRepository productRepository;
    private final MetricsTracker metricsTracker;

    public ProductController(ProductRepository productRepository, MetricsTracker metricsTracker) {
        this.productRepository = productRepository;
        this.metricsTracker = metricsTracker;
    }

    @GetMapping("/search")
    public TimedResponse<List<Product>> search(@RequestParam String q) {
        List<Product> results = productRepository.searchProducts(q);
        long dbMs = metricsTracker.getLastQueryDurationMs();
        return new TimedResponse<>(results, dbMs, dbMs, 0);
    }

    @GetMapping("/category/{category}")
    public TimedResponse<List<Product>> byCategory(@PathVariable String category) {
        List<Product> results = productRepository.getByCategory(category);
        long dbMs = metricsTracker.getLastQueryDurationMs();
        return new TimedResponse<>(results, dbMs, dbMs, 0);
    }

    @GetMapping("/{id}")
    public Product getProduct(@PathVariable Long id) {
        return productRepository.getById(id);
    }

    @GetMapping("/recommendations")
    public TimedResponse<List<Product>> recommendations(
            @RequestParam(defaultValue = "1") Long userId,
            @RequestParam(defaultValue = "Electronics") String category) {
        List<Product> results = productRepository.getRecommendations(userId, category);
        long dbMs = metricsTracker.getLastQueryDurationMs();
        return new TimedResponse<>(results, dbMs, dbMs, 0);
    }

    @GetMapping("/categories")
    public List<String> categories() {
        return productRepository.getCategories();
    }
}
