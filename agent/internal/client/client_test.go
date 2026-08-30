package client

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"cadence/agent/internal/report"
)

func TestSendReportParsesJob(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/api/v1/reports" {
			t.Errorf("unexpected request %s %s", r.Method, r.URL.Path)
		}
		if got := r.Header.Get("Authorization"); got != "Bearer tok" {
			t.Errorf("Authorization = %q", got)
		}
		w.Header().Set("Content-Type", "application/json")
		io.WriteString(w, `{"host_id":"h","job":{"id":"job-1","job_type":"apt_upgrade","params":{}}}`)
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

func TestSendReportNoJob(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		io.WriteString(w, `{"host_id":"h","job":null}`)
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
		io.WriteString(w, `{"detail":"invalid token"}`)
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
		io.WriteString(w, `{"job":{"id":"job-9","job_type":"apt_upgrade","params":{}}}`)
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
		io.WriteString(w, `{"job":null}`)
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

func TestSubmitJobResult(t *testing.T) {
	var gotPath, gotAuth string
	var gotBody JobResult
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		gotAuth = r.Header.Get("Authorization")
		_ = json.NewDecoder(r.Body).Decode(&gotBody)
		io.WriteString(w, `{}`)
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
	if gotAuth != "Bearer tok" {
		t.Errorf("Authorization = %q", gotAuth)
	}
	if gotBody.Status != "succeeded" || !gotBody.RebootRequired || gotBody.Log != "done" {
		t.Errorf("body = %+v", gotBody)
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
		io.WriteString(w, `{"job":null}`)
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
		io.WriteString(w, `{"detail":"not running"}`)
	}))
	defer srv.Close()

	_ = New(srv.URL, "tok", time.Second).SubmitJobResult(context.Background(), "j", JobResult{Status: "succeeded"})
	if calls != 1 {
		t.Fatalf("calls = %d, want 1 (no retry on 4xx)", calls)
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
