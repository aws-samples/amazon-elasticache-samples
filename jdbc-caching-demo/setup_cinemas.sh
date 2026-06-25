#!/bin/bash
# Setup cinemas table for DatabaseConnectionWithCacheExample
# Usage: ./setup_cinemas.sh [rows]
# Default: 10,000 rows

ROWS=${1:-10000}
PGPASSWORD=TempPassword12345 psql -h localhost -p 5432 -U dbadmin -d testdb -c "SET my.rows = $ROWS;" -f setup_cinemas_table.sql
