package healthcheck

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"reflect"
	"strings"
	"syscall"
	"testing"
	"time"
)

type commandResult struct {
	output string
	err    error
}

type fakeRunner struct {
	results []commandResult
	calls   []string
}

func (r *fakeRunner) Run(_ context.Context, name string, args ...string) (string, error) {
	r.calls = append(r.calls, strings.Join(append([]string{name}, args...), " "))
	if len(r.results) == 0 {
		return "", nil
	}
	result := r.results[0]
	r.results = r.results[1:]
	return result.output, result.err
}

func TestNewPhaseUsesWorstStatus(t *testing.T) {
	tests := []struct {
		name   string
		checks []Result
		want   Status
	}{
		{name: "empty", want: StatusUnknown},
		{name: "passed", checks: []Result{{Status: StatusPassed}}, want: StatusPassed},
		{name: "warning", checks: []Result{{Status: StatusPassed}, {Status: StatusWarning}}, want: StatusWarning},
		{name: "unknown", checks: []Result{{Status: StatusWarning}, {Status: StatusUnknown}}, want: StatusUnknown},
		{name: "failed", checks: []Result{{Status: StatusUnknown}, {Status: StatusFailed}}, want: StatusFailed},
		{name: "skipped after failure", checks: []Result{{Status: StatusFailed}, {Status: StatusSkipped}}, want: StatusFailed},
		{name: "all skipped", checks: []Result{{Status: StatusSkipped}}, want: StatusUnknown},
		{name: "invalid", checks: []Result{{Status: Status("invalid")}}, want: StatusUnknown},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := NewPhase(tc.checks...).Status; got != tc.want {
				t.Fatalf("status = %q, want %q", got, tc.want)
			}
		})
	}
}

func TestHealthFromPostChecks(t *testing.T) {
	tests := []struct {
		status Status
		want   HealthStatus
	}{
		{StatusPassed, HealthHealthy},
		{StatusWarning, HealthDegraded},
		{StatusFailed, HealthUnhealthy},
		{StatusUnknown, HealthUnknown},
	}
	for _, tc := range tests {
		if got := HealthFromPostChecks([]Result{{Status: tc.status}}); got != tc.want {
			t.Errorf("status %q: health = %q, want %q", tc.status, got, tc.want)
		}
	}
}

func TestDiskSpaceDeduplicatesFilesystemsAndUsesHighestThreshold(t *testing.T) {
	targets := []DiskTarget{
		{Path: "/var", MinimumAvailableBytes: 1000},
		{Path: "/boot", MinimumAvailableBytes: 200},
		{Path: "/boot/efi", MinimumAvailableBytes: 200},
	}
	probe := func(path string) (Filesystem, error) {
		switch path {
		case "/var", "/boot":
			return Filesystem{Device: 1, AvailableBytes: 900}, nil
		case "/boot/efi":
			return Filesystem{Device: 2, AvailableBytes: 300}, nil
		default:
			return Filesystem{}, os.ErrNotExist
		}
	}

	got := DiskSpace(targets, probe)

	if got.Status != StatusFailed {
		t.Fatalf("status = %q, want failed", got.Status)
	}
	if len(got.Details.Filesystems) != 2 {
		t.Fatalf("filesystems = %#v, want two deduplicated entries", got.Details.Filesystems)
	}
	var shared *FilesystemResult
	for i := range got.Details.Filesystems {
		if len(got.Details.Filesystems[i].Paths) == 2 {
			shared = &got.Details.Filesystems[i]
		}
	}
	if shared == nil || shared.MinimumAvailableBytes != 1000 {
		t.Fatalf("shared filesystem = %#v, want threshold 1000", shared)
	}
}

func TestDiskSpaceIgnoresMissingOptionalPath(t *testing.T) {
	got := DiskSpace([]DiskTarget{
		{Path: "/var", MinimumAvailableBytes: 100},
		{Path: "/boot/efi", MinimumAvailableBytes: 20},
	}, func(path string) (Filesystem, error) {
		if path == "/boot/efi" {
			return Filesystem{}, os.ErrNotExist
		}
		return Filesystem{Device: 1, AvailableBytes: 200}, nil
	})

	if got.Status != StatusPassed {
		t.Fatalf("status = %q, want passed: %#v", got.Status, got)
	}
}

func TestDiskSpaceReportsProbeFailureAsUnknown(t *testing.T) {
	got := DiskSpace([]DiskTarget{{Path: "/var", MinimumAvailableBytes: 100}},
		func(string) (Filesystem, error) { return Filesystem{}, errors.New("denied") })

	if got.Status != StatusUnknown || len(got.Details.Problems) != 1 {
		t.Fatalf("unexpected result: %#v", got)
	}
}

