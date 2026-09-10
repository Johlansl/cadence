package executor

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"testing"
	"time"

	"cadence/agent/internal/apterr"
	"cadence/agent/internal/healthcheck"
)

type checkCommandResult struct {
	output string
	err    error
}

type scriptedCheckRunner struct {
	results map[string][]checkCommandResult
	calls   []string
}

func (r *scriptedCheckRunner) Run(_ context.Context, name string, args ...string) (string, error) {
	call := strings.Join(append([]string{name}, args...), " ")
	r.calls = append(r.calls, call)
	results := r.results[call]
	if len(results) == 0 {
		return "", nil
	}
	result := results[0]
	r.results[call] = results[1:]
	return result.output, result.err
}

func checkOptions(runner healthcheck.Runner) CheckOptions {
	options := DefaultCheckOptions()
	options.Runner = runner
	options.DiskProbe = func(string) (healthcheck.Filesystem, error) {
		return healthcheck.Filesystem{Device: 1, AvailableBytes: 2 * healthcheck.DefaultMinimumAvailableBytes}, nil
	}
	options.LockProbe = func([]string) ([]healthcheck.HeldLock, error) { return nil, nil }
	options.RebootProbe = func() bool { return false }
	options.LockPollingPeriod = time.Millisecond
	return options
}

func fakeSuccessfulUpgrade(t *testing.T) string {
	t.Helper()
	dir := fakePATH(t)
	fakeBin(t, dir, "apt-get", `case "$*" in *dist-upgrade*) echo "upgrade complete";; esac; exit 0`)
	fakeBin(t, dir, "dpkg", `exit 0`)
	fakeBin(t, dir, "apt-mark", `exit 0`)
	return dir
}

// fakeBin writes an executable shell script named `name` into `dir`.
func fakeBin(t *testing.T, dir, name, body string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, name), []byte("#!/bin/sh\n"+body), 0o755); err != nil {
		t.Fatal(err)
	}
}

// fakePATH prepends a fresh temp dir to PATH and returns it.
func fakePATH(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
	return dir
}

func noRetryWait(t *testing.T) {
	t.Helper()
	old := aptLockRetryWaits
	aptLockRetryWaits = []time.Duration{0, 0, 0, 0}
	t.Cleanup(func() { aptLockRetryWaits = old })
}

func readCount(t *testing.T, path string) int {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		return 0
	}
	n, _ := strconv.Atoi(strings.TrimSpace(string(b)))
	return n
}

func TestRunAptUpgradeReportsHealthyAfterACleanAction(t *testing.T) {
	fakeSuccessfulUpgrade(t)
	runner := &scriptedCheckRunner{results: make(map[string][]checkCommandResult)}

	res := RunAptUpgrade(context.Background(), nil, nil, checkOptions(runner))

	if res.Err != nil || res.ExitCode != 0 {
		t.Fatalf("action failed: exit=%d err=%v log=%q", res.ExitCode, res.Err, res.Log)
	}
	if res.PreChecks == nil || res.PreChecks.Status != healthcheck.StatusPassed {
		t.Fatalf("pre-checks = %#v, want passed", res.PreChecks)
	}
	if res.PostChecks == nil || res.PostChecks.Status != healthcheck.StatusPassed {
		t.Fatalf("post-checks = %#v, want passed", res.PostChecks)
	}
	if res.HealthStatus != healthcheck.HealthHealthy {
		t.Fatalf("health = %q, want healthy", res.HealthStatus)
	}
	if !reflect.DeepEqual(runner.calls, []string{
		"dpkg --audit",
		"apt-get check",
		"systemctl --failed --no-legend --plain --no-pager",
		"apt-get --error-on=any update",
		"dpkg --audit",
		"apt-get check",
		"systemctl --failed --no-legend --plain --no-pager",
	}) {
		t.Fatalf("check calls = %#v", runner.calls)
	}
	if !strings.Contains(res.Log, "pre-check package_indexes: passed") ||
		!strings.Contains(res.Log, "post-check failed_services: passed") {
		t.Fatalf("structured checks missing from log: %q", res.Log)
	}
}

