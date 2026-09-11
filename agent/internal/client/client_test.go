package client

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"

	"cadence/agent/internal/healthcheck"
	"cadence/agent/internal/report"
)

// verifySignedHeaders independently recomputes the expected signed-request
// headers (mirroring app.api.deps._auth_signed) and fails the test if the
// request the client actually sent does not match -- a real cross-check of
// the wire format, not just "some headers are present".
func verifySignedHeaders(t *testing.T, r *http.Request, token string, body []byte) {
	t.Helper()
	if got := r.Header.Get("Authorization"); got != "" {
		t.Errorf("Authorization should not be sent at all, got %q", got)
	}
	gotHash := r.Header.Get("X-Cadence-Token-Hash")
	sum := sha256.Sum256([]byte(token))
	if want := hex.EncodeToString(sum[:]); gotHash != want {
		t.Errorf("X-Cadence-Token-Hash = %q, want %q", gotHash, want)
	}
	tsHdr := r.Header.Get("X-Cadence-Timestamp")
	ts, err := strconv.ParseInt(tsHdr, 10, 64)
	if err != nil {
		t.Fatalf("X-Cadence-Timestamp = %q, not an integer: %v", tsHdr, err)
	}
	if skew := time.Now().Unix() - ts; skew < -5 || skew > 5 {
		t.Errorf("X-Cadence-Timestamp skew = %ds, want within 5s of now", skew)
	}
	bodySum := sha256.Sum256(body)
	canonical := tsHdr + "\n" + r.Method + "\n" + r.URL.Path + "\n" + hex.EncodeToString(bodySum[:])
	mac := hmac.New(sha256.New, []byte(token))
	mac.Write([]byte(canonical))
	want := hex.EncodeToString(mac.Sum(nil))
	if got := r.Header.Get("X-Cadence-Signature"); got != want {
		t.Errorf("X-Cadence-Signature = %q, want %q (canonical %q)", got, want, canonical)
	}
}

