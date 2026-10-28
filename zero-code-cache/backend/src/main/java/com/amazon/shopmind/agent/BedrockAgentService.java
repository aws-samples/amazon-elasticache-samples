package com.amazon.shopmind.agent;

import com.amazon.shopmind.metrics.MetricsTracker;
import com.amazon.shopmind.repository.ProductRepository;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;
import software.amazon.awssdk.auth.credentials.DefaultCredentialsProvider;
import software.amazon.awssdk.core.document.Document;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.bedrockruntime.BedrockRuntimeClient;
import software.amazon.awssdk.services.bedrockruntime.model.*;

import java.util.*;

@Service
public class BedrockAgentService {

    private final ProductRepository productRepository;
    private final MetricsTracker metricsTracker;
    private final BedrockRuntimeClient bedrockClient;
    private final ObjectMapper objectMapper = new ObjectMapper();

    @Value("${bedrock.model-id:us.anthropic.claude-haiku-4-5-20251001-v1:0}")
    private String modelId;

    private static final String SYSTEM_PROMPT = """
            You are ShopMind, an intelligent shopping assistant for an electronics store.
            You help customers find products by searching the catalog and browsing categories.

            ALWAYS use your tools to look up products. Never make up product information.

            TOOL USAGE RULES:
            - For every query, ALWAYS call search_products with the user's exact query first
            - Then ALWAYS call browse_category with the most relevant category
            - Do NOT call the same tool twice with different parameters
            - Do NOT rephrase or modify the user's search query
            - Use max_results=10 for search_products

            FORMATTING RULES:
            - Do NOT use markdown formatting (no **, no ##, no bullet points with *)
            - Use plain text only
            - List products with their name, price, rating, and a brief note
            - Keep responses concise
            """;

    public BedrockAgentService(
            ProductRepository productRepository,
            MetricsTracker metricsTracker,
            @Value("${bedrock.region:us-east-1}") String region) {
        this.productRepository = productRepository;
        this.metricsTracker = metricsTracker;
        this.bedrockClient = BedrockRuntimeClient.builder()
                .region(Region.of(region))
                .credentialsProvider(DefaultCredentialsProvider.create())
                .build();
    }

    public AgentResponse chat(String userMessage) {
        long totalStart = System.nanoTime();
        long bedrockTimeNs = 0;
        long toolTimeNs = 0;

        List<AgentToolCall> toolCalls = new ArrayList<>();
        List<Message> messages = new ArrayList<>();

        messages.add(Message.builder()
                .role(ConversationRole.USER)
                .content(ContentBlock.fromText(userMessage))
                .build());

        int maxIterations = 10;
        String finalText = "";

        for (int i = 0; i < maxIterations; i++) {
            long bedrockStart = System.nanoTime();
            ConverseResponse response = bedrockClient.converse(ConverseRequest.builder()
                    .modelId(modelId)
                    .system(SystemContentBlock.fromText(SYSTEM_PROMPT))
                    .messages(messages)
                    .toolConfig(buildToolConfig())
                    .build());
            bedrockTimeNs += (System.nanoTime() - bedrockStart);

            Message assistantMessage = response.output().message();
            messages.add(assistantMessage);

            if (response.stopReason() == StopReason.TOOL_USE) {
                List<ContentBlock> toolResults = new ArrayList<>();

                for (ContentBlock block : assistantMessage.content()) {
                    if (block.toolUse() != null) {
                        ToolUseBlock toolUse = block.toolUse();
                        String toolName = toolUse.name();
                        Map<String, Object> input = documentToMap(toolUse.input());

                        String result = executeTool(toolName, input);

                        // Sum actual JDBC query times from the metrics (matches the feed)
                        long toolDurationMs = metricsTracker.getLastQueryDurationMs();
                        toolTimeNs += toolDurationMs * 1_000_000;

                        toolCalls.add(new AgentToolCall(toolName, input, toolDurationMs));

                        toolResults.add(ContentBlock.fromToolResult(ToolResultBlock.builder()
                                .toolUseId(toolUse.toolUseId())
                                .content(ToolResultContentBlock.fromText(result))
                                .build()));
                    }
                }

                messages.add(Message.builder()
                        .role(ConversationRole.USER)
                        .content(toolResults)
                        .build());
            } else {
                for (ContentBlock block : assistantMessage.content()) {
                    if (block.text() != null) {
                        finalText = block.text();
                    }
                }
                break;
            }
        }

        long totalMs = (System.nanoTime() - totalStart) / 1_000_000;
        long bedrockMs = bedrockTimeNs / 1_000_000;
        long toolMs = toolTimeNs / 1_000_000;

        return new AgentResponse(finalText, toolCalls, totalMs, bedrockMs, toolMs);
    }

