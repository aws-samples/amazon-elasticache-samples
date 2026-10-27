-- ShopMind Demo Database Schema
DROP TABLE IF EXISTS product_relations CASCADE;
DROP TABLE IF EXISTS user_preferences CASCADE;
DROP TABLE IF EXISTS products CASCADE;

CREATE TABLE products (
    id BIGSERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    category VARCHAR(100) NOT NULL,
    brand VARCHAR(100),
    price DECIMAL(10, 2) NOT NULL,
    rating DOUBLE PRECISION DEFAULT 0,
    review_count INTEGER DEFAULT 0,
    image_url VARCHAR(500),
    in_stock BOOLEAN DEFAULT true,
    tags TEXT DEFAULT ''
);

CREATE TABLE user_preferences (
    user_id BIGINT PRIMARY KEY,
    preferred_categories TEXT NOT NULL
);

CREATE TABLE product_relations (
    id BIGSERIAL PRIMARY KEY,
    category VARCHAR(100),
    related_product_id BIGINT REFERENCES products(id)
);

CREATE INDEX idx_products_category ON products(category);
CREATE INDEX idx_products_brand ON products(brand);
CREATE INDEX idx_products_name ON products(LOWER(name));
CREATE INDEX idx_product_relations_category ON product_relations(category);

-- Laptops (1-10)
INSERT INTO products (name, description, category, brand, price, rating, review_count, image_url, in_stock, tags) VALUES
('ASUS ROG Strix G16', '16" Gaming Laptop, Intel i9, RTX 4070, 32GB RAM, 1TB SSD', 'Laptops', 'ASUS', 1599.99, 4.7, 2341, '/img/asus-rog.jpg', true, 'gaming,laptop,high-performance'),
('Razer Blade 15', '15.6" QHD 240Hz, Intel i7, RTX 4060, 16GB RAM', 'Laptops', 'Razer', 1499.99, 4.5, 1876, '/img/razer.jpg', true, 'gaming,laptop,portable'),
('MSI Stealth 16', '16" FHD+, Intel i7, RTX 4070, 32GB RAM, 1TB SSD', 'Laptops', 'MSI', 1399.99, 4.6, 1543, '/img/msi.jpg', true, 'gaming,laptop,thin'),
('MacBook Pro 16"', 'M3 Max, 36GB RAM, 1TB SSD, Space Black', 'Laptops', 'Apple', 2499.99, 4.9, 5621, '/img/mbp.jpg', true, 'productivity,laptop,creative'),
('Dell XPS 15', '15.6" OLED, Intel i7, 32GB RAM, 1TB SSD', 'Laptops', 'Dell', 1299.99, 4.4, 3210, '/img/xps.jpg', true, 'productivity,laptop,business'),
('Lenovo ThinkPad X1 Carbon', '14" 2.8K OLED, Intel i7, 32GB RAM, 512GB SSD', 'Laptops', 'Lenovo', 1649.99, 4.6, 4102, '/img/thinkpad.jpg', true, 'business,laptop,ultralight'),
('HP Spectre x360', '16" 3K OLED, Intel i7, 16GB RAM, 1TB SSD, 2-in-1', 'Laptops', 'HP', 1399.99, 4.3, 2876, '/img/spectre.jpg', true, 'convertible,laptop,creative'),
('ASUS ZenBook 14', '14" OLED, AMD Ryzen 7, 16GB RAM, 512GB SSD', 'Laptops', 'ASUS', 899.99, 4.5, 3654, '/img/zenbook.jpg', true, 'ultrabook,laptop,portable'),
('Acer Predator Helios 18', '18" QHD+ 165Hz, Intel i9, RTX 4080, 32GB RAM', 'Laptops', 'Acer', 2199.99, 4.4, 987, '/img/predator.jpg', true, 'gaming,laptop,desktop-replacement'),
('MacBook Air 15"', 'M3, 16GB RAM, 512GB SSD, Midnight', 'Laptops', 'Apple', 1299.99, 4.8, 8932, '/img/mba.jpg', true, 'ultrabook,laptop,portable');

