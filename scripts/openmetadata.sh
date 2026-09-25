#!/usr/bin/env bash
# Runs OpenMetadata 2.0.2 (server, Postgres, Elasticsearch, Airflow-based ingestion) next to DataFusion.
# The server and ingestion containers also join the "datafusion" network so the backend can reach
# OpenMetadata and the ingestion workers can reach your client databases.
#   ./scripts/openmetadata.sh up | down | logs
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION=2.0.2
DIR=openmetadata
FILE=$DIR/docker-compose-postgres.yml
mkdir -p $DIR
if [ ! -f $FILE ]; then
  curl -fsSL -o $FILE "https://github.com/open-metadata/OpenMetadata/releases/download/${VERSION}-release/docker-compose-postgres.yml"
fi
cat > $DIR/datafusion-network.override.yml <<'YML'
services:
  openmetadata-server:
    networks: [app_net, datafusion]
  ingestion:
    networks: [app_net, datafusion]
networks:
  datafusion:
    external: true
    name: datafusion
YML
cmd=${1:-up}
case $cmd in
  up)   docker compose -p openmetadata -f $FILE -f $DIR/datafusion-network.override.yml up -d
        echo "OpenMetadata: http://localhost:8585 (admin@open-metadata.org / admin)."
        echo "Then set OM_URL=http://openmetadata_server:8585 and OM_TOKEN (ingestion-bot token) in .env"
        echo "and run: docker compose up -d backend" ;;
  down) docker compose -p openmetadata -f $FILE -f $DIR/datafusion-network.override.yml down ;;
  logs) docker compose -p openmetadata -f $FILE -f $DIR/datafusion-network.override.yml logs -f ;;
  *)    echo "usage: $0 up|down|logs"; exit 1 ;;
esac
