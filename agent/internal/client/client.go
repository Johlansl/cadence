// Package client talks to the Cadence server over HTTP. Communication is
// outbound-only. Reports are one-shot (no retry loop; the systemd timer drives
// the next attempt); a job result is posted once, right after the job runs.
package client

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
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

// JobResult is the body of POST /api/v1/jobs/{id}/result.
type JobResult struct {
	Status         string `json:"status"` // "succeeded" | "failed"
	ExitCode       int    `json:"exit_code"`
	Log            string `json:"log"`
	RebootRequired bool   `json:"reboot_required"`
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

func (c *Client) do(ctx context.Context, path string, body []byte) (*http.Response, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+path, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+c.token)

	resp, err := c.hc.Do(req)
	if err != nil {
		return nil, fmt.Errorf("POST %s: %w", path, err)
	}
	return resp, nil
}
