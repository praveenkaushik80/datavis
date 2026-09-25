# Sample "shop" client database with its seed script baked in (for hosts without bind mounts).
FROM postgres:17-alpine
COPY 01-shop.sql /docker-entrypoint-initdb.d/01-shop.sql
# On a mounted volume the mount root contains lost+found; keep the cluster in a subfolder.
ENV PGDATA=/var/lib/postgresql/data/pgdata
