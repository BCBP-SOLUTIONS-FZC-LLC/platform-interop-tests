-- Schema + seed data for pair 1's pg-rls-check: both go-probe and python-probe connect with a
-- GUCSet scoped to a specific tenant_id (in both standard and pgbouncer-simulated modes) and
-- must observe exactly the same row set under this RLS policy.
CREATE TABLE IF NOT EXISTS interop_rls_test (
    id text PRIMARY KEY,
    tenant_id text NOT NULL
);

ALTER TABLE interop_rls_test ENABLE ROW LEVEL SECURITY;

CREATE POLICY interop_rls_test_tenant_isolation ON interop_rls_test
    USING (tenant_id = current_setting('app.tenant_id', true));

-- A role that actually enforces RLS (table owners bypass RLS by default in Postgres).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'interop_app') THEN
        CREATE ROLE interop_app LOGIN PASSWORD 'interop_app';
    END IF;
END
$$;

GRANT ALL ON interop_rls_test TO interop_app;

INSERT INTO interop_rls_test (id, tenant_id) VALUES
    ('row-acme-1', 'acme'),
    ('row-acme-2', 'acme'),
    ('row-globex-1', 'globex'),
    ('row-initech-1', 'initech')
ON CONFLICT (id) DO NOTHING;
