package main

import (
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"

	"github.com/BCBP-SOLUTIONS-FZC-LLC/platform-events/pkg/events"
)

type hmacVectorFixture struct {
	KeyHex       string          `json:"key_hex"`
	EnvelopeJSON json.RawMessage `json:"envelope_json"`
	SignatureHex string          `json:"signature_hex"`
}

func runHMACCheck(args []string) error {
	if len(args) < 1 {
		return fmt.Errorf("hmac-check requires a path to hmac_vector.json")
	}
	data, err := os.ReadFile(args[0])
	if err != nil {
		return fmt.Errorf("reading fixture: %w", err)
	}
	var fixture hmacVectorFixture
	if err := json.Unmarshal(data, &fixture); err != nil {
		return fmt.Errorf("parsing fixture: %w", err)
	}

	key, err := hex.DecodeString(fixture.KeyHex)
	if err != nil {
		return fmt.Errorf("decoding key_hex: %w", err)
	}

	var env events.Envelope[json.RawMessage]
	if err := json.Unmarshal(fixture.EnvelopeJSON, &env); err != nil {
		return fmt.Errorf("parsing envelope_json: %w", err)
	}

	// Direction 1: Go reproduces the exact signature already in the fixture (self-check that
	// the fixture is internally consistent — this is how the fixture was generated in the
	// first place).
	goSig, err := events.SignEnvelope(key, env)
	if err != nil {
		return fmt.Errorf("SignEnvelope: %w", err)
	}
	goReproducesFixtureSig := goSig == fixture.SignatureHex

	// Direction 2: verify the fixture's committed signature against this envelope.
	verifyOK, err := events.VerifyEnvelope(key, env, fixture.SignatureHex)
	if err != nil {
		return fmt.Errorf("VerifyEnvelope: %w", err)
	}

	return printJSON(map[string]any{
		"language":                  "go",
		"check":                     "hmac-check",
		"go_signature_hex":          goSig,
		"go_reproduces_fixture_sig": goReproducesFixtureSig,
		"verify_fixture_signature":  verifyOK,
		"all_match":                 goReproducesFixtureSig && verifyOK,
	})
}

type externalSigRequest struct {
	KeyHex       string          `json:"key_hex"`
	EnvelopeJSON json.RawMessage `json:"envelope_json"`
	SignatureHex string          `json:"signature_hex"`
}

// runHMACVerify completes the bidirectional check: run_interop.sh pipes a Python-computed
// {key_hex, envelope_json, signature_hex} blob (via python-probe's hmac-sign subcommand) into
// this subcommand's stdin, and Go verifies it — proving a Python-produced signature is accepted
// by the Go library, the reverse direction of hmac-check's Go-signs/fixture-verifies check.
func runHMACVerify(args []string) error {
	data, err := io.ReadAll(os.Stdin)
	if err != nil {
		return fmt.Errorf("reading stdin: %w", err)
	}
	var req externalSigRequest
	if err := json.Unmarshal(data, &req); err != nil {
		return fmt.Errorf("parsing stdin JSON: %w", err)
	}

	key, err := hex.DecodeString(req.KeyHex)
	if err != nil {
		return fmt.Errorf("decoding key_hex: %w", err)
	}
	var env events.Envelope[json.RawMessage]
	if err := json.Unmarshal(req.EnvelopeJSON, &env); err != nil {
		return fmt.Errorf("parsing envelope_json: %w", err)
	}

	ok, err := events.VerifyEnvelope(key, env, req.SignatureHex)
	if err != nil {
		return fmt.Errorf("VerifyEnvelope: %w", err)
	}

	return printJSON(map[string]any{
		"language":  "go",
		"check":     "hmac-verify",
		"all_match": ok,
		"verified":  ok,
	})
}
