package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"

	"github.com/BCBP-SOLUTIONS-FZC-LLC/platform-gincommon/pkg/gincommon"
	"github.com/gin-gonic/gin"
)

// runHTTPServe starts a minimal gincommon-based HTTP server exercising the pair-2 contract:
// /health (unauthenticated), /whoami (protected — returns the parsed RequestContext), and
// /call-other (protected — propagates headers to --peer and returns what it got back, used for
// the cross-service trace-continuity check). This is the Go side of the pair-2 interop test;
// pair 2's header-validation rules live in gincommon's unexported internal/core/domain package
// and can only be exercised this way (via the real HTTP surface), not called directly.
func runHTTPServe(args []string) error {
	fs := flag.NewFlagSet("http-serve", flag.ContinueOnError)
	port := fs.String("port", "8081", "port to listen on")
	peer := fs.String("peer", "", "base URL of a peer service for /call-other to propagate to")
	if err := fs.Parse(args); err != nil {
		return err
	}

	gin.SetMode(gin.ReleaseMode)
	r := gin.New()

	cfg := gincommon.Config{ServiceName: "go-probe"}
	r.Use(gincommon.ObservabilityMiddlewares(cfg)...)

	r.GET("/health", gincommon.HealthHandler())

	protected := r.Group("/")
	protected.Use(gincommon.ProtectedMiddlewares(cfg)...)

	protected.GET("/whoami", func(c *gin.Context) {
		rc, ok := gincommon.RequestContext(c)
		if !ok {
			c.JSON(http.StatusInternalServerError, gin.H{"error": "no RequestContext bound"})
			return
		}
		c.JSON(http.StatusOK, gin.H{
			"language":  "go",
			"tenant_id": rc.TenantID,
			"user_id":   rc.UserID,
			"roles":     rc.Roles,
			"trace_id":  rc.TraceID,
			"client_ip": rc.ClientIP,
		})
	})

	protected.GET("/call-other", func(c *gin.Context) {
		if *peer == "" {
			c.JSON(http.StatusInternalServerError, gin.H{"error": "no --peer configured"})
			return
		}
		req, err := http.NewRequestWithContext(c.Request.Context(), http.MethodGet, *peer+"/whoami", nil)
		if err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
			return
		}
		gincommon.PropagateHeaders(c, req)

		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			c.JSON(http.StatusBadGateway, gin.H{"error": err.Error()})
			return
		}
		defer resp.Body.Close()
		body, err := io.ReadAll(resp.Body)
		if err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
			return
		}
		var peerBody map[string]any
		if err := json.Unmarshal(body, &peerBody); err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": "peer returned non-JSON: " + err.Error()})
			return
		}
		c.JSON(http.StatusOK, gin.H{
			"language":      "go",
			"own_trace_id":  func() string { rc, _ := gincommon.RequestContext(c); return rc.TraceID }(),
			"peer_response": peerBody,
			"peer_status":   resp.StatusCode,
		})
	})

	fmt.Fprintf(os.Stderr, "go-probe http-serve listening on :%s (peer=%q)\n", *port, *peer)
	return r.Run(":" + *port)
}