func TestRunAptUpgradeBlocksBeforeMutationWhenDiskIsLow(t *testing.T) {
	dir := fakePATH(t)
	marker := filepath.Join(t.TempDir(), "apt-ran")
	fakeBin(t, dir, "apt-get", `: > `+marker+`; exit 0`)
	runner := &scriptedCheckRunner{results: make(map[string][]checkCommandResult)}
	options := checkOptions(runner)
	options.DiskProbe = func(string) (healthcheck.Filesystem, error) {
		return healthcheck.Filesystem{Device: 1, AvailableBytes: 1}, nil
	}

	res := RunAptUpgrade(context.Background(), nil, nil, options)

	if !errors.Is(res.Err, errPreChecks) || res.FailureCategory != apterr.CategoryDiskFull {
		t.Fatalf("unexpected blocked result: %#v", res)
	}
	if _, err := os.Stat(marker); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("apt ran despite a blocking pre-check: %v", err)
	}
	if len(runner.calls) != 0 {
		t.Fatalf("later command checks ran after disk failure: %#v", runner.calls)
	}
	if res.PostChecks != nil || res.HealthStatus != healthcheck.HealthUnknown {
		t.Fatalf("post-check state = %#v, health = %q", res.PostChecks, res.HealthStatus)
	}
	if res.PreChecks == nil || len(res.PreChecks.Checks) != 6 ||
		res.PreChecks.Checks[1].Status != healthcheck.StatusSkipped {
		t.Fatalf("pre-check shape = %#v", res.PreChecks)
	}
}

func TestRunAptUpgradeKeepsActionSuccessSeparateFromUnhealthyPostChecks(t *testing.T) {
	fakeSuccessfulUpgrade(t)
	systemctl := "systemctl --failed --no-legend --plain --no-pager"
	runner := &scriptedCheckRunner{results: map[string][]checkCommandResult{
		systemctl: {
			{output: "old.service loaded failed failed Old\n"},
			{output: "new.service loaded failed failed New\nold.service loaded failed failed Old\n"},
		},
	}}

	res := RunAptUpgrade(context.Background(), nil, nil, checkOptions(runner))

	if res.Err != nil || res.ExitCode != 0 {
		t.Fatalf("apt action should have succeeded: %#v", res)
	}
	if res.HealthStatus != healthcheck.HealthUnhealthy || res.PostChecks == nil ||
		res.PostChecks.Status != healthcheck.StatusFailed {
		t.Fatalf("post-upgrade health was not unhealthy: %#v", res)
	}
	services := res.PostChecks.Checks[3]
	if !reflect.DeepEqual(services.Details.NewServices, []string{"new.service"}) ||
		!reflect.DeepEqual(services.Details.ExistingServices, []string{"old.service"}) {
		t.Fatalf("service delta = %#v", services.Details)
	}
}

func TestRunAptUpgradeRunsPostChecksAfterActionFailure(t *testing.T) {
	noRetryWait(t)
	dir := fakePATH(t)
	fakeBin(t, dir, "apt-get", `echo "E: package failed" >&2; exit 100`)
	fakeBin(t, dir, "dpkg", `exit 0`)
	fakeBin(t, dir, "apt-mark", `exit 0`)
	runner := &scriptedCheckRunner{results: make(map[string][]checkCommandResult)}

	res := RunAptUpgrade(context.Background(), nil, nil, checkOptions(runner))

	if res.Err == nil || res.ExitCode != 100 {
		t.Fatalf("apt action should have failed: %#v", res)
	}
	if res.PostChecks == nil || res.HealthStatus != healthcheck.HealthHealthy {
		t.Fatalf("post-checks did not run independently: %#v", res)
	}
}

func TestPreCheckFailureCategory(t *testing.T) {
	tests := map[string]string{
		healthcheck.CheckDiskSpace:       apterr.CategoryDiskFull,
		healthcheck.CheckPackageLocks:    apterr.CategoryAptLocked,
		healthcheck.CheckDPKGAudit:       apterr.CategoryDpkgError,
		healthcheck.CheckAPTDependencies: apterr.CategoryDpkgError,
		healthcheck.CheckPackageIndexes:  apterr.CategoryNetworkOrRepo,
		healthcheck.CheckFailedServices:  apterr.CategoryAgentRefused,
	}
	for check, want := range tests {
		if got := preCheckFailureCategory(healthcheck.Result{Name: check, Status: healthcheck.StatusFailed}); got != want {
			t.Errorf("check %q: category = %q, want %q", check, got, want)
		}
	}
	unknown := healthcheck.Result{Name: healthcheck.CheckDiskSpace, Status: healthcheck.StatusUnknown}
	if got := preCheckFailureCategory(unknown); got != apterr.CategoryAgentRefused {
		t.Errorf("unknown pre-check: category = %q, want %q", got, apterr.CategoryAgentRefused)
	}
}

