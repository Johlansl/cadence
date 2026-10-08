package main

import (
	"context"
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
	"cadence/agent/internal/upgrade"
)

// submitCapture is a fake Cadence server recording result submissions.
type submitCapture struct {
	t      *testing.T
	bodies []map[string]any
	srv    *httptest.Server
	client *client.Client
}

func newSubmitCapture(t *testing.T) *submitCapture {
	t.Helper()
	c := &submitCapture{t: t}
	c.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		var decoded map[string]any
		if err := json.Unmarshal(body, &decoded); err != nil {
			t.Errorf("result body is not JSON: %v", err)
		}
		c.bodies = append(c.bodies, decoded)
		w.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(c.srv.Close)
	c.client = client.New(c.srv.URL, "tok", 5*time.Second)
	return c
}

func upgradeJob(target string) *report.JobHandoff {
	return &report.JobHandoff{
		ID:      "job-up",
		JobType: "agent_upgrade",
		Params:  json.RawMessage(`{"target_version":"` + target + `"}`),
	}
}

func upgradeCfg() config.Config {
	return config.Config{
		ServerURL:      "https://cadence.test:8443",
		HTTPTimeout:    5 * time.Second,
		EnableUpgrades: true,
	}
}

// swapPrimitive installs a fake install primitive and returns its call
// counter. For gate tests the fake must never run (fail loudly if it does).
func swapPrimitive(t *testing.T, installed bool, err error) *int {
	t.Helper()
	prev := runAgentUpgradeFunc
	calls := 0
	runAgentUpgradeFunc = func(
		context.Context, config.Config, upgrade.Version, upgrade.Artifacts,
	) (bool, error) {
		calls++
		return installed, err
	}
	t.Cleanup(func() { runAgentUpgradeFunc = prev })
	return &calls
}

func swapEligible(t *testing.T, eligible bool) {
	t.Helper()
	prev := executableEligibleFunc
	executableEligibleFunc = func() bool { return eligible }
	t.Cleanup(func() { executableEligibleFunc = prev })
}

func requireRefused(t *testing.T, c *submitCapture, wantSubstr string) {
	t.Helper()
	if len(c.bodies) != 1 {
		t.Fatalf("got %d submissions, want exactly 1", len(c.bodies))
	}
	got := c.bodies[0]
	if got["status"] != "failed" || got["failure_category"] != apterr.CategoryAgentRefused {
		t.Fatalf("submission = %v, want failed/agent_refused", got)
	}
	if !strings.Contains(got["failure_summary"].(string), wantSubstr) {
		t.Fatalf("summary = %v, want substring %q", got["failure_summary"], wantSubstr)
	}
}

func TestUpgradeDisabledRefusesBeforePrimitive(t *testing.T) {
	c := newSubmitCapture(t)
	calls := swapPrimitive(t, true, nil)
	cfg := upgradeCfg()
	cfg.EnableUpgrades = false

	if err := runJob(cfg, c.client, upgradeJob("0.99.0")); err != nil {
		t.Fatal(err)
	}
	requireRefused(t, c, "CADENCE_ENABLE_UPGRADES=false")
	if *calls != 0 {
		t.Fatal("primitive ran despite disabled upgrades")
	}
}

func TestUpgradeMalformedParamsRefusesBeforePrimitive(t *testing.T) {
	c := newSubmitCapture(t)
	calls := swapPrimitive(t, true, nil)
	job := &report.JobHandoff{
		ID: "job-up", JobType: "agent_upgrade",
		Params: json.RawMessage(`{"target_version":"0.99.0","url":"https://evil.test/x"}`),
	}

	if err := runJob(upgradeCfg(), c.client, job); err != nil {
		t.Fatal(err)
	}
	requireRefused(t, c, "params rejected")
	if *calls != 0 {
		t.Fatal("primitive ran despite malformed params")
	}
}

func TestUpgradeOlderRefusesBeforePrimitive(t *testing.T) {
	c := newSubmitCapture(t)
	calls := swapPrimitive(t, true, nil)

	if err := runJob(upgradeCfg(), c.client, upgradeJob("0.1.0")); err != nil {
		t.Fatal(err)
	}
	requireRefused(t, c, "downgrade is refused")
	if *calls != 0 {
		t.Fatal("primitive ran for a downgrade")
	}
}

