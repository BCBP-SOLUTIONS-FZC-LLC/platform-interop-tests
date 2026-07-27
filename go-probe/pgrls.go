package main

import (
	"context"
	"flag"
	"fmt"

	"github.com/BCBP-SOLUTIONS-FZC-LLC/platform-pgcommon/pkg/domain"
	"github.com/BCBP-SOLUTIONS-FZC-LLC/platform-pgcommon/pkg/pgcommon"
	"github.com/jackc/pgx/v5/pgxpool"
)

// runPGRLSCheck connects to the shared Postgres (schema pre-created by
// docker/init-rls.sql), scopes a connection to --tenant-id under either standard
// (session-scoped, is_local=false) or --mode=pgbouncer (transaction-scoped, is_local=true)
// GUC injection, and reports which rows of interop_rls_test are visible. run_interop.sh
// compares this against python-probe's identical check for the same tenant/mode — both must
// see exactly the same row set.
func runPGRLSCheck(args []string) error {
	fs := flag.NewFlagSet("pg-rls-check", flag.ContinueOnError)
	dsn := fs.String("dsn", "", "postgres DSN (required)")
	mode := fs.String("mode", "standard", "standard|pgbouncer")
	tenantID := fs.String("tenant-id", "", "tenant_id to scope the connection as")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *dsn == "" {
		return fmt.Errorf("--dsn is required")
	}
	if *mode != "standard" && *mode != "pgbouncer" {
		return fmt.Errorf("--mode must be 'standard' or 'pgbouncer', got %q", *mode)
	}

	ctx := context.Background()
	cfg := pgcommon.Config{
		DSN:           *dsn,
		GUCProvider:   pgcommon.GUCSetFromContext,
		PGBouncerMode: *mode == "pgbouncer",
		MaxConns:      2,
		MinConns:      1,
	}
	pool, err := pgcommon.NewPool(ctx, cfg)
	if err != nil {
		return fmt.Errorf("connecting: %w", err)
	}
	defer pool.Close()

	reqCtx := pgcommon.WithGUCSet(ctx, domain.GUCSet{TenantID: *tenantID})

	var visibleIDs []string
	err = pool.WithConn(reqCtx, func(ctx context.Context, conn *pgxpool.Conn) error {
		rows, err := conn.Query(ctx, "SELECT id FROM interop_rls_test ORDER BY id")
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			var id string
			if err := rows.Scan(&id); err != nil {
				return err
			}
			visibleIDs = append(visibleIDs, id)
		}
		return rows.Err()
	})
	if err != nil {
		return fmt.Errorf("querying under RLS: %w", err)
	}
	if visibleIDs == nil {
		visibleIDs = []string{}
	}

	return printJSON(map[string]any{
		"language":    "go",
		"check":       "pg-rls-check",
		"mode":        *mode,
		"tenant_id":   *tenantID,
		"visible_ids": visibleIDs,
	})
}
