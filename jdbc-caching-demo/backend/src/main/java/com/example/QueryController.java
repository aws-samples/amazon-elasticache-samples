package com.example;

import com.example.model.QueryResult;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.sql.SQLException;
import java.util.Map;

@RestController
@RequestMapping("/api")
@CrossOrigin(origins = {"http://localhost:5173", "http://localhost:3000"})
public class QueryController {

    private final QueryService queryService;

    public QueryController(QueryService queryService) {
        this.queryService = queryService;
    }

    @PostMapping("/connections/connect")
    public ResponseEntity<?> connect() {
        return ResponseEntity.ok(queryService.connect());
    }

    @PostMapping("/connections/disconnect")
    public ResponseEntity<?> disconnect() {
        return ResponseEntity.ok(queryService.disconnect());
    }

    @GetMapping("/connections/status")
    public ResponseEntity<?> connectionStatus() {
        return ResponseEntity.ok(queryService.status());
    }

    @GetMapping("/query/direct")
    public ResponseEntity<?> queryDirect(
            @RequestParam(defaultValue = "false") boolean includePlan,
            @RequestParam(required = false) String sql) {
        return runQuery(false, includePlan, sql, null);
    }

    @GetMapping("/query/cached")
    public ResponseEntity<?> queryCached(
            @RequestParam(defaultValue = "false") boolean includePlan,
            @RequestParam(required = false) String sql,
            @RequestParam(defaultValue = "300s") String ttl) {
        return runQuery(true, includePlan, sql, ttl);
    }

    @GetMapping("/query/info")
    public ResponseEntity<?> queryInfo() {
        return ResponseEntity.ok(Map.of(
            "query", QueryService.PRODUCT_CATALOG_QUERY,
            "cachedQuery", "/* CACHE_PARAM(ttl=300s) */ " + QueryService.PRODUCT_CATALOG_QUERY
        ));
    }

    private ResponseEntity<?> runQuery(boolean useCache, boolean includePlan, String sql, String ttl) {
        try {
            QueryResult result = queryService.execute(useCache, includePlan, sql, ttl);
            return ResponseEntity.ok(result);
        } catch (SQLException e) {
            return ResponseEntity.internalServerError()
                .body(Map.of("error", e.getMessage(), "mode", useCache ? "cached" : "direct"));
        }
    }
}
