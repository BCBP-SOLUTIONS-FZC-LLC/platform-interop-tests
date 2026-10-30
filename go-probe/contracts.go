package main

import (
	"fmt"
	"net/http"

	"github.com/BCBP-SOLUTIONS-FZC-LLC/platform-gincommon/pkg/gincommon"
	"github.com/BCBP-SOLUTIONS-FZC-LLC/platform-gincommon/pkg/problem"
	"go.opentelemetry.io/otel/propagation"
)

// runContractsExport prints the public gincommon contracts the frontend SDK
// (platform-reactcommon) generates its contracts/ files from: the problem type registry, the
// problem constants and TypeForStatus mapping, and the transport header names. It reads only
// exported API (pkg/problem, pkg/gincommon), so the values are exactly what services emit.
// Header validation limits live in the unexported internal/core/domain package and are not
// part of this export.
func runContractsExport(args []string) error {
	if len(args) != 0 {
		return fmt.Errorf("contracts-export takes no arguments")
	}

	type exportedType struct {
		Domain string `json:"domain"`
		Name   string `json:"name"`
		ID     string `json:"id"`
		Title  string `json:"title"`
		Status int    `json:"status"`
		Code   string `json:"code"`
	}
	toExported := func(t problem.Type) exportedType {
		return exportedType{Domain: t.Domain, Name: t.Name, ID: t.ID(), Title: t.Title, Status: t.Status, Code: t.Code}
	}

	types := make([]exportedType, 0, len(problem.PlatformTypes()))
	for _, t := range problem.PlatformTypes() {
		types = append(types, toExported(t))
	}

	// TypeForStatus for every status net/http knows, in ascending order. A status without a
	// platform type maps to an about:blank type (empty domain and name) with the status text.
	typeForStatus := make([]exportedType, 0, 64)
	for status := 100; status <= 599; status++ {
		if http.StatusText(status) == "" {
			continue
		}
		typeForStatus = append(typeForStatus, toExported(problem.TypeForStatus(status)))
	}

	return printJSON(map[string]any{
		"problem": map[string]any{
			"contentType":        problem.ContentType,
			"defaultTypeBase":    problem.DefaultTypeBase,
			"timestampLayout":    problem.TimestampLayout,
			"platformDomain":     problem.PlatformDomain,
			"codeRequestTimeout": problem.CodeRequestTimeout,
			"maxRequestIdLength": problem.MaxRequestIDLength,
			"types":              types,
			"typeForStatus":      typeForStatus,
		},
		"headers": map[string]any{
			"request": map[string]string{
				"userId":      gincommon.HeaderUserID,
				"tenantId":    gincommon.HeaderTenantID,
				"tenantRoles": gincommon.HeaderTenantRoles,
				"requestId":   gincommon.HeaderRequestID,
			},
			"response": map[string]string{
				"traceId":   gincommon.HeaderTraceIDResponse,
				"requestId": gincommon.HeaderRequestIDResponse,
			},
			// W3C trace context fields, as gincommon's propagator injects and extracts them.
			"traceContext": propagation.TraceContext{}.Fields(),
		},
	})
}
