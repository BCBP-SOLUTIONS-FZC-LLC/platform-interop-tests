package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"time"

	"github.com/BCBP-SOLUTIONS-FZC-LLC/platform-gincommon/pkg/gincommon"
	"github.com/BCBP-SOLUTIONS-FZC-LLC/platform-gincommon/pkg/problem"
	"github.com/gin-gonic/gin"
)

// problemScenario is one error response both probes produce through their real framework stack
// (python-probe problem-bodies has the same list, by name).
type problemScenario struct {
	Name string `json:"name"`
	Path string `json:"path"`
}

var problemScenarios = []problemScenario{
	{"validation_failed", "/validation"},
	{"not_found", "/tenders/123"},
	{"conflict_with_extensions", "/conflict"},
	{"internal_error", "/internal"},
	{"unauthorized", "/protected"},
	{"route_not_found", "/no-such-route"},
	{"about_blank", "/gone"},
	{"server_timeout", "/slow"},
}

// capturedProblem is one raw response: what a client receives, byte for byte.
type capturedProblem struct {
	Name     string            `json:"name"`
	Source   string            `json:"source"`
	Path     string            `json:"path"`
	Status   int               `json:"status"`
	Headers  map[string]string `json:"headers"`
	Body     string            `json:"body"`
	Expected *decodedProblem   `json:"expected,omitempty"`
}

// decodedProblem is gincommon's problem.FromResponse result for a captured body: the reference
// the frontend SDK's parseProblemResponse is compared against.
type decodedProblem struct {
	Type             string               `json:"type"`
	Title            string               `json:"title"`
	Status           int                  `json:"status"`
	Detail           string               `json:"detail,omitempty"`
	Instance         string               `json:"instance,omitempty"`
	TraceID          string               `json:"traceId,omitempty"`
	ErrorCode        string               `json:"errorCode,omitempty"`
	TenantID         string               `json:"tenantId,omitempty"`
	Timestamp        string               `json:"timestamp,omitempty"`
	ValidationErrors []problem.FieldError `json:"validationErrors,omitempty"`
	Extensions       map[string]any       `json:"extensions"`
}

// problemRequestHeaders are sent with every scenario, so trace and request ids are deterministic.
func problemRequestHeaders(name string) map[string]string {
	return map[string]string{
		"traceparent":  "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
		"x-request-id": "fixture-" + strings.ReplaceAll(name, "_", "-"),
	}
}

// problemEngine is a gincommon service with one route per scenario. cfg is the process's
// gincommon config (metrics are initialised once per identity); a 50 ms RequestTimeout is added
// for the server-timeout scenario. http-serve also mounts it under /errors.
func problemEngine(cfg gincommon.Config) *gin.Engine {
	gin.SetMode(gin.ReleaseMode)
	r := gin.New()
	cfg.RequestTimeout = 50 * time.Millisecond
	r.Use(gincommon.ObservabilityMiddlewares(cfg)...)

	r.GET("/validation", func(c *gin.Context) {
		gincommon.AbortWithProblem(c, problem.NewValidationError(
			problem.FieldError{Field: "deadline", Message: "must be in the future"},
			problem.FieldError{Field: "items[0].sku", Message: "is required"},
		))
	})
	r.GET("/tenders/:id", func(c *gin.Context) {
		gincommon.AbortWithProblem(c, problem.NewNotFoundError("Tender 123 was not found.").WithCode("TENDER_NOT_FOUND"))
	})
	r.GET("/conflict", func(c *gin.Context) {
		gincommon.AbortWithProblem(c, problem.NewConflictError("The tenant was modified concurrently.").
			WithCode("VERSION_CONFLICT").
			WithExtension("record_version", 7).
			WithExtensions(map[string]any{"active_workflows": []string{"wf-1", "wf-2"}, "Zeta": true}))
	})
	r.GET("/internal", func(c *gin.Context) {
		gincommon.AbortWithProblem(c, problem.NewInternalError(errors.New("pq: password authentication failed for user app")))
	})
	r.GET("/gone", func(c *gin.Context) {
		gincommon.AbortWithProblem(c, problem.NewError(problem.TypeForStatus(http.StatusGone), "The tender was withdrawn."))
	})
	r.GET("/slow", func(c *gin.Context) {
		select {
		case <-c.Request.Context().Done():
		case <-time.After(2 * time.Second):
			c.String(http.StatusOK, "too late")
		}
	})
	protected := r.Group("/")
	protected.Use(gincommon.ProtectedMiddlewares(cfg)...)
	protected.GET("/protected", func(c *gin.Context) { c.String(http.StatusOK, "ok") })
	return r
}

