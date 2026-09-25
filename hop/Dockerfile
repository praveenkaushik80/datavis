# Hop Web with the DataFusion project baked in (for hosts without bind mounts, e.g. Railway).
# Set HOP_OPTIONS with -DDATAFUSION_API_URL, -DHOP_API_TOKEN and -DHOP_ADMIN_TOKEN (see README).
FROM apache/hop-web:2.19.0
COPY --chown=hop:hop datafusion /project
COPY --chown=hop:hop environments /environments
ENV HOP_PROJECT_NAME=datafusion \
    HOP_PROJECT_FOLDER=/project \
    HOP_ENVIRONMENT_NAME=railway \
    HOP_ENVIRONMENT_CONFIG_FILE_NAME_PATHS=/environments/datafusion-railway.json
EXPOSE 8080
