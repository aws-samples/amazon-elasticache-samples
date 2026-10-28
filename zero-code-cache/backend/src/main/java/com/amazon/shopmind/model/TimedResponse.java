package com.amazon.shopmind.model;

/**
 * Wraps any API response with server-side timing breakdown.
 */
public class TimedResponse<T> {
    private T data;
    private long serverTimeMs;   // Total server processing time
    private long dbTimeMs;       // Time spent in JDBC queries
    private long agentTimeMs;    // Time spent in Bedrock agent (0 if no agent)

    public TimedResponse(T data, long serverTimeMs, long dbTimeMs, long agentTimeMs) {
        this.data = data;
        this.serverTimeMs = serverTimeMs;
        this.dbTimeMs = dbTimeMs;
        this.agentTimeMs = agentTimeMs;
    }

    public T getData() { return data; }
    public long getServerTimeMs() { return serverTimeMs; }
    public long getDbTimeMs() { return dbTimeMs; }
    public long getAgentTimeMs() { return agentTimeMs; }
}
