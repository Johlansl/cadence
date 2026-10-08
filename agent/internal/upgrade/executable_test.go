package upgrade

import (
	"os"
	"path/filepath"
	"testing"
)

func TestExecutableEligible(t *testing.T) {
	cases := map[string]bool{
		"/usr/bin/cadence-agent":           true,
		"":                                 false,
		"/usr/local/bin/cadence-agent":     false, // pre-PR1 location
		"/tmp/cadence-agent":               false,
		"/opt/cadence-agent":               false,
		"/usr/bin/other":                   false, // right dir, wrong file
		"/usr/bin/cadence-agent-plus":      false, // no prefix game
		"/usr/bin/cadence-agent/":          false, // trailing slash
		"/usr/bin//cadence-agent":          false, // no normalization
		"usr/bin/cadence-agent":            false, // relative
		"/usr/bin/cadence-agent (deleted)": false, // replaced under us
		"/usr/bin/cadence-agent(deleted)":  false,
	}
	for path, want := range cases {
		if got := ExecutableEligible(path); got != want {
			t.Errorf("ExecutableEligible(%q) = %v, want %v", path, got, want)
		}
	}
}

func TestExecutableEligibleSymlinkPolicy(t *testing.T) {
	// Eligibility compares the resolved path: the pure function sees only
	// the final string. A link at the canonical path resolving elsewhere is
	// refused; resolution itself is the wrapper's job (os.Executable plus
	// EvalSymlinks), exercised below on a temp link.
	dir := t.TempDir()
	target := filepath.Join(dir, "real-agent")
	if err := os.WriteFile(target, []byte("x"), 0o755); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(dir, "link-agent")
	if err := os.Symlink(target, link); err != nil {
		t.Fatal(err)
	}
	resolved, err := filepath.EvalSymlinks(link)
	if err != nil {
		t.Fatal(err)
	}
	if ExecutableEligible(resolved) {
		t.Errorf("resolved non-canonical link %q accepted", resolved)
	}
}

func TestCurrentExecutableEligibleRunsReadOnly(t *testing.T) {
	// In a test run the binary lives under the build cache, never at the
	// canonical path: expect false. Guarded so the test stays honest if it
	// ever does run from /usr/bin.
	exe, err := os.Executable()
	if err != nil {
		t.Skip("os.Executable unavailable")
	}
	resolved, err := filepath.EvalSymlinks(exe)
	if err != nil {
		t.Skip("cannot resolve test executable")
	}
	if resolved == CanonicalPath {
		t.Skip("test itself runs from the canonical path")
	}
	if CurrentExecutableEligible() {
		t.Errorf("test binary %q reported eligible", resolved)
	}
}
