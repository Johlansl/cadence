// Package client sends the assembled report to the Cadence server over HTTP.
// Communication is outbound-only and one-shot: a single POST, no retry loop
// (the systemd timer drives the next attempt).
package client

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
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

// SendReport POSTs the report to {baseURL}/api/v1/reports.
func (c *Client) SendReport(ctx context.Context, r report.Report) error {
	body, err := json.Marshal(r)
	if err != nil {
		return fmt.Errorf("encoding report: %w", err)
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost,
		c.baseURL+"/api/v1/reports", bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+c.token)

	resp, err := c.hc.Do(req)
	if err != nil {
		return fmt.Errorf("sending report: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		snippet, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return fmt.Errorf("server returned %s: %s", resp.Status, bytes.TrimSpace(snippet))
	}
	return nil
}
