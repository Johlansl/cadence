// Package client talks to the Cadence server over HTTP. Communication is
// outbound-only. Reports are one-shot (no retry loop; the systemd timer drives
// the next attempt); a job result is posted once, right after the job runs.
package client

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"time"

	"cadence/agent/internal/report"
)

type Client struct {
	baseURL string
	token   string
	hc      *http.Client
}

func New(baseURL, token string, timeout time.Duration) *Client {
	return &Client{
		baseURL: baseURL,
		token:   token,
		hc:      &http.Client{Timeout: timeout},
	}
}

// SendReport POSTs the report to {baseURL}/api/v1/reports and returns the
// piggybacked job, if the server handed one back (nil otherwise).
func (c *Client) SendReport(ctx context.Context, r report.Report) (*report.JobHandoff, error) {
	body, err := json.Marshal(r)
	if err != nil {
		return nil, fmt.Errorf("encoding report: %w", err)
	}

	resp, err := c.do(ctx, "/api/v1/reports", body)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	payload, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("server returned %s: %s", resp.Status, bytes.TrimSpace(payload))
	}

	var parsed report.Response
	if err := json.Unmarshal(payload, &parsed); err != nil {
		return nil, fmt.Errorf("decoding report response: %w", err)
	}
	return parsed.Job, nil
}

// ClaimNextJob asks the server for a pending job without sending a package
// report (the fast poll path). Returns nil when nothing is pending.
func (c *Client) ClaimNextJob(ctx context.Context) (*report.JobHandoff, error) {
	resp, err := c.do(ctx, "/api/v1/agent/next-job", []byte("{}"))
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	payload, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("server returned %s: %s", resp.Status, bytes.TrimSpace(payload))
	}

	var parsed report.Response // {"job": ...}
	if err := json.Unmarshal(payload, &parsed); err != nil {
		return nil, fmt.Errorf("decoding next-job response: %w", err)
	}
	return parsed.Job, nil
}

// JobResult is the body of POST /api/v1/jobs/{id}/result.
type JobResult struct {
	Status         string `json:"status"` // "succeeded" | "failed"
	ExitCode       int    `json:"exit_code"`
	Log            string `json:"log"`
	RebootRequired bool   `json:"reboot_required"`

	// Failure classification, sent only for a failed job (roadmap item 2).
	// omitempty keeps a success result byte-identical to older agents.
	FailureCategory string `json:"failure_category,omitempty"`
	FailureSummary  string `json:"failure_summary,omitempty"`

	// HeldConflicts names any held package a hold/dependency conflict was
	// found in (roadmap item 3). Deliberately NOT omitempty: nil must encode
	// as JSON null (not applicable -- a non-apt_upgrade job) so the server
	// can tell that apart from an empty list (reconciliation ran and found
	// nothing wrong).
	HeldConflicts []string `json:"held_conflicts"`

	// HeldPackages is what Cadence actually holds on this host after this
	// run's reconciliation (roadmap item 3 follow-up). Same never-omitted
	// convention as HeldConflicts: null = not applicable, [] = reconciled
	// and nothing held, list = the real set.
	HeldPackages []string `json:"held_packages"`

	// DryRun is the structured simulation preview, set only for an
	// apt_dry_run job that ran the simulation (roadmap item 4). Deliberately
	// NOT omitempty: null tells the server "not a dry-run, or a dry-run that
	// failed before producing a result" apart from a real preview.
	DryRun *report.DryRun `json:"dry_run"`
}

// SubmitJobResult reports the outcome of a job back to the server.
func (c *Client) SubmitJobResult(ctx context.Context, jobID string, result JobResult) error {
	body, err := json.Marshal(result)
	if err != nil {
		return fmt.Errorf("encoding job result: %w", err)
	}

	resp, err := c.do(ctx, "/api/v1/jobs/"+url.PathEscape(jobID)+"/result", body)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		snippet, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return fmt.Errorf("server returned %s: %s", resp.Status, bytes.TrimSpace(snippet))
	}
	return nil
}

// sign builds the three signed-request headers for one POST: the SHA-256 of
// the token (a non-secret lookup key -- the server already stores the same
// hash, never the raw secret over the wire), the current Unix timestamp, and
// an HMAC-SHA256 over "timestamp\nMETHOD\npath\nsha256(body)" keyed with the
// real token. Must match app.api.deps._auth_signed byte for byte.
func (c *Client) sign(method, path string, body []byte) (tokenHash, timestamp, signature string) {
	sum := sha256.Sum256([]byte(c.token))
	tokenHash = hex.EncodeToString(sum[:])
	timestamp = strconv.FormatInt(time.Now().Unix(), 10)
	bodySum := sha256.Sum256(body)
	canonical := timestamp + "\n" + method + "\n" + path + "\n" + hex.EncodeToString(bodySum[:])
	mac := hmac.New(sha256.New, []byte(c.token))
	mac.Write([]byte(canonical))
	signature = hex.EncodeToString(mac.Sum(nil))
	return tokenHash, timestamp, signature
}

// retryWaits is the delay before attempts 2..N. A transient network error or a
// 5xx is retried; a 4xx is returned as-is (caller decides). The systemd timer
// still drives the next full run -- this just rides out a brief server restart
// or blip instead of failing the unit every minute.
var retryWaits = []time.Duration{0, 2 * time.Second, 5 * time.Second}

func (c *Client) do(ctx context.Context, path string, body []byte) (*http.Response, error) {
	var lastErr error
	for attempt, wait := range retryWaits {
		if wait > 0 {
			select {
			case <-time.After(wait):
			case <-ctx.Done():
				return nil, ctx.Err()
			}
		}

		req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+path, bytes.NewReader(body))
		if err != nil {
			return nil, err
		}
		req.Header.Set("Content-Type", "application/json")
		// Signed request, not a raw bearer token: the token never crosses the
		// wire per call. A fresh timestamp each attempt (retries are seconds
		// apart at most, well inside the server's freshness window). See
		// docs/decisions.md "Authentication" for the canonical string and the
		// server-side verification this must match byte for byte.
		tokenHash, timestamp, signature := c.sign(http.MethodPost, path, body)
		req.Header.Set("X-Cadence-Token-Hash", tokenHash)
		req.Header.Set("X-Cadence-Timestamp", timestamp)
		req.Header.Set("X-Cadence-Signature", signature)

		resp, err := c.hc.Do(req)
		if err != nil {
			lastErr = fmt.Errorf("POST %s: %w", path, err)
			continue
		}
		if resp.StatusCode >= 500 && attempt < len(retryWaits)-1 {
			resp.Body.Close()
			lastErr = fmt.Errorf("POST %s: server returned %s", path, resp.Status)
			continue
		}
		return resp, nil
	}
	return nil, lastErr
}
