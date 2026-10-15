package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/BCBP-SOLUTIONS-FZC-LLC/platform-pgcommon/pkg/domain"
	"github.com/BCBP-SOLUTIONS-FZC-LLC/platform-pgcommon/pkg/migrate"
	gomigrate "github.com/golang-migrate/migrate/v4"
)

// captureLogger records the Runner's log messages: "migrations: no changes to apply" vs
// "migrations: applied up to version N" is how a run reports whether it changed anything —
// the same messages pgcommon-py's Runner logs.
type captureLogger struct {
	mu   sync.Mutex
	msgs []string
}

func (l *captureLogger) add(msg string) { l.mu.Lock(); l.msgs = append(l.msgs, msg); l.mu.Unlock() }

func (l *captureLogger) Debug(msg string, _ ...domain.Field) {}
func (l *captureLogger) Info(msg string, _ ...domain.Field)  { l.add(msg) }
func (l *captureLogger) Warn(msg string, _ ...domain.Field)  { l.add("warning: " + msg) }
func (l *captureLogger) Error(msg string, _ ...domain.Field) { l.add("error: " + msg) }

// migrateErrorCode maps a Runner error to the language-agnostic codes python-probe's
// pg-migrate uses for pgcommon-py's equivalents.
func migrateErrorCode(err error) string {
	var short gomigrate.ErrShortLimit
	switch {
	case errors.Is(err, migrate.ErrMigrationDirty):
		return "dirty"
	case errors.Is(err, migrate.ErrVersionNotInSource):
		return "not_in_source"
	case errors.As(err, &short):
		return "short_limit"
	}
	return "error"
}

// runPGMigrate runs ONE pgcommon migrate.Runner operation and reports its result as JSON:
//
//	go-probe pg-migrate --dsn DSN --dir DIR [--table T] [--lock-timeout D] <up|down N|steps N|version|force V>
//
// It is the Go half of pg-migrate-check: python-probe's pg-migrate-check orchestrator runs
// it and python-probe's own pg-migrate (the same interface over pgcommon-py's Runner)
// against one database, interleaved and concurrently, and observes pg_locks meanwhile.
// A failed operation is data ("ok": false, "error_code"), not a process failure.
func runPGMigrate(args []string) error {
	fs := flag.NewFlagSet("pg-migrate", flag.ContinueOnError)
	dsn := fs.String("dsn", "", "postgres DSN (required)")
	dir := fs.String("dir", "", "migrations directory (required)")
	table := fs.String("table", "", "tracking table (Runner.MigrationsTable; default pgcommon_migrations)")
	lockTimeout := fs.Duration("lock-timeout", 0, "Runner.LockTimeout (0 = the 30s default)")
	if err := fs.Parse(args); err != nil {
		return err
	}
	rest := fs.Args()
	if *dsn == "" || *dir == "" || len(rest) == 0 {
		return fmt.Errorf("usage: pg-migrate --dsn DSN --dir DIR [--table T] <up|down N|steps N|version|force V>")
	}
	action := rest[0]
	n := 0
	switch action {
	case "down", "steps", "force":
		if len(rest) != 2 {
			return fmt.Errorf("pg-migrate %s requires a number", action)
		}
		v, err := strconv.Atoi(rest[1])
		if err != nil {
			return fmt.Errorf("pg-migrate %s: %w", action, err)
		}
		n = v
	case "up", "version":
	default:
		return fmt.Errorf("pg-migrate: unknown action %q", action)
	}

	logger := &captureLogger{}
	r := &migrate.Runner{
		FS:              os.DirFS(*dir),
		DSN:             *dsn,
		MigrationsTable: *table,
		Logger:          logger,
		LockTimeout:     *lockTimeout,
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()

	out := map[string]any{
		"language": "go",
		"action":   strings.Join(rest, " "),
		"ok":       true,
	}
	var err error
	switch action {
	case "up":
		err = r.Up(ctx)
	case "down":
		err = r.Down(ctx, n)
	case "steps":
		err = r.Steps(ctx, n)
	case "force":
		err = r.Force(ctx, n)
	case "version":
		var v uint
		var dirty bool
		v, dirty, err = r.Version(ctx)
		switch {
		case errors.Is(err, gomigrate.ErrNilVersion):
			out["version"], out["dirty"], err = nil, false, nil
		case err == nil || errors.Is(err, migrate.ErrVersionNotInSource):
			out["version"], out["dirty"] = v, dirty
		case dirty: // no version recorded, but dirty (a failed down of the first migration)
			out["version"], out["dirty"] = nil, true
		}
	}
	if err != nil {
		out["ok"] = false
		out["error_code"] = migrateErrorCode(err)
		out["error"] = err.Error()
	}
	logger.mu.Lock()
	msgs := append([]string{}, logger.msgs...)
	logger.mu.Unlock()
	out["log"] = msgs
	out["outcome"] = runOutcome(action, err, msgs)
	return printJSON(out)
}

// runOutcome classifies a successful up/down/steps run from its log: "no_change" when the
// Runner reported nothing to do, "changed" otherwise. nil for other actions or a failed run.
func runOutcome(action string, err error, msgs []string) any {
	if err != nil || (action != "up" && action != "down" && action != "steps") {
		return nil
	}
	for _, m := range msgs {
		if strings.HasPrefix(m, "migrations: no changes") || strings.HasPrefix(m, "migrations: nothing to roll back") {
			return "no_change"
		}
	}
	return "changed"
}
