-- Create and populate cinemas table for cache demo
-- Usage: psql -h localhost -p 5432 -U dbadmin -d testdb -v rows=10000 -f setup_cinemas_table.sql

-- Drop table if exists
DROP TABLE IF EXISTS cinemas;

-- Create cinemas table
CREATE TABLE cinemas (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    location VARCHAR(100) NOT NULL,
    capacity INTEGER NOT NULL
);

-- Generate sample data (default 10,000 rows)
DO $$
DECLARE
    row_count INTEGER := 10000;
    cinema_names TEXT[] := ARRAY['Grand Cinema', 'Star Theater', 'Movie Palace', 'Cinema City', 'Film House', 'Regal Cinemas', 'AMC Theater', 'Cineplex', 'IMAX Center', 'Drive-In'];
    locations TEXT[] := ARRAY['Downtown', 'Westside', 'Eastside', 'Northside', 'Southside', 'Uptown', 'Midtown', 'Suburbs', 'Mall', 'Airport'];
BEGIN
    -- Override with parameter if provided
    IF current_setting('my.rows', true) IS NOT NULL THEN
        row_count := current_setting('my.rows')::INTEGER;
    END IF;
    
    FOR i IN 1..row_count LOOP
        INSERT INTO cinemas (name, location, capacity)
        VALUES (
            cinema_names[1 + (i % array_length(cinema_names, 1))],
            locations[1 + (i % array_length(locations, 1))],
            200 + (i % 500)
        );
    END LOOP;
END $$;

-- Show summary
SELECT COUNT(*) as total_rows FROM cinemas;
SELECT * FROM cinemas LIMIT 5;
