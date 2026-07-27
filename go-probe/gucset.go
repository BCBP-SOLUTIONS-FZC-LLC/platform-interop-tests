package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"

	"github.com/BCBP-SOLUTIONS-FZC-LLC/platform-pgcommon/pkg/domain"
)

type gucsetCase struct {
	Name          string   `json:"name"`
	UserID        string   `json:"user_id"`
	TenantID      string   `json:"tenant_id"`
	TenantRoles   []string `json:"tenant_roles"`
	ExpectedValid bool     `json:"expected_valid"`
	ExpectedError *string  `json:"expected_error"`
}

type gucsetFixture struct {
	Cases []gucsetCase `json:"cases"`
}

type gucsetResult struct {
	Name           string  `json:"name"`
	Valid          bool    `json:"valid"`
	Error          *string `json:"error"`
	MatchesFixture bool    `json:"matches_fixture"`
}

// errorCode maps a validation error to the fixture's language-agnostic error code strings.
func errorCode(err error) *string {
	if err == nil {
		return nil
	}
	var code string
	switch {
	case errors.Is(err, domain.ErrInconsistentGUCSet):
		code = "inconsistent"
	case errors.Is(err, domain.ErrEmptyTenantRole):
		code = "empty_role"
	case errors.Is(err, domain.ErrInvalidTenantRole):
		code = "invalid_role"
	default:
		code = "unknown:" + err.Error()
	}
	return &code
}

func runGUCSetCheck(args []string) error {
	if len(args) < 1 {
		return fmt.Errorf("gucset-check requires a path to gucset_cases.json")
	}
	data, err := os.ReadFile(args[0])
	if err != nil {
		return fmt.Errorf("reading fixture: %w", err)
	}
	var fixture gucsetFixture
	if err := json.Unmarshal(data, &fixture); err != nil {
		return fmt.Errorf("parsing fixture: %w", err)
	}

	results := make([]gucsetResult, 0, len(fixture.Cases))
	allMatch := true
	for _, c := range fixture.Cases {
		g := domain.GUCSet{UserID: c.UserID, TenantID: c.TenantID, TenantRoles: c.TenantRoles}
		verr := g.Validate()
		code := errorCode(verr)
		matches := (verr == nil) == c.ExpectedValid
		if code != nil && c.ExpectedError != nil {
			matches = matches && *code == *c.ExpectedError
		} else {
			matches = matches && (code == nil) == (c.ExpectedError == nil)
		}
		if !matches {
			allMatch = false
		}
		results = append(results, gucsetResult{
			Name:           c.Name,
			Valid:          verr == nil,
			Error:          code,
			MatchesFixture: matches,
		})
	}

	return printJSON(map[string]any{
		"language":  "go",
		"check":     "gucset-check",
		"all_match": allMatch,
		"results":   results,
	})
}
