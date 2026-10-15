-- pg-migrate-check fixture. Every up migration records itself in interop_migration_effects,
-- so the check can prove from the database itself that exactly one runner applied it.
CREATE TABLE interop_migration_effects (version int NOT NULL, applied_by text NOT NULL DEFAULT current_setting('application_name'));
INSERT INTO interop_migration_effects (version) VALUES (1);
