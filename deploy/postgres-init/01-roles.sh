#!/bin/bash
# Runs once, when the database volume is first created: the application roles
# the migration chain expects (ira_app, ira_worker, ira_migrator, ira_schema_owner).
set -eu
psql -v ON_ERROR_STOP=1 --username postgres --dbname "$POSTGRES_DB" <<SQL
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ira_schema_owner') THEN CREATE ROLE ira_schema_owner NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ira_migrator') THEN CREATE ROLE ira_migrator LOGIN PASSWORD '${IRA_MIGRATOR_PASSWORD}' IN ROLE ira_schema_owner; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ira_app') THEN CREATE ROLE ira_app LOGIN PASSWORD '${IRA_APP_PASSWORD}'; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ira_worker') THEN CREATE ROLE ira_worker LOGIN PASSWORD '${IRA_APP_PASSWORD}'; END IF;
END \$\$;
GRANT ira_schema_owner TO ira_migrator;
ALTER DATABASE "$POSTGRES_DB" OWNER TO ira_schema_owner;
GRANT ALL ON SCHEMA public TO ira_schema_owner;
SQL