func TestPackageManagerLocksWaitsUntilClear(t *testing.T) {
	calls := 0
	probe := func([]string) ([]HeldLock, error) {
		calls++
		if calls < 3 {
			return []HeldLock{{Path: "/lock", PID: 42}}, nil
		}
		return nil, nil
	}

	got := PackageManagerLocks(context.Background(), []string{"/lock"}, time.Millisecond, probe)

	if got.Status != StatusPassed || calls != 3 {
		t.Fatalf("result = %#v, calls = %d", got, calls)
	}
}

func TestPackageManagerLocksFailsAtDeadline(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	probe := func([]string) ([]HeldLock, error) {
		return []HeldLock{{Path: "/lock", PID: 42}}, nil
	}

	got := PackageManagerLocks(ctx, []string{"/lock"}, time.Millisecond, probe)

	if got.Status != StatusFailed || !reflect.DeepEqual(got.Details.Locks, []HeldLock{{Path: "/lock", PID: 42}}) {
		t.Fatalf("unexpected result: %#v", got)
	}
}

func TestPackageManagerLocksReportsProbeFailure(t *testing.T) {
	got := PackageManagerLocks(context.Background(), nil, time.Millisecond,
		func([]string) ([]HeldLock, error) { return nil, errors.New("denied") })

	if got.Status != StatusUnknown || len(got.Details.Problems) != 1 {
		t.Fatalf("unexpected result: %#v", got)
	}
}

func TestProbePackageManagerLocksFindsAnExternalOwner(t *testing.T) {
	path := t.TempDir() + "/apt.lock"
	if err := os.WriteFile(path, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command(os.Args[0], "-test.run=TestPackageLockHelperProcess")
	cmd.Env = append(os.Environ(), "CADENCE_TEST_LOCK_PATH="+path)
	stdin, err := cmd.StdinPipe()
	if err != nil {
		t.Fatal(err)
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_ = stdin.Close()
		if cmd.ProcessState == nil {
			_ = cmd.Process.Kill()
			_ = cmd.Wait()
		}
	})
	ready := bufio.NewScanner(stdout)
	if !ready.Scan() || ready.Text() != "ready" {
		t.Fatalf("lock helper did not become ready: %q", ready.Text())
	}

	held, err := ProbePackageManagerLocks([]string{path})
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(held, []HeldLock{{Path: path, PID: cmd.Process.Pid}}) {
		t.Fatalf("held locks = %#v, want helper PID %d", held, cmd.Process.Pid)
	}

	if err := stdin.Close(); err != nil {
		t.Fatal(err)
	}
	if err := cmd.Wait(); err != nil {
		t.Fatal(err)
	}
	held, err = ProbePackageManagerLocks([]string{path})
	if err != nil || len(held) != 0 {
		t.Fatalf("lock remained after helper exit: held=%#v err=%v", held, err)
	}
}

