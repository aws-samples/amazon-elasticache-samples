package com.amazon.shopmind.agent;

import java.util.List;

public class AgentResponse {
    private final String text;
    private final List<AgentToolCall> toolCalls;
    private final long totalTimeMs;     // Total agent loop time
    private final long bedrockTimeMs;   // Time in Bedrock API calls
    private final long toolTimeMs;      // Time in tool executions (JDBC queries)

    public AgentResponse(String text, List<AgentToolCall> toolCalls,
                         long totalTimeMs, long bedrockTimeMs, long toolTimeMs) {
        this.text = text;
        this.toolCalls = toolCalls;
        this.totalTimeMs = totalTimeMs;
        this.bedrockTimeMs = bedrockTimeMs;
        this.toolTimeMs = toolTimeMs;
    }

    public String getText() { return text; }
    public List<AgentToolCall> getToolCalls() { return toolCalls; }
    public long getTotalTimeMs() { return totalTimeMs; }
    public long getBedrockTimeMs() { return bedrockTimeMs; }
    public long getToolTimeMs() { return toolTimeMs; }
}
