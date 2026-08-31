package executor

import (
	"context"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

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

func TestRunAptUpgradeSuccess(t *testing.T) {
	dir := fakePATH(t)
	fakeBin(t, dir, "apt-get", `echo "$*"; exit 0`)
	fakeBin(t, dir, "dpkg", `exit 0`)

	res := RunAptUpgrade(context.Background())

	if res.Err != nil || res.ExitCode != 0 {
		t.Fatalf("want clean success, got exit=%d err=%v log=%q", res.ExitCode, res.Err, res.Log)
	}
}

func TestRunAptUpgradeFailureMapsExitCodeAndRepairsDpkg(t *testing.T) {
	dir := fakePATH(t)
	marker := filepath.Join(t.TempDir(), "configured")
	fakeBin(t, dir, "apt-get", `case "$*" in *dist-upgrade*) echo "E: boom" >&2; exit 100;; esac; exit 0`)
	fakeBin(t, dir, "dpkg", `[ "$1 $2" = "--configure -a" ] && : > `+marker+`; exit 0`)

	res := RunAptUpgrade(context.Background())

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

	res := RunAptUpgrade(context.Background())

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

	res := RunAptUpgrade(context.Background())

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

	res := RunAptUpgrade(context.Background())

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

func TestRunAptUpgradeRepairsDpkgOnAFreshContextAfterTheDeadline(t *testing.T) {
	dir := fakePATH(t)
	marker := filepath.Join(t.TempDir(), "configured")
	// dist-upgrade hangs well past the caller's deadline; `exec` so the sleep
	// *is* the tracked process and dies promptly on cancel. dpkg records its run.
	fakeBin(t, dir, "apt-get", `case "$*" in *dist-upgrade*) exec sleep 10;; esac; exit 0`)
	fakeBin(t, dir, "dpkg", `[ "$1 $2" = "--configure -a" ] && : > `+marker+`; exit 0`)

	ctx, cancel := context.WithTimeout(context.Background(), 150*time.Millisecond)
	defer cancel()

	res := RunAptUpgrade(ctx)

	if res.Err == nil {
		t.Fatal("expected the expired deadline to fail the upgrade")
	}
	if _, err := os.Stat(marker); err != nil {
		t.Fatalf("dpkg --configure -a must still run under a fresh context: %v", err)
	}
}
