package com.amazon.shopmind.controller;

import com.amazon.shopmind.agent.AgentResponse;
import com.amazon.shopmind.agent.BedrockAgentService;
import org.springframework.web.bind.annotation.*;

import java.util.Map;

@RestController
@RequestMapping("/api/agent")
@CrossOrigin(origins = "*")
public class AgentController {

    private final BedrockAgentService agentService;

    public AgentController(BedrockAgentService agentService) {
        this.agentService = agentService;
    }

    @PostMapping("/chat")
    public AgentResponse chat(@RequestBody Map<String, String> body) {
        String message = body.getOrDefault("message", "");
        if (message.isBlank()) {
            return new AgentResponse("Please enter a search query or question.", java.util.List.of(), 0, 0, 0);
        }
        return agentService.chat(message);
    }
}
