package com.amazon.shopmind.model;

import java.math.BigDecimal;
import java.util.List;

public class Product {
    private Long id;
    private String name;
    private String description;
    private String category;
    private String brand;
    private BigDecimal price;
    private double rating;
    private int reviewCount;
    private String imageUrl;
    private boolean inStock;
    private List<String> tags;

    public Product() {}

    public Product(Long id, String name, String description, String category, String brand,
                   BigDecimal price, double rating, int reviewCount, String imageUrl,
                   boolean inStock, List<String> tags) {
        this.id = id; this.name = name; this.description = description;
        this.category = category; this.brand = brand; this.price = price;
        this.rating = rating; this.reviewCount = reviewCount; this.imageUrl = imageUrl;
        this.inStock = inStock; this.tags = tags;
    }

    public Long getId() { return id; }
    public String getName() { return name; }
    public String getDescription() { return description; }
    public String getCategory() { return category; }
    public String getBrand() { return brand; }
    public BigDecimal getPrice() { return price; }
    public double getRating() { return rating; }
    public int getReviewCount() { return reviewCount; }
    public String getImageUrl() { return imageUrl; }
    public boolean isInStock() { return inStock; }
    public List<String> getTags() { return tags; }

    public void setId(Long id) { this.id = id; }
    public void setName(String name) { this.name = name; }
    public void setDescription(String description) { this.description = description; }
    public void setCategory(String category) { this.category = category; }
    public void setBrand(String brand) { this.brand = brand; }
    public void setPrice(BigDecimal price) { this.price = price; }
    public void setRating(double rating) { this.rating = rating; }
    public void setReviewCount(int reviewCount) { this.reviewCount = reviewCount; }
    public void setImageUrl(String imageUrl) { this.imageUrl = imageUrl; }
    public void setInStock(boolean inStock) { this.inStock = inStock; }
    public void setTags(List<String> tags) { this.tags = tags; }

    @Override
    public boolean equals(Object o) {
        if (this == o) return true;
        if (o == null || getClass() != o.getClass()) return false;
        return id != null && id.equals(((Product) o).id);
    }

    @Override
    public int hashCode() { return id != null ? id.hashCode() : 0; }
}
