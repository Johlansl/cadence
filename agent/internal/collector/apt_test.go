package collector

import (
	"testing"

	"cadence/agent/internal/aptsim"
	"cadence/agent/internal/report"
)

func TestMergeUpdatesIgnoresNonInstalled(t *testing.T) {
	installed := map[string]report.Package{
		pkgKey("openssl", "amd64"): {Name: "openssl", Architecture: "amd64", InstalledVersion: "3.0.11-1"},
		pkgKey("bash", "amd64"):    {Name: "bash", Architecture: "amd64", InstalledVersion: "5.2-2"},
	}
	updates := map[string]aptsim.Inst{
		pkgKey("openssl", "amd64"): {Name: "openssl", Arch: "amd64", OldVersion: "3.0.11-1", Candidate: "3.0.14-1", Origin: "Debian-Security:12/stable-security", IsSecurity: true},
		// New dependency, not installed -> must be ignored.
		pkgKey("libnew", "amd64"): {Name: "libnew", Arch: "amd64", Candidate: "1.0-1", Origin: "Debian:12/stable"},
	}

	got := mergeUpdates(installed, updates)
	if len(got) != 2 {
		t.Fatalf("got %d packages, want 2 (%+v)", len(got), got)
	}
	if got[0].Name != "bash" || got[1].Name != "openssl" {
		t.Fatalf("not sorted by name: %+v", got)
	}
	if got[0].CandidateVersion != nil {
		t.Errorf("bash should have no candidate, got %v", *got[0].CandidateVersion)
	}
	if got[1].CandidateVersion == nil || *got[1].CandidateVersion != "3.0.14-1" || !got[1].IsSecurityUpdate {
		t.Errorf("openssl update not applied: %+v", got[1])
	}
}