func TestCheckOptionsFromThresholdsUsesDefaultsAndCapsWait(t *testing.T) {
	defaults := CheckOptionsFromThresholds(0, 0, 0)
	if defaults.MinimumAvailableBytes != healthcheck.DefaultMinimumAvailableBytes ||
		defaults.BootMinimumAvailableBytes != healthcheck.DefaultBootMinimumAvailableBytes ||
		defaults.LockWait != defaultLockWait {
		t.Fatalf("defaults = %#v", defaults)
	}

	overridden := CheckOptionsFromThresholds(10, 20, ^uint64(0))
	if overridden.MinimumAvailableBytes != 10 || overridden.BootMinimumAvailableBytes != 20 ||
		overridden.LockWait != maximumLockWait {
		t.Fatalf("overridden = %#v", overridden)
	}
}

func TestRunAptUpgradeSuccess(t *testing.T) {
	dir := fakePATH(t)
	fakeBin(t, dir, "apt-get", `echo "$*"; exit 0`)
	fakeBin(t, dir, "dpkg", `exit 0`)

	res := runAptUpgradeAction(context.Background(), nil, nil)

	if res.Err != nil || res.ExitCode != 0 {
		t.Fatalf("want clean success, got exit=%d err=%v log=%q", res.ExitCode, res.Err, res.Log)
	}
}

func TestRunAptUpgradeFailureMapsExitCodeAndRepairsDpkg(t *testing.T) {
	dir := fakePATH(t)
	marker := filepath.Join(t.TempDir(), "configured")
	fakeBin(t, dir, "apt-get", `case "$*" in *dist-upgrade*) echo "E: boom" >&2; exit 100;; esac; exit 0`)
	fakeBin(t, dir, "dpkg", `[ "$1 $2" = "--configure -a" ] && : > `+marker+`; exit 0`)

	res := runAptUpgradeAction(context.Background(), nil, nil)

	if res.ExitCode != 100 {
		t.Fatalf("exit code: want 100, got %d", res.ExitCode)
	}
	if _, err := os.Stat(marker); err != nil {
		t.Fatalf("dpkg --configure -a was not run on failure: %v", err)
	}
	if !strings.Contains(res.Log, "boom") || !strings.Contains(res.Log, "dpkg --configure -a") {
		t.Fatalf("log missing expected content: %q", res.Log)
	}
}

func TestRunAptUpgradeRetriesWhileLockHeld(t *testing.T) {
	noRetryWait(t)
	dir := fakePATH(t)
	counter := filepath.Join(t.TempDir(), "n")
	fakeBin(t, dir, "apt-get", `
case "$*" in
*dist-upgrade*)
  n=$(cat `+counter+` 2>/dev/null || echo 0); n=$((n+1)); echo $n > `+counter+`
  if [ "$n" -le 2 ]; then echo "E: Could not get lock /var/lib/dpkg/lock-frontend" >&2; exit 100; fi
  exit 0 ;;
esac
exit 0`)
	fakeBin(t, dir, "dpkg", `exit 0`)

	res := runAptUpgradeAction(context.Background(), nil, nil)

	if res.Err != nil {
		t.Fatalf("want success after the lock clears, got err=%v log=%q", res.Err, res.Log)
	}
	if got := readCount(t, counter); got != 3 {
		t.Fatalf("dist-upgrade attempts: want 3, got %d", got)
	}
}

func TestRunAptUpgradeDoesNotRetryARealFailure(t *testing.T) {
	noRetryWait(t)
	dir := fakePATH(t)
	counter := filepath.Join(t.TempDir(), "n")
	fakeBin(t, dir, "apt-get", `
case "$*" in
*dist-upgrade*)
  n=$(cat `+counter+` 2>/dev/null || echo 0); n=$((n+1)); echo $n > `+counter+`
  echo "E: Sub-process /usr/bin/dpkg returned an error code (1)" >&2; exit 100 ;;
esac
exit 0`)
	fakeBin(t, dir, "dpkg", `exit 0`)

	res := runAptUpgradeAction(context.Background(), nil, nil)

	if res.ExitCode != 100 {
		t.Fatalf("exit code: want 100, got %d", res.ExitCode)
	}
	if got := readCount(t, counter); got != 1 {
		t.Fatalf("a real failure must not be retried, ran %d times", got)
	}
}