-- Monitors (11-18)
INSERT INTO products (name, description, category, brand, price, rating, review_count, image_url, in_stock, tags) VALUES
('Samsung Odyssey G9', '49" DQHD Curved Gaming Monitor, 240Hz', 'Monitors', 'Samsung', 1299.99, 4.5, 2187, '/img/g9.jpg', true, 'gaming,monitor,ultrawide'),
('LG UltraGear 27GP950', '27" 4K Nano IPS, 160Hz, HDMI 2.1', 'Monitors', 'LG', 799.99, 4.7, 3456, '/img/lg27.jpg', true, 'gaming,monitor,4k'),
('Dell UltraSharp U2723QE', '27" 4K IPS Black, USB-C Hub', 'Monitors', 'Dell', 619.99, 4.6, 5432, '/img/ultrasharp.jpg', true, 'productivity,monitor,color-accurate'),
('ASUS ProArt PA32UCG', '32" Mini LED 4K, 120Hz, Thunderbolt 3', 'Monitors', 'ASUS', 2999.99, 4.8, 876, '/img/proart.jpg', true, 'creative,monitor,hdr'),
('BenQ PD2725U', '27" 4K IPS, Thunderbolt 3, HDR400', 'Monitors', 'BenQ', 749.99, 4.5, 2341, '/img/benq.jpg', true, 'design,monitor,mac-compatible'),
('Alienware AW3423DWF', '34" QD-OLED Curved, 165Hz, 0.1ms', 'Monitors', 'Alienware', 1099.99, 4.7, 4567, '/img/alienware.jpg', true, 'gaming,monitor,oled'),
('LG DualUp 28MQ780', '28" SDQHD 16:18 Nano IPS, Ergo Stand', 'Monitors', 'LG', 699.99, 4.3, 1234, '/img/dualup.jpg', true, 'productivity,monitor,unique'),
('Samsung ViewFinity S9', '27" 5K IPS, Matte Display', 'Monitors', 'Samsung', 1299.99, 4.4, 987, '/img/viewfinity.jpg', true, 'creative,monitor,5k');

-- Accessories (19-28)
INSERT INTO products (name, description, category, brand, price, rating, review_count, image_url, in_stock, tags) VALUES
('Logitech G Pro X Superlight', 'Wireless Gaming Mouse, 25K DPI, 63g', 'Accessories', 'Logitech', 159.99, 4.8, 8932, '/img/gpro.jpg', true, 'gaming,mouse,wireless'),
('SteelSeries Apex Pro', 'Mechanical Gaming Keyboard, OmniPoint', 'Accessories', 'SteelSeries', 199.99, 4.6, 4521, '/img/apex.jpg', true, 'gaming,keyboard,mechanical'),
('Razer DeathAdder V3 Pro', 'Wireless Ergonomic Gaming Mouse, 30K DPI', 'Accessories', 'Razer', 149.99, 4.7, 6789, '/img/deathadder.jpg', true, 'gaming,mouse,ergonomic'),
('Keychron Q1 Pro', '75% Wireless Mechanical Keyboard, QMK/VIA', 'Accessories', 'Keychron', 199.99, 4.6, 3456, '/img/keychron.jpg', true, 'mechanical,keyboard,wireless'),
('Logitech MX Master 3S', 'Wireless Productivity Mouse, 8K DPI', 'Accessories', 'Logitech', 99.99, 4.8, 12543, '/img/mxmaster.jpg', true, 'productivity,mouse,ergonomic'),
('Elgato Stream Deck MK.2', '15 LCD Keys, Customizable Controller', 'Accessories', 'Elgato', 149.99, 4.7, 9876, '/img/streamdeck.jpg', true, 'streaming,controller,productivity'),
('Apple Magic Keyboard', 'Wireless with Touch ID, Numeric Keypad', 'Accessories', 'Apple', 199.99, 4.4, 7654, '/img/magickb.jpg', true, 'productivity,keyboard,wireless'),
('Razer Huntsman V3 Pro', 'Analog Optical Gaming Keyboard', 'Accessories', 'Razer', 249.99, 4.5, 2345, '/img/huntsman.jpg', true, 'gaming,keyboard,optical'),
('Logitech G915 X', 'Low-Profile Wireless Mechanical Keyboard', 'Accessories', 'Logitech', 229.99, 4.6, 5678, '/img/g915.jpg', true, 'gaming,keyboard,low-profile'),
('SteelSeries Aerox 5', 'Wireless Gaming Mouse, 18K DPI, 74g', 'Accessories', 'SteelSeries', 139.99, 4.4, 3210, '/img/aerox.jpg', true, 'gaming,mouse,lightweight');

