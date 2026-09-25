# LiteLLM gateway with DataFusion's model list baked in (for hosts without bind mounts, e.g. Railway).
FROM ghcr.io/berriai/litellm:main-stable
COPY config.yaml /app/config.yaml
# "::" = IPv4 + IPv6, so the gateway is reachable over Railway's private network.
CMD ["--config", "/app/config.yaml", "--host", "::", "--port", "4000"]