func TestUpgradeNonCanonicalTargetRefuses(t *testing.T) {
	c := newSubmitCapture(t)
	calls := swapPrimitive(t, true, nil)

	if err := runJob(upgradeCfg(), c.client, upgradeJob("01.2.3")); err != nil {
		t.Fatal(err)
	}
	requireRefused(t, c, "params rejected")
	if *calls != 0 {
		t.Fatal("primitive ran for a non-canonical target")
	}
}

func TestUpgradeEqualSubmitsNothing(t *testing.T) {
	c := newSubmitCapture(t)
	calls := swapPrimitive(t, true, nil)

	// Target == running agent: no-op. No download, no write (the
	// primitive never runs), no result: the next versioned contact
	// proves the job server-side (PR4).
	if err := runJob(upgradeCfg(), c.client, upgradeJob(agentVersion)); err != nil {
		t.Fatal(err)
	}
	if len(c.bodies) != 0 {
		t.Fatalf("got %d submissions, want 0 (no-op must not submit)", len(c.bodies))
	}
	if *calls != 0 {
		t.Fatal("primitive ran for an equal target")
	}
}

func TestUpgradeNonCanonicalPathRefusesBeforePrimitive(t *testing.T) {
	c := newSubmitCapture(t)
	calls := swapPrimitive(t, true, nil)
	// Real gate (no swap): the test binary never runs from /usr/bin.
	swapEligible(t, false)

	if err := runJob(upgradeCfg(), c.client, upgradeJob("0.99.0")); err != nil {
		t.Fatal(err)
	}
	requireRefused(t, c, upgrade.CanonicalPath)
	if *calls != 0 {
		t.Fatal("primitive ran off the canonical path")
	}
}

func TestUpgradeNewerReachesPrimitive(t *testing.T) {
	c := newSubmitCapture(t)
	swapEligible(t, true)
	prev := runAgentUpgradeFunc
	var gotTarget upgrade.Version
	var gotURLs upgrade.Artifacts
	calls := 0
	runAgentUpgradeFunc = func(
		_ context.Context, _ config.Config, target upgrade.Version, urls upgrade.Artifacts,
	) (bool, error) {
		calls++
		gotTarget, gotURLs = target, urls
		return true, nil // installed
	}
	t.Cleanup(func() { runAgentUpgradeFunc = prev })

	if err := runJob(upgradeCfg(), c.client, upgradeJob("0.99.0")); err != nil {
		t.Fatal(err)
	}
	if calls != 1 {
		t.Fatalf("primitive calls = %d, want 1", calls)
	}
	if gotTarget.String() != "0.99.0" {
		t.Fatalf("target = %v", gotTarget)
	}
	if !strings.Contains(gotURLs.Binary, "/agent/v0.99.0/cadence-agent-") {
		t.Fatalf("binary URL = %q", gotURLs.Binary)
	}
	// Installed: the old agent submits nothing at all.
	if len(c.bodies) != 0 {
		t.Fatalf("got %d submissions after install, want 0", len(c.bodies))
	}
}

func TestUpgradePrimitiveFailureSubmitsCategory(t *testing.T) {
	c := newSubmitCapture(t)
	swapEligible(t, true)
	calls := swapPrimitive(t, false, &upgrade.Error{
		Category: upgrade.CategoryVerificationFailed,
		Err:      context.DeadlineExceeded,
	})

	if err := runJob(upgradeCfg(), c.client, upgradeJob("0.99.0")); err != nil {
		t.Fatal(err)
	}
	if *calls != 1 {
		t.Fatalf("primitive calls = %d, want 1", *calls)
	}
	if len(c.bodies) != 1 {
		t.Fatalf("got %d submissions, want 1", len(c.bodies))
	}
	got := c.bodies[0]
	if got["status"] != "failed" ||
		got["failure_category"] != upgrade.CategoryVerificationFailed {
		t.Fatalf("submission = %v", got)
	}
}
