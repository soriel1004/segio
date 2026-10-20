-- Schema metadata only: never SELECT application rows or password values.
-- Use the PostgreSQL server's own psql client (including older installations).
\set ON_ERROR_STOP on
\pset pager off
\pset format aligned
\pset null '(null)'

BEGIN TRANSACTION READ ONLY;
SET LOCAL statement_timeout = 15000;

\echo === SEGIO_SCHEMA_REPORT_V1 ===
\echo This report contains schema metadata, not approval documents or account passwords.
\echo === DATABASE ===
SELECT current_database() AS database_name,
       current_setting('server_version') AS server_version,
       current_setting('server_encoding') AS server_encoding;

\echo === APPROVAL_RELATIONS ===
SELECT n.nspname AS schema_name, c.relname AS relation_name,
       CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view'
                      WHEN 'm' THEN 'materialized_view' WHEN 'p' THEN 'partitioned_table'
                      ELSE c.relkind::text END AS relation_type,
       has_table_privilege(c.oid, 'SELECT') AS can_select
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname ~* '^approval_'
  AND c.relkind IN ('r', 'v', 'm', 'p')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
ORDER BY n.nspname, c.relname;

\echo === APPROVAL_COLUMNS ===
SELECT table_schema, table_name, ordinal_position, column_name,
       data_type, udt_name, is_nullable, character_maximum_length
FROM information_schema.columns
WHERE table_name ~* '^approval_'
  AND table_schema NOT IN ('pg_catalog', 'information_schema')
ORDER BY table_schema, table_name, ordinal_position;

-- Restrict to key definitions. Do not export defaults/check expressions that
-- might contain application literals; never export stored function bodies.
\echo === APPROVAL_KEYS ===
SELECT n.nspname AS schema_name, t.relname AS table_name, k.conname AS key_name,
       CASE k.contype WHEN 'p' THEN 'primary_key' WHEN 'f' THEN 'foreign_key'
                      WHEN 'u' THEN 'unique_key' END AS key_type,
       pg_catalog.pg_get_constraintdef(k.oid) AS key_definition
FROM pg_catalog.pg_constraint k
JOIN pg_catalog.pg_class t ON t.oid = k.conrelid
JOIN pg_catalog.pg_namespace n ON n.oid = t.relnamespace
WHERE t.relname ~* '^approval_'
  AND k.contype IN ('p', 'f', 'u')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
ORDER BY n.nspname, t.relname, k.conname;

\echo === APPROVAL_VIEW_DEPENDENCIES ===
SELECT view_schema, view_name, table_schema, table_name
FROM information_schema.view_table_usage
WHERE view_name ~* '^approval_'
ORDER BY view_schema, view_name, table_schema, table_name;

\echo === STATUS ===
SELECT current_setting('transaction_read_only') AS transaction_read_only;
ROLLBACK;
\echo === END_SEGIO_SCHEMA_REPORT_V1 ===
