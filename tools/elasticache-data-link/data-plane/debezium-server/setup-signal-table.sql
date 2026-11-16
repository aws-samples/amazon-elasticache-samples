-- ═══════════════════════════════════════════════════════════════════
-- ECDL v3 — One-Time Aurora Setup
-- ═══════════════════════════════════════════════════════════════════
-- This creates ONLY the signal table in Aurora.
-- The signal table allows adding new tables to CDC without restarting Debezium.
--
-- Debezium offsets and schema history are stored in Valkey (not Aurora).
-- No other metadata tables are needed in the customer's source database.
-- ═══════════════════════════════════════════════════════════════════

-- Signal table: allows day-2 operations (add tables, trigger snapshots)
-- without restarting Debezium Server
CREATE TABLE IF NOT EXISTS debezium_signal (
    id VARCHAR(42) NOT NULL,
    type VARCHAR(32) NOT NULL,
    data VARCHAR(2048) NULL,
    PRIMARY KEY (id)
) ENGINE=InnoDB;

-- ═══════════════════════════════════════════════════════════════════
-- USAGE EXAMPLES:
-- ═══════════════════════════════════════════════════════════════════

-- Add a new table to CDC (incremental snapshot, no full restart):
-- INSERT INTO debezium_signal (id, type, data)
-- VALUES ('snap-orders', 'execute-snapshot',
--   '{"data-collections": ["Inventory.orders"], "type": "incremental"}');

-- Add multiple tables at once:
-- INSERT INTO debezium_signal (id, type, data)
-- VALUES ('snap-multi', 'execute-snapshot',
--   '{"data-collections": ["Inventory.orders","Inventory.products"], "type": "incremental"}');

-- Stop an in-progress incremental snapshot:
-- INSERT INTO debezium_signal (id, type, data)
-- VALUES ('stop-snap', 'stop-snapshot', '{"data-collections": ["Inventory.orders"]}');