func TestRunAptUpgradeCapsHugeOutput(t *testing.T) {
	dir := fakePATH(t)
	fakeBin(t, dir, "apt-get", `case "$*" in *dist-upgrade*) yes cadence-output-line | head -c 400000; echo; exit 0;; esac; exit 0`)
	fakeBin(t, dir, "dpkg", `exit 0`)

	res := runAptUpgradeAction(context.Background(), nil, nil)

	if res.Err != nil {
		t.Fatalf("unexpected err: %v", res.Err)
	}
	if len(res.Log) > maxLogBytes+512 {
		t.Fatalf("log not capped: %d bytes", len(res.Log))
	}
	if !strings.Contains(res.Log, "bytes of output elided") {
		t.Fatalf("expected an elision marker in a %d-byte log", len(res.Log))
	}
}

func TestRunAptUpgradeClassifiesTheFailure(t *testing.T) {
	cases := []struct {
		name    string
		stderr  string
		wantCat string
		wantIn  string
	}{
		{
			name:    "package failure",
			stderr:  "E: Sub-process /usr/bin/dpkg returned an error code (1)",
			wantCat: "dpkg_error",
			wantIn:  "Sub-process /usr/bin/dpkg",
		},
		{
			name:    "mirror unreachable",
			stderr:  "E: Failed to fetch http://deb.debian.org/debian/x.deb  404  Not Found",
			wantCat: "network_or_repo",
			wantIn:  "Failed to fetch",
		},
		{
			name:    "out of space",
			stderr:  "dpkg: unrecoverable fatal error, aborting: failed to write: No space left on device",
			wantCat: "disk_full",
			wantIn:  "No space left on device",
		},
		{
			name:    "nothing recognisable",
			stderr:  "E: something the patterns do not know about",
			wantCat: "unknown",
			wantIn:  "something the patterns do not know",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			noRetryWait(t)
			dir := fakePATH(t)
			fakeBin(t, dir, "apt-get",
				`case "$*" in *dist-upgrade*) echo `+shq(tc.stderr)+` >&2; exit 100;; esac; exit 0`)
			fakeBin(t, dir, "dpkg", `exit 0`)

			res := runAptUpgradeAction(context.Background(), nil, nil)

			if res.FailureCategory != tc.wantCat {
				t.Fatalf("FailureCategory = %q, want %q (log=%q)", res.FailureCategory, tc.wantCat, res.Log)
			}
			if !strings.Contains(res.FailureSummary, tc.wantIn) {
				t.Fatalf("FailureSummary = %q, want it to contain %q", res.FailureSummary, tc.wantIn)
			}
		})
	}
}

// shq single-quotes s for a POSIX shell.
func shq(s string) string { return "'" + strings.ReplaceAll(s, "'", `'\''`) + "'" }

func TestRunAptUpgradeSuccessLeavesFailureFieldsEmpty(t *testing.T) {
	dir := fakePATH(t)
	fakeBin(t, dir, "apt-get", `echo "$*"; exit 0`)
	fakeBin(t, dir, "dpkg", `exit 0`)

	res := runAptUpgradeAction(context.Background(), nil, nil)

	if res.FailureCategory != "" || res.FailureSummary != "" {
		t.Fatalf("want empty failure fields on success, got category=%q summary=%q",
			res.FailureCategory, res.FailureSummary)
	}
}

func TestRunAptUpgradeClassifiesAnExpiredDeadlineAsTimeout(t *testing.T) {
	dir := fakePATH(t)
	fakeBin(t, dir, "apt-get", `case "$*" in *dist-upgrade*) exec sleep 10;; esac; exit 0`)
	fakeBin(t, dir, "dpkg", `exit 0`)

	ctx, cancel := context.WithTimeout(context.Background(), 150*time.Millisecond)
	defer cancel()

	res := runAptUpgradeAction(ctx, nil, nil)

	if res.FailureCategory != "timeout" {
		t.Fatalf("FailureCategory = %q, want %q", res.FailureCategory, "timeout")
	}
}

