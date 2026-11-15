#!/bin/sh
# Creates the separate database used for scale measurement, so the demo database (and the evaluation that
# depends on its exact contents) is never touched. Run from the repository root with the stack up:
#   sh backend/load/create_load_db.sh && cd backend && DATABASE_URL_OWNER=...procuremind_load uv run alembic upgrade head
set -e
docker compose exec -T db sh -c 'psql -v ON_ERROR_STOP=1 -U "${POSTGRES_USER:-postgres}"' <<SQL
CREATE DATABASE procuremind_load OWNER procuremind_owner;
SQL
docker compose exec -T db sh -c 'psql -v ON_ERROR_STOP=1 -U "${POSTGRES_USER:-postgres}" -d procuremind_load' <<SQL
CREATE EXTENSION IF NOT EXISTS pg_trgm;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO procuremind_app;
ALTER DEFAULT PRIVILEGES FOR ROLE procuremind_owner IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO procuremind_app;
ALTER DEFAULT PRIVILEGES FOR ROLE procuremind_owner IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO procuremind_app;
SQL
