INDEX_NAME = "idx:requests"
KEY_PREFIX_VECTOR = "request:vector:"
KEY_PREFIX_REQUEST_RESPONSE = "rr:"
VECTOR_DIM = 1024  # Titan Embed Text v2 supports 256, 512, 1024

# Bedrock pricing (per 1M tokens, as of Jan 2025)
NOVA_PREMIER_INPUT_COST = 2.50  # $2.50 per 1M input tokens
NOVA_PREMIER_OUTPUT_COST = 12.50  # $12.50 per 1M output tokens
