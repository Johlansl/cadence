package main

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"cadence/agent/internal/apterr"
	"cadence/agent/internal/client"
	"cadence/agent/internal/config"
	"cadence/agent/internal/report"
)

// An agent that does not know a job type must refuse it before running
// anything: exactly one result submission, failed/agent_refused, naming the
// type. This pins the closed dispatch runJob is built on, so any future
// type stays refused until its execution code lands (agent_upgrade left
// this set when PR6 wired it).
func TestRunJobRefusesUnknownType(t *testing.T) {
	var got []map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || !strings.HasSuffix(r.URL.Path, "/result") {
			t.Errorf("unexpected request %s %s", r.Method, r.URL.Path)
		}
		body, _ := io.ReadAll(r.Body)
		var decoded map[string]any
		if err := json.Unmarshal(body, &decoded); err != nil {
			t.Errorf("result body is not JSON: %v", err)
		}
		got = append(got, decoded)
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	// Upgrades enabled so the test reaches the type dispatch, not the
	// CADENCE_ENABLE_UPGRADES gate that precedes it.
	cfg := config.Config{HTTPTimeout: 5 * time.Second, EnableUpgrades: true}
	c := client.New(srv.URL, "tok", 5*time.Second)
	job := &report.JobHandoff{ID: "job-1", JobType: "not_a_real_type"}

	if err := runJob(cfg, c, job); err != nil {
		t.Fatalf("runJob returned error: %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("got %d submissions, want exactly 1", len(got))
	}
	if got[0]["status"] != "failed" {
		t.Errorf("status = %v, want failed", got[0]["status"])
	}
	if got[0]["failure_category"] != apterr.CategoryAgentRefused {
		t.Errorf("failure_category = %v, want %q", got[0]["failure_category"], apterr.CategoryAgentRefused)
	}
	if !strings.Contains(got[0]["failure_summary"].(string), "not_a_real_type") {
		t.Errorf("failure_summary = %v, want it to name the type", got[0]["failure_summary"])
	}
}