func TestSendReportParsesJob(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/api/v1/reports" {
			t.Errorf("unexpected request %s %s", r.Method, r.URL.Path)
		}
		body, _ := io.ReadAll(r.Body)
		verifySignedHeaders(t, r, "tok", body)
		if strings.Contains(string(body), "claim_job") {
			t.Errorf("a regular report must omit claim_job: %s", body)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"host_id":"h","job":{"id":"job-1","job_type":"apt_upgrade","params":{}}}`)
	}))
	defer srv.Close()

	job, err := New(srv.URL, "tok", 5*time.Second).SendReport(context.Background(), report.Report{})
	if err != nil {
		t.Fatal(err)
	}
	if job == nil || job.ID != "job-1" || job.JobType != "apt_upgrade" {
		t.Fatalf("job = %+v", job)
	}
}

func TestSendPostJobReportDisablesJobClaim(t *testing.T) {
	var got map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		verifySignedHeaders(t, r, "tok", body)
		if err := json.Unmarshal(body, &got); err != nil {
			t.Fatal(err)
		}
		_, _ = io.WriteString(w, `{"host_id":"h","job":null}`)
	}))
	defer srv.Close()

	err := New(srv.URL, "tok", 5*time.Second).SendPostJobReport(
		context.Background(), report.Report{},
	)
	if err != nil {
		t.Fatal(err)
	}
	if claim, ok := got["claim_job"].(bool); !ok || claim {
		t.Fatalf("claim_job = %#v, want false", got["claim_job"])
	}
}

func TestSendReportNoJob(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = io.WriteString(w, `{"host_id":"h","job":null}`)
	}))
	defer srv.Close()

	job, err := New(srv.URL, "tok", 5*time.Second).SendReport(context.Background(), report.Report{})
	if err != nil {
		t.Fatal(err)
	}
	if job != nil {
		t.Fatalf("expected nil job, got %+v", job)
	}
}

func TestSendReportServerError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = io.WriteString(w, `{"detail":"invalid token"}`)
	}))
	defer srv.Close()

	if _, err := New(srv.URL, "tok", 5*time.Second).SendReport(context.Background(), report.Report{}); err == nil {
		t.Fatal("expected an error on HTTP 401")
	}
}

func TestClaimNextJob(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/api/v1/agent/next-job" {
			t.Errorf("unexpected request %s %s", r.Method, r.URL.Path)
		}
		_, _ = io.WriteString(w, `{"job":{"id":"job-9","job_type":"apt_upgrade","params":{}}}`)
	}))
	defer srv.Close()

	job, err := New(srv.URL, "tok", 5*time.Second).ClaimNextJob(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if job == nil || job.ID != "job-9" {
		t.Fatalf("job = %+v", job)
	}
}

func TestClaimNextJobEmpty(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = io.WriteString(w, `{"job":null}`)
	}))
	defer srv.Close()

	job, err := New(srv.URL, "tok", 5*time.Second).ClaimNextJob(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if job != nil {
		t.Fatalf("expected nil, got %+v", job)
	}
}

func TestClaimHealthCheckJob(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/api/v1/agent/health-check-job" {
			t.Errorf("unexpected request %s %s", r.Method, r.URL.Path)
		}
		_, _ = io.WriteString(w, `{"job":{"id":"job-9","job_type":"health_check","params":{}}}`)
	}))
	defer srv.Close()

	job, err := New(srv.URL, "tok", 5*time.Second).ClaimHealthCheckJob(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if job == nil || job.ID != "job-9" {
		t.Fatalf("job = %+v", job)
	}
}

func TestClaimHealthCheckJobEmpty(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = io.WriteString(w, `{"job":null}`)
	}))
	defer srv.Close()

	job, err := New(srv.URL, "tok", 5*time.Second).ClaimHealthCheckJob(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if job != nil {
		t.Fatalf("expected nil, got %+v", job)
	}
}

func TestClaimHealthCheckJobServerError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = io.WriteString(w, "boom")
	}))
	defer srv.Close()

	if _, err := New(srv.URL, "tok", 5*time.Second).ClaimHealthCheckJob(context.Background()); err == nil {
		t.Fatal("expected an error on HTTP 500")
	}
}

func TestSubmitJobResult(t *testing.T) {
	var gotPath, gotRaw string
	var gotBody JobResult
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		raw, _ := io.ReadAll(r.Body)
		gotRaw = string(raw)
		verifySignedHeaders(t, r, "tok", raw)
		_ = json.Unmarshal(raw, &gotBody)
		_, _ = io.WriteString(w, `{}`)
	}))
	defer srv.Close()

	err := New(srv.URL, "tok", 5*time.Second).SubmitJobResult(context.Background(), "job-1", JobResult{
		Status: "succeeded", ExitCode: 0, Log: "done", RebootRequired: true,
	})
	if err != nil {
		t.Fatal(err)
	}
	if gotPath != "/api/v1/jobs/job-1/result" {
		t.Errorf("path = %q", gotPath)
	}
	if gotBody.Status != "succeeded" || !gotBody.RebootRequired || gotBody.Log != "done" {
		t.Errorf("body = %+v", gotBody)
	}
	if !strings.Contains(gotRaw, `"status":"succeeded"`) || strings.Contains(gotRaw, "failure_category") {
		t.Errorf("a success result must not carry failure_category: %s", gotRaw)
	}
}

func TestSubmitJobResultCarriesFailureClassification(t *testing.T) {
	var gotBody JobResult
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(raw, &gotBody)
		_, _ = io.WriteString(w, `{}`)
	}))
	defer srv.Close()

	err := New(srv.URL, "tok", 5*time.Second).SubmitJobResult(context.Background(), "j", JobResult{
		Status: "failed", ExitCode: 100, Log: "boom",
		FailureCategory: "dpkg_error", FailureSummary: "E: Sub-process /usr/bin/dpkg returned an error code (1)",
	})
	if err != nil {
		t.Fatal(err)
	}
	if gotBody.FailureCategory != "dpkg_error" || gotBody.FailureSummary == "" {
		t.Errorf("body = %+v", gotBody)
	}
}

func TestSubmitJobResultCarriesUpgradeHealthSeparatelyFromStatus(t *testing.T) {
	var gotBody JobResult
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(raw, &gotBody)
		_, _ = io.WriteString(w, `{}`)
	}))
	defer srv.Close()

	pre := healthcheck.NewPhase(healthcheck.Result{
		Name: healthcheck.CheckDiskSpace, Status: healthcheck.StatusPassed, Summary: "enough space",
	})
	post := healthcheck.NewPhase(healthcheck.Result{
		Name: healthcheck.CheckFailedServices, Status: healthcheck.StatusFailed, Summary: "one service newly failed",
	})
	err := New(srv.URL, "tok", 5*time.Second).SubmitJobResult(context.Background(), "j", JobResult{
		Status: "succeeded", ExitCode: 0,
		PreChecks: &pre, PostChecks: &post, HealthStatus: healthcheck.HealthUnhealthy,
	})
	if err != nil {
		t.Fatal(err)
	}
	if gotBody.Status != "succeeded" || gotBody.HealthStatus != healthcheck.HealthUnhealthy {
		t.Fatalf("action and health were not kept separate: %#v", gotBody)
	}
	if gotBody.PreChecks == nil || gotBody.PostChecks == nil || gotBody.PostChecks.Status != healthcheck.StatusFailed {
		t.Fatalf("check phases did not round-trip: %#v", gotBody)
	}
}

func TestSubmitJobResultHeldConflictsIsNeverOmitted(t *testing.T) {
	var gotRaw string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		gotRaw = string(raw)
		_, _ = io.WriteString(w, `{}`)
	}))
	defer srv.Close()

	// Unlike FailureCategory/FailureSummary, HeldConflicts has no omitempty:
	// nil must serialize as JSON null (not applicable), never be dropped, so
	// the server can tell it apart from an empty list (checked, found nothing).
	err := New(srv.URL, "tok", 5*time.Second).SubmitJobResult(context.Background(), "j", JobResult{
		Status: "succeeded", ExitCode: 0,
	})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(gotRaw, `"held_conflicts":null`) {
		t.Errorf("expected an explicit null held_conflicts, got: %s", gotRaw)
	}
	if !strings.Contains(gotRaw, `"held_packages":null`) {
		t.Errorf("expected an explicit null held_packages, got: %s", gotRaw)
	}
}

func TestSubmitJobResultHeldPackagesRoundTrips(t *testing.T) {
	var gotBody JobResult
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(raw, &gotBody)
		_, _ = io.WriteString(w, `{}`)
	}))
	defer srv.Close()

	err := New(srv.URL, "tok", 5*time.Second).SubmitJobResult(context.Background(), "j", JobResult{
		Status: "succeeded", ExitCode: 0,
		HeldPackages: []string{"docker-ce", "postgresql-14"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(gotBody.HeldPackages) != 2 {
		t.Errorf("HeldPackages = %v", gotBody.HeldPackages)
	}
}

func TestDoRetriesOn5xxThenSucceeds(t *testing.T) {
	old := retryWaits
	retryWaits = []time.Duration{0, 0, 0}
	defer func() { retryWaits = old }()

	var calls int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		calls++
		if calls < 3 {
			w.WriteHeader(http.StatusBadGateway)
			return
		}
		_, _ = io.WriteString(w, `{"job":null}`)
	}))
	defer srv.Close()

	if _, err := New(srv.URL, "tok", time.Second).ClaimNextJob(context.Background()); err != nil {
		t.Fatal(err)
	}
	if calls != 3 {
		t.Fatalf("calls = %d, want 3 (two retries)", calls)
	}
}

func TestDoDoesNotRetry4xx(t *testing.T) {
	old := retryWaits
	retryWaits = []time.Duration{0, 0, 0}
	defer func() { retryWaits = old }()

	var calls int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		calls++
		w.WriteHeader(http.StatusConflict)
		_, _ = io.WriteString(w, `{"detail":"not running"}`)
	}))
	defer srv.Close()

	_ = New(srv.URL, "tok", time.Second).SubmitJobResult(context.Background(), "j", JobResult{Status: "succeeded"})
	if calls != 1 {
		t.Fatalf("calls = %d, want 1 (no retry on 4xx)", calls)
	}
}

func TestDoSignsEachRetryAttemptIndependently(t *testing.T) {
	old := retryWaits
	retryWaits = []time.Duration{0, 0, 0}
	defer func() { retryWaits = old }()

	var calls int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		body, _ := io.ReadAll(r.Body)
		verifySignedHeaders(t, r, "tok", body) // every attempt must verify on its own
		if calls < 3 {
			w.WriteHeader(http.StatusBadGateway)
			return
		}
		_, _ = io.WriteString(w, `{"job":null}`)
	}))
	defer srv.Close()

	if _, err := New(srv.URL, "tok", time.Second).ClaimNextJob(context.Background()); err != nil {
		t.Fatal(err)
	}
	if calls != 3 {
		t.Fatalf("calls = %d, want 3", calls)
	}
}

func TestDoGivesUpAfterRetries(t *testing.T) {
	old := retryWaits
	retryWaits = []time.Duration{0, 0, 0}
	defer func() { retryWaits = old }()

	var calls int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		calls++
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer srv.Close()

	if _, err := New(srv.URL, "tok", time.Second).ClaimNextJob(context.Background()); err == nil {
		t.Fatal("expected an error after exhausting retries")
	}
	if calls != len(retryWaits) {
		t.Fatalf("calls = %d, want %d", calls, len(retryWaits))
	}
}
