ALTER TABLE interop_widgets ADD COLUMN color text;
INSERT INTO interop_migration_effects (version) VALUES (3);
