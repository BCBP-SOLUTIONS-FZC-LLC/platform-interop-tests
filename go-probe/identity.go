package main

import (
	"encoding/json"
	"fmt"
	"os"
	"strings"

	"github.com/BCBP-SOLUTIONS-FZC-LLC/platform-gincommon/pkg/obsregistry"
)

type identityInput struct {
	Service     string  `json:"service"`
	Domain      string  `json:"domain"`
	Environment string  `json:"environment"`
	Version     *string `json:"version"`
}

type identityValues struct {
	Service     string `json:"service"`
	Domain      string `json:"domain"`
	Environment string `json:"environment"`
	Version     string `json:"version"`
}

type identityCase struct {
	Name     string          `json:"name"`
	Input    identityInput   `json:"input"`
	Identity *identityValues `json:"identity"`
	Error    *string         `json:"error"`
}

type identityResult struct {
	Name           string          `json:"name"`
	Identity       *identityValues `json:"identity"`
	Error          *string         `json:"error"`
	MatchesFixture bool            `json:"matches_fixture"`
}

// checkIdentity applies the identity rules every platform library shares, against gincommon's
// registry (the checks CheckIdentity makes): domain, service, then environment (trimmed and
// lower-cased), then the build version (trimmed, empty → "unknown", checked by CheckVersion).
// It returns the field that failed first, in that order.
func checkIdentity(reg *obsregistry.Registry, in identityInput) (*identityValues, string) {
	if !reg.IsDomain(in.Domain) {
		return nil, "domain"
	}
	if l := reg.Labels["service"]; l == nil || !l.Allows(in.Service) {
		return nil, "service"
	}
	env, err := reg.NormalizeEnvironment(in.Environment)
	if err != nil {
		return nil, "environment"
	}
	version := ""
	if in.Version != nil {
		version = strings.TrimSpace(*in.Version)
	}
	if version == "" {
		version = "unknown"
	}
	if err := reg.CheckVersion(version); err != nil {
		return nil, "version"
	}
	return &identityValues{Service: in.Service, Domain: in.Domain, Environment: env, Version: version}, ""
}

// runIdentityCheck runs every case in identity_cases.json through checkIdentity and reports,
// per case, the result and whether it matches the fixture. The frontend SDK's initializeCore
// runs the same file, so the browser and the services accept exactly the same identities.
func runIdentityCheck(args []string) error {
	if len(args) < 1 {
		return fmt.Errorf("identity-check requires a path to identity_cases.json")
	}
	raw, err := os.ReadFile(args[0])
	if err != nil {
		return err
	}
	var fixture struct {
		Cases []identityCase `json:"cases"`
	}
	if err := json.Unmarshal(raw, &fixture); err != nil {
		return fmt.Errorf("parse %s: %w", args[0], err)
	}
	reg := obsregistry.Default()
	results := make([]identityResult, 0, len(fixture.Cases))
	allMatch := true
	for _, c := range fixture.Cases {
		got, field := checkIdentity(reg, c.Input)
		r := identityResult{Name: c.Name, Identity: got}
		if field != "" {
			r.Error = &field
		}
		switch {
		case c.Error != nil:
			r.MatchesFixture = r.Error != nil && *r.Error == *c.Error
		case c.Identity != nil:
			r.MatchesFixture = got != nil && *got == *c.Identity
		}
		allMatch = allMatch && r.MatchesFixture
		results = append(results, r)
	}
	return printJSON(map[string]any{"all_match": allMatch, "results": results})
}
