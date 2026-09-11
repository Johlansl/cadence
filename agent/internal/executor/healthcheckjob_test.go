package executor

import (
	"context"
	"reflect"
	"testing"

	"cadence/agent/internal/healthcheck"
)

func TestRunHealthCheckReportsHealthyWithCleanState(t *testing.T) {
	runner := &scriptedCheckRunner{results: make(map[string][]checkCommandResult)}
	res := RunHealthCheck(context.Background(), checkOptions(runner))

	if res.Status != "succeeded" || res.ExitCode != 0 {
		t.Fatalf("status=%q exit=%d, want succeeded/0", res.Status, res.ExitCode)
	}
	if res.PostChecks == nil || res.PostChecks.Status != healthcheck.StatusPassed {
		t.Fatalf("post-checks = %#v, want passed", res.PostChecks)
	}
	if res.HealthStatus != healthcheck.HealthHealthy {
		t.Fatalf("health = %q, want healthy", res.HealthStatus)
	}
	if res.RebootRequired {
		t.Fatal("want RebootRequired = false")
	}
	want := []string{
		"dpkg --audit",
		"apt-get check",
		"systemctl --failed --no-legend --plain --no-pager",
	}
	if !reflect.DeepEqual(runner.calls, want) {
		t.Fatalf("check calls = %#v, want %#v", runner.calls, want)
	}
}

func TestRunHealthCheckDoesNotBlockOnAnExistingFailedService(t *testing.T) {
	runner := &scriptedCheckRunner{results: map[string][]checkCommandResult{
		"systemctl --failed --no-legend --plain --no-pager": {
			{output: "foo.service loaded failed failed Foo\n"},
		},
	}}
	res := RunHealthCheck(context.Background(), checkOptions(runner))

	if res.PostChecks.Status != healthcheck.StatusWarning {
		t.Fatalf("post-checks status = %q, want warning (non-blocking)", res.PostChecks.Status)
	}
	if res.HealthStatus != healthcheck.HealthDegraded {
		t.Fatalf("health = %q, want degraded", res.HealthStatus)
	}
}

func TestRunHealthCheckReportsRebootRequired(t *testing.T) {
	runner := &scriptedCheckRunner{results: make(map[string][]checkCommandResult)}
	options := checkOptions(runner)
	options.RebootProbe = func() bool { return true }

	res := RunHealthCheck(context.Background(), options)

	if !res.RebootRequired {
		t.Fatal("want RebootRequired = true")
	}
	if res.HealthStatus != healthcheck.HealthDegraded {
		t.Fatalf("health = %q, want degraded", res.HealthStatus)
	}
}
