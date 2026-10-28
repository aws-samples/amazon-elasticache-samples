-- Generate 100K+ products for realistic query latency
-- Run this AFTER init.sql

-- Create a function to generate random products
DO $$
DECLARE
    categories TEXT[] := ARRAY['Laptops','Monitors','Accessories','Audio','Components','Networking','Furniture','Storage'];
    brands TEXT[] := ARRAY['ASUS','Dell','HP','Lenovo','Apple','Samsung','LG','Sony','Razer','MSI','Corsair','Logitech','SteelSeries','BenQ','Acer','HyperX','JBL','Bose','Sennheiser','NZXT','Western Digital','G.Skill','TP-Link','Netgear','Ubiquiti','Synology','QNAP','SanDisk','Secretlab','Herman Miller','Steelcase','Uplift','Autonomous','Alienware','Elgato','Shure','Keychron','NVIDIA','AMD'];
    adjectives TEXT[] := ARRAY['Pro','Ultra','Elite','Max','Plus','Premium','Advanced','Extreme','Turbo','Hyper','Super','Mega','Nano','Quantum','Stellar','Apex','Prime','Titan','Nova','Zenith','Vertex','Fusion','Nexus','Phantom','Shadow','Storm','Thunder','Lightning','Blaze','Frost'];
    nouns TEXT[] := ARRAY['Runner','Shield','Core','Wave','Pulse','Drive','Force','Edge','Link','Stream','Blade','Spark','Bolt','Surge','Flux','Grid','Beam','Arc','Vibe','Drift','Glide','Swift','Rapid','Flash','Sonic','Pixel','Vector','Matrix','Orbit','Prism'];
    tag_options TEXT[] := ARRAY['gaming','productivity','creative','business','portable','wireless','mechanical','ergonomic','high-performance','budget','premium','compact','ultrawide','noise-cancelling','rgb','modular','fast','rugged','lightweight','professional'];
    i INTEGER;
    cat TEXT;
    brand TEXT;
    adj TEXT;
    noun TEXT;
    prod_name TEXT;
    prod_desc TEXT;
    prod_price DECIMAL;
    prod_rating DOUBLE PRECISION;
    prod_reviews INTEGER;
    prod_tags TEXT;
BEGIN
    FOR i IN 1..100000 LOOP
        cat := categories[1 + floor(random() * array_length(categories, 1))::int];
        brand := brands[1 + floor(random() * array_length(brands, 1))::int];
        adj := adjectives[1 + floor(random() * array_length(adjectives, 1))::int];
        noun := nouns[1 + floor(random() * array_length(nouns, 1))::int];
        prod_name := brand || ' ' || adj || ' ' || noun || ' ' || (100 + floor(random() * 900))::int;
        prod_desc := cat || ' product by ' || brand || '. Features ' || adj || ' ' || noun || ' technology with advanced specifications for ' || 
                     CASE cat 
                         WHEN 'Laptops' THEN 'computing and development'
                         WHEN 'Monitors' THEN 'visual clarity and color accuracy'
                         WHEN 'Accessories' THEN 'input precision and comfort'
                         WHEN 'Audio' THEN 'immersive sound experience'
                         WHEN 'Components' THEN 'system building and upgrades'
                         WHEN 'Networking' THEN 'connectivity and throughput'
                         WHEN 'Furniture' THEN 'ergonomic comfort and posture'
                         WHEN 'Storage' THEN 'data management and backup'
                         ELSE 'general use'
                     END;
        prod_price := 19.99 + (random() * 2980)::numeric(10,2);
        prod_rating := 3.0 + (random() * 2.0)::numeric(2,1);
        prod_reviews := floor(random() * 15000)::int;
        prod_tags := tag_options[1 + floor(random() * array_length(tag_options, 1))::int] || ',' ||
                     tag_options[1 + floor(random() * array_length(tag_options, 1))::int] || ',' ||
                     lower(cat);

        INSERT INTO products (name, description, category, brand, price, rating, review_count, image_url, in_stock, tags)
        VALUES (prod_name, prod_desc, cat, brand, prod_price, prod_rating, prod_reviews, '/img/gen-' || i || '.jpg', random() > 0.1, prod_tags);
    END LOOP;
END $$;

-- Drop the index on name to make LIKE queries slower (realistic for demo)
DROP INDEX IF EXISTS idx_products_name;

-- Add some complex views for heavier queries
CREATE OR REPLACE VIEW product_analytics AS
SELECT 
    p.category,
    p.brand,
    COUNT(*) as product_count,
    AVG(p.price) as avg_price,
    AVG(p.rating) as avg_rating,
    SUM(p.review_count) as total_reviews,
    COUNT(*) FILTER (WHERE p.in_stock) as in_stock_count
FROM products p
GROUP BY p.category, p.brand;

ANALYZE products;
ANALYZE product_relations;
ANALYZE user_preferences;
