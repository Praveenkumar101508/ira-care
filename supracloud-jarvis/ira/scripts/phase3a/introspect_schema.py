"""Phase 3A schema introspection — full metadata for every table.

No credentials or connection strings appear in the output; the DSN is read from
the environment and never written.
"""
import json, os, subprocess, sys

DSN = os.environ["PG3A_DSN"]

def truthy(v):
    """psql renders a boolean as t/f, but `boolean::text` renders true/false.
    Accept both — reading only one was a real bug caught by the manifest test."""
    return str(v).strip().lower() in ("t", "true", "y", "yes", "1")


def q(sql):
    out = subprocess.run(
        ["psql", DSN, "-At", "-F", "\x1f", "-c", sql],
        capture_output=True, text=True, check=True).stdout
    return [line.split("\x1f") for line in out.splitlines() if line]

tables = [r[0] for r in q("""
  SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
  WHERE n.nspname='public' AND c.relkind='r' ORDER BY c.relname""")]

cols = {}
for t, name, typ, nullable, default, pos in q("""
  SELECT table_name, column_name, data_type, is_nullable,
         COALESCE(column_default,''), ordinal_position
  FROM information_schema.columns WHERE table_schema='public'
  ORDER BY table_name, ordinal_position"""):
    cols.setdefault(t, []).append({
        "name": name, "type": typ, "nullable": nullable == "YES",
        "default": default, "position": int(pos)})

rls = {r[0]: {"enabled": truthy(r[1]), "forced": truthy(r[2]), "owner": r[3]}
       for r in q("""
  SELECT c.relname, c.relrowsecurity::text, c.relforcerowsecurity::text, pg_get_userbyid(c.relowner)
  FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
  WHERE n.nspname='public' AND c.relkind='r'""")}

policies = {}
for t, name, cmd, roles, using, check in q("""
  SELECT tablename, policyname, cmd, array_to_string(roles,','),
         COALESCE(qual,''), COALESCE(with_check,'')
  FROM pg_policies WHERE schemaname='public' ORDER BY tablename, policyname"""):
    policies.setdefault(t, []).append(
        {"name": name, "command": cmd, "roles": roles, "using": using, "with_check": check})

constraints = {}
for t, name, ctype, definition in q("""
  SELECT rel.relname, con.conname, con.contype,
         pg_get_constraintdef(con.oid)
  FROM pg_constraint con
  JOIN pg_class rel ON rel.oid=con.conrelid
  JOIN pg_namespace n ON n.oid=rel.relnamespace
  WHERE n.nspname='public' ORDER BY rel.relname, con.conname"""):
    kind = {"p": "PRIMARY KEY", "f": "FOREIGN KEY", "u": "UNIQUE",
            "c": "CHECK", "x": "EXCLUDE"}.get(ctype, ctype)
    constraints.setdefault(t, []).append(
        {"name": name, "kind": kind, "definition": definition})

indexes = {}
for t, name, definition in q("""
  SELECT tablename, indexname, indexdef FROM pg_indexes
  WHERE schemaname='public' ORDER BY tablename, indexname"""):
    indexes.setdefault(t, []).append({"name": name, "definition": definition})

triggers = {}
for t, name, definition in q("""
  SELECT c.relname, tg.tgname, pg_get_triggerdef(tg.oid)
  FROM pg_trigger tg JOIN pg_class c ON c.oid=tg.tgrelid
  JOIN pg_namespace n ON n.oid=c.relnamespace
  WHERE n.nspname='public' AND NOT tg.tgisinternal ORDER BY c.relname, tg.tgname"""):
    triggers.setdefault(t, []).append({"name": name, "definition": definition})

grants = {}
for t, grantee, priv in q("""
  SELECT table_name, grantee, privilege_type FROM information_schema.role_table_grants
  WHERE table_schema='public' ORDER BY table_name, grantee, privilege_type"""):
    grants.setdefault(t, {}).setdefault(grantee, []).append(priv)

snapshot = {
    "schema": "public",
    "postgres_version": q("SELECT current_setting('server_version')")[0][0],
    "extensions": sorted(r[0] for r in q("SELECT extname FROM pg_extension")),
    "table_count": len(tables),
    "application_role": {
        "name": "ira_app",
        "is_superuser": truthy(q("SELECT rolsuper::text FROM pg_roles WHERE rolname='ira_app'")[0][0]),
        "bypassrls":    truthy(q("SELECT rolbypassrls::text FROM pg_roles WHERE rolname='ira_app'")[0][0]),
    },
    "tables": {
        t: {
            "columns": cols.get(t, []),
            "rls": rls.get(t, {}),
            "policies": policies.get(t, []),
            "constraints": constraints.get(t, []),
            "indexes": indexes.get(t, []),
            "triggers": triggers.get(t, []),
            "grants": grants.get(t, {}),
        } for t in tables
    },
}
json.dump(snapshot, open(sys.argv[1], "w"), indent=1, sort_keys=True)
print("tables:", len(tables))
print("policies:", sum(len(v) for v in policies.values()))
print("triggers:", sum(len(v) for v in triggers.values()))
print("constraints:", sum(len(v) for v in constraints.values()))