-- Audio (29-36)
INSERT INTO products (name, description, category, brand, price, rating, review_count, image_url, in_stock, tags) VALUES
('Sony WH-1000XM5', 'Wireless Noise Cancelling Headphones, 30hr', 'Audio', 'Sony', 349.99, 4.8, 12543, '/img/xm5.jpg', true, 'headphones,wireless,noise-cancelling'),
('Apple AirPods Pro 2', 'Active Noise Cancellation, USB-C', 'Audio', 'Apple', 249.99, 4.7, 23456, '/img/airpods.jpg', true, 'earbuds,wireless,noise-cancelling'),
('HyperX Cloud III', 'Wired Gaming Headset, DTS Spatial Audio', 'Audio', 'HyperX', 99.99, 4.5, 4321, '/img/hyperx.jpg', true, 'headset,gaming,wired'),
('SteelSeries Arctis Nova Pro', 'Wireless Gaming Headset, Active ANC', 'Audio', 'SteelSeries', 349.99, 4.6, 3456, '/img/arctis.jpg', true, 'headset,gaming,wireless'),
('Bose QuietComfort Ultra', 'Wireless Noise Cancelling, Spatial Audio', 'Audio', 'Bose', 429.99, 4.7, 8765, '/img/bose.jpg', true, 'headphones,wireless,premium'),
('Sennheiser HD 660S2', 'Open-Back Audiophile Headphones, 300 Ohm', 'Audio', 'Sennheiser', 499.99, 4.8, 2345, '/img/hd660.jpg', true, 'headphones,audiophile,open-back'),
('JBL Quantum 910', 'Wireless Gaming Headset, Head Tracking', 'Audio', 'JBL', 249.99, 4.3, 1876, '/img/jbl.jpg', true, 'headset,gaming,wireless'),
('Shure MV7+', 'Dynamic USB/XLR Podcast Microphone', 'Audio', 'Shure', 269.99, 4.7, 5432, '/img/shure.jpg', true, 'microphone,podcast,streaming');

-- Components (37-44)
INSERT INTO products (name, description, category, brand, price, rating, review_count, image_url, in_stock, tags) VALUES
('Corsair Vengeance DDR5', '32GB (2x16GB) DDR5-6000, CL36', 'Components', 'Corsair', 129.99, 4.6, 5678, '/img/corsair-ram.jpg', true, 'ram,ddr5,memory'),
('Samsung 990 Pro', '2TB NVMe M.2 SSD, PCIe 4.0, 7450MB/s', 'Components', 'Samsung', 179.99, 4.8, 7890, '/img/990pro.jpg', true, 'ssd,storage,nvme'),
('NVIDIA RTX 4070 Ti Super', '16GB GDDR6X, DLSS 3, Ray Tracing', 'Components', 'NVIDIA', 799.99, 4.7, 3456, '/img/rtx4070.jpg', true, 'gpu,graphics-card,gaming'),
('AMD Ryzen 9 7950X3D', '16-Core, 5.7GHz Boost, 3D V-Cache', 'Components', 'AMD', 549.99, 4.9, 4567, '/img/ryzen.jpg', true, 'cpu,processor,gaming'),
('Corsair RM1000x', '1000W 80+ Gold Fully Modular PSU', 'Components', 'Corsair', 189.99, 4.8, 6543, '/img/psu.jpg', true, 'psu,power-supply,modular'),
('NZXT Kraken X73', '360mm AIO Liquid CPU Cooler, LCD', 'Components', 'NZXT', 249.99, 4.5, 3210, '/img/kraken.jpg', true, 'cooler,aio,liquid-cooling'),
('WD Black SN850X', '4TB NVMe M.2 SSD, PCIe 4.0, 7300MB/s', 'Components', 'Western Digital', 299.99, 4.7, 2345, '/img/sn850x.jpg', true, 'ssd,storage,high-capacity'),
('G.Skill Trident Z5 RGB', '64GB (2x32GB) DDR5-6400, CL32', 'Components', 'G.Skill', 249.99, 4.6, 1987, '/img/trident.jpg', true, 'ram,ddr5,rgb');

-- Networking (45-50)
INSERT INTO products (name, description, category, brand, price, rating, review_count, image_url, in_stock, tags) VALUES
('ASUS ROG Rapture GT-AXE16000', 'Quad-Band WiFi 6E Gaming Router', 'Networking', 'ASUS', 549.99, 4.5, 1234, '/img/rog-router.jpg', true, 'router,wifi6e,gaming'),
('TP-Link Deco XE75', 'WiFi 6E Mesh System, 3-Pack', 'Networking', 'TP-Link', 299.99, 4.6, 5678, '/img/deco.jpg', true, 'mesh,wifi6e,whole-home'),
('Netgear Nighthawk RAXE500', 'Tri-Band WiFi 6E Router, 10.8Gbps', 'Networking', 'Netgear', 449.99, 4.3, 2345, '/img/nighthawk.jpg', true, 'router,wifi6e,high-speed'),
('Ubiquiti UniFi U7 Pro', 'WiFi 7 Access Point, 6 GHz, PoE+', 'Networking', 'Ubiquiti', 189.99, 4.7, 3456, '/img/unifi.jpg', true, 'access-point,wifi7,enterprise'),
('ASUS ExpertWiFi EBM68', 'WiFi 6 Mesh System, Business Grade', 'Networking', 'ASUS', 349.99, 4.4, 876, '/img/expertwifi.jpg', true, 'mesh,wifi6,business'),
('Synology RT6600ax', 'Tri-Band WiFi 6 Router, Quad-Core', 'Networking', 'Synology', 329.99, 4.5, 1543, '/img/synology-rt.jpg', true, 'router,wifi6,nas-integration');

