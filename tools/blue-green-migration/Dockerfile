FROM ghcr.io/tair-opensource/redisshake:latest

# Labels
LABEL maintainer="your-team"
LABEL description="RedisShake - Redis data migration and transformation tool"

# Create config and data directories
RUN mkdir -p /opt/redisshake /opt/redisshake/data

# Copy the entrypoint script
COPY entrypoint.sh /opt/redisshake/entrypoint.sh
RUN chmod +x /opt/redisshake/entrypoint.sh

# Copy default config template
COPY shake.toml /opt/redisshake/shake.toml

# Expose status port for health checks
EXPOSE 16001

ENTRYPOINT ["/opt/redisshake/entrypoint.sh"]
