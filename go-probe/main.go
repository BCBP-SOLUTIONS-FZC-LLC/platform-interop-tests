// Command go-probe exercises the Go side of each cross-language compatibility contract
// audited in COMPATIBILITY.md. Each subcommand prints a single JSON object to stdout and exits
// 0 on success, non-zero on any internal error (a *contract* mismatch is reported as data in
// the JSON, not as a process failure — run_interop.sh does the actual pass/fail diffing).
package main

import (
	"fmt"
	"os"
)

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "usage: go-probe <gucset-check|hmac-check|hmac-verify|envelope-check|metrics-check|pg-rls-check|http-serve> [args...]")
		os.Exit(2)
	}

	var err error
	switch os.Args[1] {
	case "gucset-check":
		err = runGUCSetCheck(os.Args[2:])
	case "hmac-check":
		err = runHMACCheck(os.Args[2:])
	case "hmac-verify":
		err = runHMACVerify(os.Args[2:])
	case "envelope-check":
		err = runEnvelopeCheck(os.Args[2:])
	case "metrics-check":
		err = runMetricsCheck(os.Args[2:])
	// No "header-check" subcommand: Go's header validation/parsing rules
	// (NormalizeHeaderValue/NormalizeRequestID/ParseRoles) live in platform-gincommon's
	// unexported internal/core/domain package, which Go's own module-privacy rules forbid this
	// separate module from importing. That contract is exercised only observably, through the
	// real HTTP surface — see "http-serve" and COMPATIBILITY.md § gincommon/fastapicommon's
	// residual-risk note.
	case "pg-rls-check":
		err = runPGRLSCheck(os.Args[2:])
	case "http-serve":
		err = runHTTPServe(os.Args[2:])
	default:
		fmt.Fprintf(os.Stderr, "unknown subcommand %q\n", os.Args[1])
		os.Exit(2)
	}

	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
}