-- Furniture (51-55)
INSERT INTO products (name, description, category, brand, price, rating, review_count, image_url, in_stock, tags) VALUES
('Secretlab Titan Evo', 'Ergonomic Gaming Chair, Magnetic Headrest', 'Furniture', 'Secretlab', 519.99, 4.7, 6543, '/img/secretlab.jpg', true, 'chair,gaming,ergonomic'),
('Herman Miller Aeron', 'Ergonomic Office Chair, PostureFit SL', 'Furniture', 'Herman Miller', 1395.00, 4.8, 9876, '/img/aeron.jpg', true, 'chair,office,premium'),
('Uplift V2 Standing Desk', '60x30" Bamboo Top, Electric Adjustable', 'Furniture', 'Uplift', 699.99, 4.6, 4567, '/img/uplift.jpg', true, 'desk,standing,electric'),
('Autonomous SmartDesk Pro', '53x29" Standing Desk, Dual Motor', 'Furniture', 'Autonomous', 529.99, 4.4, 3210, '/img/smartdesk.jpg', true, 'desk,standing,affordable'),
('Steelcase Leap V2', 'Ergonomic Office Chair, LiveBack', 'Furniture', 'Steelcase', 1189.00, 4.7, 5432, '/img/leap.jpg', true, 'chair,office,ergonomic');

-- Storage (56-60)
INSERT INTO products (name, description, category, brand, price, rating, review_count, image_url, in_stock, tags) VALUES
('Synology DS923+', '4-Bay NAS, AMD Ryzen R1600, 4GB RAM', 'Storage', 'Synology', 549.99, 4.7, 3456, '/img/ds923.jpg', true, 'nas,storage,backup'),
('QNAP TS-464', '4-Bay NAS, Intel N5095, 8GB RAM', 'Storage', 'QNAP', 499.99, 4.5, 2345, '/img/qnap.jpg', true, 'nas,storage,multimedia'),
('Samsung T7 Shield', '4TB Portable SSD, USB 3.2, IP65', 'Storage', 'Samsung', 299.99, 4.7, 8765, '/img/t7.jpg', true, 'portable-ssd,storage,rugged'),
('WD My Cloud EX2 Ultra', '2-Bay Personal Cloud NAS, 16TB', 'Storage', 'Western Digital', 449.99, 4.3, 4321, '/img/mycloud.jpg', true, 'nas,cloud,personal'),
('SanDisk Extreme Pro', '2TB Portable SSD, USB 3.2, 2000MB/s', 'Storage', 'SanDisk', 179.99, 4.6, 6789, '/img/sandisk.jpg', true, 'portable-ssd,storage,fast');

-- User Preferences
INSERT INTO user_preferences (user_id, preferred_categories) VALUES
(1, 'Laptops,Accessories,Monitors'),
(2, 'Audio,Components,Storage'),
(3, 'Laptops,Furniture,Accessories'),
(4, 'Monitors,Components,Networking'),
(5, 'Audio,Accessories,Furniture');

-- Product Relations (complementary recommendations)
INSERT INTO product_relations (category, related_product_id) VALUES
('Laptops', 19), ('Laptops', 20), ('Laptops', 23), ('Laptops', 11), ('Laptops', 12), ('Laptops', 29), ('Laptops', 51),
('Monitors', 19), ('Monitors', 20), ('Monitors', 22), ('Monitors', 53), ('Monitors', 51),
('Accessories', 12), ('Accessories', 16), ('Accessories', 31), ('Accessories', 32), ('Accessories', 53),
('Audio', 24), ('Audio', 22), ('Audio', 23), ('Audio', 53), ('Audio', 52),
('Components', 1), ('Components', 9), ('Components', 11), ('Components', 16), ('Components', 42),
('Networking', 56), ('Networking', 57), ('Networking', 38), ('Networking', 43),
('Furniture', 13), ('Furniture', 17), ('Furniture', 23), ('Furniture', 25), ('Furniture', 29),
('Storage', 45), ('Storage', 46), ('Storage', 38), ('Storage', 44), ('Storage', 37);
