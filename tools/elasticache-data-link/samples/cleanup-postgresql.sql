-- Deletes the ECDL sample tables, and only those. Delete the link first
-- (ecdl destroy --link ecdlsample, and drop the replication slot it prints),
-- then run this in the same test database and schema.
DROP TABLE IF EXISTS sample_orders, sample_products, sample_customers;
-- The link's signalling table, if you created it for the sample link:
DROP TABLE IF EXISTS ecdl_signal_ecdlsample;