    private Map<String, Object> documentToMap(Document doc) {
        Map<String, Object> result = new LinkedHashMap<>();
        if (doc != null && doc.isMap()) {
            for (var entry : doc.asMap().entrySet()) {
                Document val = entry.getValue();
                if (val.isString()) result.put(entry.getKey(), val.asString());
                else if (val.isNumber()) result.put(entry.getKey(), val.asNumber());
                else if (val.isBoolean()) result.put(entry.getKey(), val.asBoolean());
                else result.put(entry.getKey(), val.toString());
            }
        }
        return result;
    }

    private String executeTool(String toolName, Map<String, Object> input) {
        try {
            return switch (toolName) {
                case "search_products" -> {
                    String query = String.valueOf(input.get("query"));
                    int maxResults = input.containsKey("max_results")
                            ? ((Number) input.get("max_results")).intValue() : 10;
                    yield objectMapper.writeValueAsString(
                            productRepository.searchProducts(query).stream()
                                    .limit(maxResults).toList());
                }
                case "browse_category" -> {
                    String category = String.valueOf(input.get("category"));
                    yield objectMapper.writeValueAsString(productRepository.getByCategory(category));
                }
                case "get_product_details" -> {
                    long productId = ((Number) input.get("product_id")).longValue();
                    var product = productRepository.getById(productId);
                    yield product != null ? objectMapper.writeValueAsString(product) : "{\"error\":\"Not found\"}";
                }
                case "get_recommendations" -> {
                    String category = String.valueOf(input.getOrDefault("category", "Electronics"));
                    long userId = input.containsKey("user_id") ? ((Number) input.get("user_id")).longValue() : 1L;
                    yield objectMapper.writeValueAsString(productRepository.getRecommendations(userId, category));
                }
                case "get_categories" -> objectMapper.writeValueAsString(productRepository.getCategories());
                default -> "{\"error\":\"Unknown tool: " + toolName + "\"}";
            };
        } catch (Exception e) {
            return "{\"error\":\"" + e.getMessage() + "\"}";
        }
    }

    private ToolConfiguration buildToolConfig() {
        return ToolConfiguration.builder()
                .tools(
                        tool("search_products",
                                "Search the product catalog by keyword. Searches names, categories, brands, tags.",
                                Map.of("query", propDoc("string", "Search query"),
                                       "max_results", propDoc("integer", "Max results (default 10)")),
                                List.of("query")),
                        tool("browse_category",
                                "Browse all products in a category: Laptops, Monitors, Accessories, Audio, Components, Networking, Furniture, Storage.",
                                Map.of("category", propDoc("string", "Category name")),
                                List.of("category")),
                        tool("get_product_details",
                                "Get detailed info about a product by ID.",
                                Map.of("product_id", propDoc("integer", "Product ID")),
                                List.of("product_id")),
                        tool("get_recommendations",
                                "Get personalized recommendations. Fires multiple DB queries internally.",
                                Map.of("category", propDoc("string", "Category context"),
                                       "user_id", propDoc("integer", "User ID (default 1)")),
                                List.of("category")),
                        tool("get_categories",
                                "List all product categories.",
                                Map.of(), List.of())
                )
                .build();
    }

    private Tool tool(String name, String description, Map<String, Document> properties, List<String> required) {
        Map<String, Document> schema = new LinkedHashMap<>();
        schema.put("type", Document.fromString("object"));
        schema.put("properties", Document.fromMap(properties));
        if (!required.isEmpty()) {
            schema.put("required", Document.fromList(required.stream().map(Document::fromString).toList()));
        }

        return Tool.fromToolSpec(ToolSpecification.builder()
                .name(name)
                .description(description)
                .inputSchema(ToolInputSchema.fromJson(Document.fromMap(schema)))
                .build());
    }

    private Document propDoc(String type, String description) {
        return Document.fromMap(Map.of(
                "type", Document.fromString(type),
                "description", Document.fromString(description)));
    }
}
