#!/bin/sh
# Creates the database roles and both databases. Runs once, on an empty data directory.
#   procuremind_owner  owns the schema; used only for migrations and loading demo data
#   procuremind_app    API and workers; not a table owner, so row-level security always applies
set -e
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" <<SQL
CREATE ROLE procuremind_owner LOGIN PASSWORD '${PROCUREMIND_OWNER_PASSWORD:-procuremind_owner}';
CREATE ROLE procuremind_app   LOGIN PASSWORD '${PROCUREMIND_APP_PASSWORD:-procuremind_app}';
CREATE DATABASE procuremind      OWNER procuremind_owner;
CREATE DATABASE procuremind_test OWNER procuremind_owner;
SQL
for db in procuremind procuremind_test; do
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$db" <<SQL
CREATE EXTENSION IF NOT EXISTS pg_trgm;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO procuremind_app;
ALTER DEFAULT PRIVILEGES FOR ROLE procuremind_owner IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO procuremind_app;
ALTER DEFAULT PRIVILEGES FOR ROLE procuremind_owner IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO procuremind_app;
SQL
done
