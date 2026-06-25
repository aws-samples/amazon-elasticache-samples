package com.example.model;

import java.util.List;
import java.util.Map;

public class QueryResult {
    private List<Map<String, Object>> rows;
    private List<String> columns;
    private long latencyMs;   // queryMs + fetchMs (connection excluded)
    private long queryMs;     // executeQuery() time — cache lookup or DB execution
    private long fetchMs;     // time to drain ResultSet rows
    private boolean cacheEnabled;
    private String mode; // "direct" or "cached"
    private String query;
    private String queryPlan;
    private int rowCount;
    private String cacheHint; // the /* CACHE_PARAM(ttl=...) */ hint actually sent

    public String getCacheHint() { return cacheHint; }
    public void setCacheHint(String cacheHint) { this.cacheHint = cacheHint; }

    public QueryResult() {}

    public List<Map<String, Object>> getRows() { return rows; }
    public void setRows(List<Map<String, Object>> rows) { this.rows = rows; }

    public List<String> getColumns() { return columns; }
    public void setColumns(List<String> columns) { this.columns = columns; }

    public long getLatencyMs() { return latencyMs; }
    public void setLatencyMs(long latencyMs) { this.latencyMs = latencyMs; }

    public long getQueryMs() { return queryMs; }
    public void setQueryMs(long queryMs) { this.queryMs = queryMs; }

    public long getFetchMs() { return fetchMs; }
    public void setFetchMs(long fetchMs) { this.fetchMs = fetchMs; }

    public boolean isCacheEnabled() { return cacheEnabled; }
    public void setCacheEnabled(boolean cacheEnabled) { this.cacheEnabled = cacheEnabled; }

    public String getMode() { return mode; }
    public void setMode(String mode) { this.mode = mode; }

    public String getQuery() { return query; }
    public void setQuery(String query) { this.query = query; }

    public String getQueryPlan() { return queryPlan; }
    public void setQueryPlan(String queryPlan) { this.queryPlan = queryPlan; }

    public int getRowCount() { return rowCount; }
    public void setRowCount(int rowCount) { this.rowCount = rowCount; }
}
