-- Deliberately slow: pg-migrate-check observes the advisory lock (pg_locks) and a second
-- runner's wait for it while this migration runs.
SELECT pg_sleep(1.5);
CREATE TABLE interop_widgets (id int PRIMARY KEY, name text NOT NULL);
INSERT INTO interop_migration_effects (version) VALUES (2);
