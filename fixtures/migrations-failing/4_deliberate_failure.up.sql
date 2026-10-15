-- pg-migrate-check overlays this on fixtures/migrations/ for its dirty-state round trip: it
-- always fails (division by zero). Sent as one simple-protocol query by both runners, the
-- CREATE TABLE is rolled back with it, so the schema needs no repair before force(3).
CREATE TABLE interop_never_created (id int);
SELECT 1 / 0;