func TestRunAptUpgradeRepairsDpkgOnAFreshContextAfterTheDeadline(t *testing.T) {
	dir := fakePATH(t)
	marker := filepath.Join(t.TempDir(), "configured")
	// dist-upgrade hangs well past the caller's deadline; `exec` so the sleep
	// *is* the tracked process and dies promptly on cancel. dpkg records its run.
	fakeBin(t, dir, "apt-get", `case "$*" in *dist-upgrade*) exec sleep 10;; esac; exit 0`)
	fakeBin(t, dir, "dpkg", `[ "$1 $2" = "--configure -a" ] && : > `+marker+`; exit 0`)

	ctx, cancel := context.WithTimeout(context.Background(), 150*time.Millisecond)
	defer cancel()

	res := runAptUpgradeAction(ctx, nil, nil)

	if res.Err == nil {
		t.Fatal("expected the expired deadline to fail the upgrade")
	}
	if _, err := os.Stat(marker); err != nil {
		t.Fatalf("dpkg --configure -a must still run under a fresh context: %v", err)
	}
}

// fakeAptMark writes an apt-mark stand-in that appends every invocation
// (space-joined args) as one line to a calls log, and answers `showhold`
// with currentlyHeld (one name per line). Returns the calls-log path.
func fakeAptMark(t *testing.T, dir string, currentlyHeld ...string) string {
	t.Helper()
	callsLog := filepath.Join(t.TempDir(), "apt-mark-calls")
	if _, err := os.Create(callsLog); err != nil {
		t.Fatal(err)
	}
	fakeBin(t, dir, "apt-mark", `
echo "$*" >> `+callsLog+`
case "$1" in
  showhold) printf '%s\n' `+shq(strings.Join(currentlyHeld, "\n"))+` ;;
esac
exit 0`)
	return callsLog
}

func TestRunAptUpgradeReconcilesHolds(t *testing.T) {
	// "old-package" is Cadence's own last-known state (knownHeldPackages),
	// not merely what apt-mark showhold happens to report -- that is the
	// point of this reconciliation model.
	dir := fakePATH(t)
	fakeBin(t, dir, "apt-get", `echo "$*"; exit 0`)
	fakeBin(t, dir, "dpkg", `exit 0`)
	calls := fakeAptMark(t, dir)

	res := runAptUpgradeAction(context.Background(),
		[]string{"docker-ce", "postgresql-14"}, []string{"old-package"})

	if res.Err != nil {
		t.Fatalf("unexpected err: %v", res.Err)
	}
	raw, err := os.ReadFile(calls)
	if err != nil {
		t.Fatal(err)
	}
	got := string(raw)
	if !strings.Contains(got, "hold docker-ce postgresql-14") &&
		!strings.Contains(got, "hold postgresql-14 docker-ce") {
		t.Errorf("apt-mark hold not called with the resolved names: %q", got)
	}
	if !strings.Contains(got, "unhold old-package") {
		t.Errorf("apt-mark unhold not called on the known-held package that fell off the list: %q", got)
	}
}

func TestRunAptUpgradeNeverTouchesAThirdPartyHold(t *testing.T) {
	// Reproduces the real incident: a package held by something other than
	// Cadence (apt-mark showhold reports it) must survive reconciliation
	// when it is in neither knownHeldPackages nor excludedPackages.
	dir := fakePATH(t)
	fakeBin(t, dir, "apt-get", `echo "$*"; exit 0`)
	fakeBin(t, dir, "dpkg", `exit 0`)
	calls := fakeAptMark(t, dir, "login", "passwd") // held by a third party, unrelated to Cadence

	res := runAptUpgradeAction(context.Background(), []string{"docker-ce"}, nil)

	if res.Err != nil {
		t.Fatalf("unexpected err: %v", res.Err)
	}
	raw, _ := os.ReadFile(calls)
	got := string(raw)
	if strings.Contains(got, "unhold login") || strings.Contains(got, "unhold passwd") {
		t.Errorf("a third-party hold must never be unheld, calls: %q", got)
	}
	if !strings.Contains(got, "hold docker-ce") {
		t.Errorf("the wanted name should still be held, calls: %q", got)
	}
}

