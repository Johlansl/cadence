package reboot

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func writeBin(t *testing.T, dir, name, body string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, name), []byte("#!/bin/sh\n"+body), 0o755); err != nil {
		t.Fatal(err)
	}
}

func TestIssuePrefersSystemctl(t *testing.T) {
	dir := t.TempDir()
	writeBin(t, dir, "systemctl", "exit 0")
	writeBin(t, dir, "shutdown", "touch "+filepath.Join(dir, "shut")+"; exit 0")
	t.Setenv("PATH", dir)

	if err := Issue(context.Background()); err != nil {
		t.Fatalf("want nil, got %v", err)
	}
	if _, err := os.Stat(filepath.Join(dir, "shut")); err == nil {
		t.Fatal("shutdown must not run when systemctl succeeds")
	}
}

func TestIssueFallsBackToShutdown(t *testing.T) {
	dir := t.TempDir()
	writeBin(t, dir, "systemctl", `echo "System has not been booted with systemd" >&2; exit 1`)
	writeBin(t, dir, "shutdown", "exit 0")
	t.Setenv("PATH", dir)

	if err := Issue(context.Background()); err != nil {
		t.Fatalf("want nil after fallback, got %v", err)
	}
}

func TestIssueReportsBothFailures(t *testing.T) {
	dir := t.TempDir()
	writeBin(t, dir, "systemctl", `echo boom-systemctl >&2; exit 1`)
	writeBin(t, dir, "shutdown", `echo boom-shutdown >&2; exit 1`)
	t.Setenv("PATH", dir)

	err := Issue(context.Background())
	if err == nil {
		t.Fatal("want an error when both commands fail")
	}
	if !strings.Contains(err.Error(), "systemctl") || !strings.Contains(err.Error(), "shutdown") {
		t.Fatalf("error should mention both paths: %v", err)
	}
}
