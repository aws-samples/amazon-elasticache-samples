package com.amazon.shopmind.agent;

import java.util.Map;

public class AgentToolCall {
    private final String toolName;
    private final Map<String, Object> input;
    private final long durationMs;

    public AgentToolCall(String toolName, Map<String, Object> input, long durationMs) {
        this.toolName = toolName;
        this.input = input;
        this.durationMs = durationMs;
    }

    public String getToolName() { return toolName; }
    public Map<String, Object> getInput() { return input; }
    public long getDurationMs() { return durationMs; }
}