func TestRunAptUpgradeFirstRunWithNoKnownStateOnlyHoldsNewOnes(t *testing.T) {
	dir := fakePATH(t)
	fakeBin(t, dir, "apt-get", `echo "$*"; exit 0`)
	fakeBin(t, dir, "dpkg", `exit 0`)
	// apt-mark showhold reports pre-existing holds Cadence has never
	// recorded; knownHeldPackages is nil (no prior apt_upgrade result yet).
	calls := fakeAptMark(t, dir, "login", "passwd", "some-other-package")

	res := runAptUpgradeAction(context.Background(), []string{"docker-ce"}, nil)

	if res.Err != nil {
		t.Fatalf("unexpected err: %v", res.Err)
	}
	raw, _ := os.ReadFile(calls)
	got := string(raw)
	if strings.Contains(got, "unhold") {
		t.Errorf("a first run with no known state must never unhold anything, calls: %q", got)
	}
	if !strings.Contains(got, "hold docker-ce") {
		t.Errorf("the wanted name should still be held, calls: %q", got)
	}
}

func TestRunAptUpgradeHeldPackagesReported(t *testing.T) {
	dir := fakePATH(t)
	fakeBin(t, dir, "apt-get", `echo "$*"; exit 0`)
	fakeBin(t, dir, "dpkg", `exit 0`)
	fakeAptMark(t, dir)

	res := runAptUpgradeAction(context.Background(),
		[]string{"docker-ce", "postgresql-14"}, []string{"postgresql-14", "old-package"})

	want := []string{"docker-ce", "postgresql-14"} // old-package fell off, docker-ce is newly held
	got := append([]string(nil), res.HeldPackages...)
	sort.Strings(got)
	if !reflect.DeepEqual(got, want) {
		t.Errorf("HeldPackages = %v, want %v", got, want)
	}
}

func TestRunAptUpgradeRejectsAnInvalidExcludedPackageName(t *testing.T) {
	dir := fakePATH(t)
	fakeBin(t, dir, "apt-get", `echo "$*"; exit 0`)
	fakeBin(t, dir, "dpkg", `exit 0`)
	calls := fakeAptMark(t, dir)

	res := runAptUpgradeAction(context.Background(), []string{"docker-ce", "rm -rf /"}, nil)

	if !strings.Contains(res.Log, `ignoring invalid excluded-package name`) {
		t.Errorf("expected a rejection breadcrumb in the log, got: %q", res.Log)
	}
	raw, _ := os.ReadFile(calls)
	if strings.Contains(string(raw), "rm -rf") {
		t.Errorf("an invalid name must never reach apt-mark, calls: %q", raw)
	}
	if !strings.Contains(string(raw), "hold docker-ce") {
		t.Errorf("the valid name should still be held, calls: %q", raw)
	}
}

func TestRunAptUpgradeReconciliationSurvivesAMissingAptMark(t *testing.T) {
	// No apt-mark on PATH at all: showhold fails, hold/unhold are never
	// reached because there is nothing to reconcile against an empty
	// current set that also equals the (empty) wanted set. Must not fail
	// the job over this.
	dir := fakePATH(t)
	fakeBin(t, dir, "apt-get", `echo "$*"; exit 0`)
	fakeBin(t, dir, "dpkg", `exit 0`)

	res := runAptUpgradeAction(context.Background(), nil, nil)

	if res.Err != nil || res.ExitCode != 0 {
		t.Fatalf("want clean success despite no apt-mark, got exit=%d err=%v", res.ExitCode, res.Err)
	}
}

func TestRunAptUpgradeHeldConflictsOnAKeptBackPackage(t *testing.T) {
	dir := fakePATH(t)
	fakeBin(t, dir, "apt-get", `case "$*" in
*dist-upgrade*) echo "The following packages have been kept back:"; echo "  docker-ce"; exit 0;;
esac
exit 0`)
	fakeBin(t, dir, "dpkg", `exit 0`)
	fakeAptMark(t, dir)

	res := runAptUpgradeAction(context.Background(), []string{"docker-ce"}, nil)

	if len(res.HeldConflicts) != 1 || res.HeldConflicts[0] != "docker-ce" {
		t.Errorf("HeldConflicts = %v, want [docker-ce]", res.HeldConflicts)
	}
}

func TestRunAptUpgradeHeldConflictsNilOnACleanRun(t *testing.T) {
	dir := fakePATH(t)
	fakeBin(t, dir, "apt-get", `echo "$*"; exit 0`)
	fakeBin(t, dir, "dpkg", `exit 0`)
	fakeAptMark(t, dir)

	res := runAptUpgradeAction(context.Background(), []string{"docker-ce"}, nil)

	if res.HeldConflicts != nil {
		t.Errorf("HeldConflicts = %v, want nil", res.HeldConflicts)
	}
}