func captureGoProblems() ([]capturedProblem, error) {
	engine := problemEngine(gincommon.Config{ServiceName: "go-probe", Domain: "documents", Environment: "test"})
	out := make([]capturedProblem, 0, len(problemScenarios))
	for _, s := range problemScenarios {
		req := httptest.NewRequest(http.MethodGet, s.Path, nil)
		for k, v := range problemRequestHeaders(s.Name) {
			req.Header.Set(k, v)
		}
		rec := httptest.NewRecorder()
		engine.ServeHTTP(rec, req)
		res := rec.Result()
		body, err := io.ReadAll(res.Body)
		if err != nil {
			return nil, err
		}
		out = append(out, capturedProblem{
			Name:   s.Name,
			Source: "go",
			Path:   s.Path,
			Status: res.StatusCode,
			Headers: map[string]string{
				"content-type": res.Header.Get("Content-Type"),
				"x-request-id": res.Header.Get("X-Request-ID"),
				"x-trace-id":   res.Header.Get("X-Trace-ID"),
			},
			Body: string(body),
		})
	}
	return out, nil
}

// decodeCaptured runs gincommon's client decoder over a captured response.
func decodeCaptured(c capturedProblem) (*decodedProblem, error) {
	res := &http.Response{
		StatusCode: c.Status,
		Header:     http.Header{"Content-Type": []string{c.Headers["content-type"]}},
		Body:       io.NopCloser(bytes.NewReader([]byte(c.Body))),
	}
	p, err := problem.FromResponse(res)
	if err != nil {
		return nil, fmt.Errorf("%s/%s: %w", c.Source, c.Name, err)
	}
	ext := p.Extensions
	if ext == nil {
		ext = map[string]any{}
	}
	return &decodedProblem{
		Type: p.Type, Title: p.Title, Status: p.Status, Detail: p.Detail, Instance: p.Instance,
		TraceID: p.TraceID, ErrorCode: p.ErrorCode, TenantID: p.TenantID, Timestamp: p.Timestamp,
		ValidationErrors: p.ValidationErrors, Extensions: ext,
	}, nil
}

// runProblemBodies captures every scenario from gincommon, adds python-probe's captures of the
// same scenarios (--python, the output of `python -m probe problem-bodies`), decodes all of them
// with problem.FromResponse and prints fixtures/problem_bodies.json.
func runProblemBodies(args []string) error {
	fs := flag.NewFlagSet("problem-bodies", flag.ContinueOnError)
	pyPath := fs.String("python", "", "python-probe problem-bodies output to include")
	if err := fs.Parse(args); err != nil {
		return err
	}
	captured, err := captureGoProblems()
	if err != nil {
		return err
	}
	if *pyPath != "" {
		raw, err := os.ReadFile(*pyPath)
		if err != nil {
			return err
		}
		var py []capturedProblem
		if err := json.Unmarshal(raw, &py); err != nil {
			return fmt.Errorf("parse %s: %w", *pyPath, err)
		}
		if len(py) != len(problemScenarios) {
			return fmt.Errorf("%s has %d scenarios, want %d", *pyPath, len(py), len(problemScenarios))
		}
		captured = append(captured, py...)
	}
	for i := range captured {
		d, err := decodeCaptured(captured[i])
		if err != nil {
			return err
		}
		captured[i].Expected = d
	}
	return printJSON(map[string]any{
		"_comment": "Real RFC 9457 error responses captured from platform-gincommon (go-probe problem-bodies) and " +
			"platform-fastapicommon (python-probe problem-bodies), one per scenario and language, with each request's " +
			"traceparent and x-request-id fixed. `body` is the raw response body; `expected` is gincommon's own client " +
			"decoder (problem.FromResponse) applied to it, the reference the frontend SDK's parseProblemResponse must " +
			"match. A capture, not a contract: regenerate with go-probe problem-bodies --python <python capture>.",
		"requestHeaders": problemRequestHeaders("<scenario>"),
		"cases":          captured,
	})
}
