#!/bin/sh
# Bind "::" (IPv4 + IPv6) when the host has IPv6, else 0.0.0.0. Railway's private network needs IPv6
# (legacy environments are IPv6-only); some Docker hosts have IPv6 disabled, where "::" would fail.
if [ -z "$HOST" ]; then
  if python3 -c "import socket; s=socket.socket(socket.AF_INET6); s.bind(('::', 0)); s.close()" 2>/dev/null; then
    HOST="::"
  else
    HOST="0.0.0.0"
  fi
fi
echo "DataFusion API listening on [$HOST]:${PORT:-8000}"
exec uvicorn app.main:app --host "$HOST" --port "${PORT:-8000}" --timeout-keep-alive 75
