package collector

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// fakeAptGet puts a stub `apt-get` on PATH that fails with a lock error for the
// first `failN` calls, then prints one Inst line.
func fakeAptGet(t *testing.T, failN int) {
	t.Helper()
	dir := t.TempDir()
	counter := filepath.Join(dir, "n")
	script := "#!/bin/sh\n" +
		"n=$(cat " + counter + " 2>/dev/null || echo 0); n=$((n+1)); echo $n > " + counter + "\n" +
		"if [ \"$n\" -le " + itoa(failN) + " ]; then\n" +
		"  echo 'E: Could not get lock /var/lib/dpkg/lock-frontend' >&2\n  exit 100\nfi\n" +
		"echo 'Inst bash [5.2-1] (5.3-1 Debian:trixie [amd64])'\nexit 0\n"
	if err := os.WriteFile(filepath.Join(dir, "apt-get"), []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var b []byte
	for n > 0 {
		b = append([]byte{byte('0' + n%10)}, b...)
		n /= 10
	}
	return string(b)
}

func TestPendingUpdatesRetriesOnAptLock(t *testing.T) {
	old := aptRetryWaits
	aptRetryWaits = []time.Duration{0, 0, 0, 0}
	defer func() { aptRetryWaits = old }()

	fakeAptGet(t, 2) // fail twice, succeed on the third

	updates, err := pendingUpdatesWithRetry(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := updates[pkgKey("bash", "amd64")]; !ok {
		t.Fatalf("expected bash update after retry, got %v", updates)
	}
}

func TestPendingUpdatesGivesUpWhenLockHeld(t *testing.T) {
	old := aptRetryWaits
	aptRetryWaits = []time.Duration{0, 0}
	defer func() { aptRetryWaits = old }()

	fakeAptGet(t, 99) // never clears

	if _, err := pendingUpdatesWithRetry(context.Background()); err == nil {
		t.Fatal("expected an error when the lock never clears")
	}
}