func TestPackageLockHelperProcess(t *testing.T) {
	path := os.Getenv("CADENCE_TEST_LOCK_PATH")
	if path == "" {
		return
	}
	file, err := os.OpenFile(path, os.O_RDWR, 0)
	if err != nil {
		t.Fatal(err)
	}
	lock := syscall.Flock_t{Type: syscall.F_WRLCK, Whence: 0, Start: 0, Len: 0}
	if err := syscall.FcntlFlock(file.Fd(), syscall.F_SETLK, &lock); err != nil {
		t.Fatal(err)
	}
	fmt.Println("ready")
	_, _ = io.Copy(io.Discard, os.Stdin)
	if err := file.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestDPKGAuditTreatsOutputAsFailure(t *testing.T) {
	runner := &fakeRunner{results: []commandResult{{output: "The following packages are only half configured:\n broken\n"}}}

	got := DPKGAudit(context.Background(), runner)

	if got.Status != StatusFailed || len(got.Details.Problems) != 2 {
		t.Fatalf("unexpected result: %#v", got)
	}
	if !reflect.DeepEqual(runner.calls, []string{"dpkg --audit"}) {
		t.Fatalf("calls = %#v", runner.calls)
	}
}

func TestAPTDependencies(t *testing.T) {
	runner := &fakeRunner{results: []commandResult{{output: "E: Unmet dependencies", err: errors.New("exit status 100")}}}

	got := APTDependencies(context.Background(), runner)

	if got.Status != StatusFailed || got.Summary != "E: Unmet dependencies" {
		t.Fatalf("unexpected result: %#v", got)
	}
}

func TestPackageIndexesUsesStrictMode(t *testing.T) {
	runner := &fakeRunner{}

	got := PackageIndexes(context.Background(), runner)

	if got.Status != StatusPassed || got.Details.StrictMode == nil || !*got.Details.StrictMode {
		t.Fatalf("unexpected result: %#v", got)
	}
	if !reflect.DeepEqual(runner.calls, []string{"apt-get --error-on=any update"}) {
		t.Fatalf("calls = %#v", runner.calls)
	}
}

func TestPackageIndexesFallsBackWhenStrictModeIsUnsupported(t *testing.T) {
	runner := &fakeRunner{results: []commandResult{
		{output: "E: Command line option --error-on=any is not understood", err: errors.New("exit status 100")},
		{},
	}}

	got := PackageIndexes(context.Background(), runner)

	if got.Status != StatusWarning || got.Details.StrictMode == nil || *got.Details.StrictMode {
		t.Fatalf("unexpected result: %#v", got)
	}
	if !reflect.DeepEqual(runner.calls, []string{"apt-get --error-on=any update", "apt-get update"}) {
		t.Fatalf("calls = %#v", runner.calls)
	}
}

func TestPackageIndexesDoesNotHideARepositoryFailure(t *testing.T) {
	runner := &fakeRunner{results: []commandResult{{output: "E: Failed to fetch repository metadata", err: errors.New("exit status 100")}}}

	got := PackageIndexes(context.Background(), runner)

	if got.Status != StatusFailed || len(runner.calls) != 1 {
		t.Fatalf("unexpected result: %#v, calls: %#v", got, runner.calls)
	}
}

func TestFailedServicesParsesAndSortsUnits(t *testing.T) {
	runner := &fakeRunner{results: []commandResult{{output: "z.service loaded failed failed Z\na.service loaded failed failed A\na.service loaded failed failed A\n"}}}

	got := FailedServices(context.Background(), runner)

	if got.Status != StatusWarning || !reflect.DeepEqual(got.Details.Services, []string{"a.service", "z.service"}) {
		t.Fatalf("unexpected result: %#v", got)
	}
}

func TestFailedServicesAfterSeparatesNewAndExistingFailures(t *testing.T) {
	runner := &fakeRunner{results: []commandResult{{output: "old.service loaded failed failed Old\nnew.service loaded failed failed New\n"}}}

	got := FailedServicesAfter(context.Background(), runner, []string{"old.service", "gone.service"})

	if got.Status != StatusFailed {
		t.Fatalf("status = %q, want failed", got.Status)
	}
	if !reflect.DeepEqual(got.Details.NewServices, []string{"new.service"}) ||
		!reflect.DeepEqual(got.Details.ExistingServices, []string{"old.service"}) {
		t.Fatalf("unexpected service delta: %#v", got.Details)
	}
}

func TestFailedServicesAfterKeepsExistingFailuresAsWarning(t *testing.T) {
	runner := &fakeRunner{results: []commandResult{{output: "old.service loaded failed failed Old\n"}}}

	got := FailedServicesAfter(context.Background(), runner, []string{"old.service"})

	if got.Status != StatusWarning {
		t.Fatalf("status = %q, want warning", got.Status)
	}
}

func TestRebootRequired(t *testing.T) {
	required := RebootRequired(true)
	notRequired := RebootRequired(false)

	if required.Status != StatusWarning || required.Details.Required == nil || !*required.Details.Required {
		t.Fatalf("unexpected required result: %#v", required)
	}
	if notRequired.Status != StatusPassed || notRequired.Details.Required == nil || *notRequired.Details.Required {
		t.Fatalf("unexpected not-required result: %#v", notRequired)
	}
}

func TestEvidenceIsBounded(t *testing.T) {
	var output strings.Builder
	for i := 0; i < maxEvidenceItems+20; i++ {
		fmt.Fprintf(&output, "%03d %s\n", i, strings.Repeat("x", maxSummaryBytes+20))
	}
	runner := &fakeRunner{results: []commandResult{{output: output.String()}}}

	got := DPKGAudit(context.Background(), runner)

	if len(got.Details.Problems) != maxEvidenceItems {
		t.Fatalf("problem count = %d, want %d", len(got.Details.Problems), maxEvidenceItems)
	}
	if len(got.Details.Problems[0]) != maxSummaryBytes {
		t.Fatalf("evidence line not bounded to %d bytes: got %d",
			maxSummaryBytes, len(got.Details.Problems[0]))
	}
}
