package upgrade

import (
	"os"
	"path/filepath"
	"strings"
)

// CanonicalPath is the only executable location eligible for self-upgrade
// (PR1 convergence): the systemd units, the installer and the .deb all
// agree on it.
const CanonicalPath = "/usr/bin/cadence-agent"

// ExecutableEligible reports whether a resolved executable path is the
// canonical agent binary. Pure and exact: no prefix game, no alternate
// directory, no other filename. A path marked " (deleted)" (the binary was
// replaced under the running process) never qualifies, and neither does an
// empty or ambiguous path.
func ExecutableEligible(resolvedPath string) bool {
	if resolvedPath == "" || strings.Contains(resolvedPath, " (deleted)") {
		return false
	}
	return resolvedPath == CanonicalPath
}

// CurrentExecutableEligible reports whether this process runs from the
// canonical path. os.Executable on Linux reads /proc/self/exe, which the
// kernel already resolves to the running file; EvalSymlinks then settles
// any remaining link. Decided explicitly: eligibility is about where the
// running code lives, resolved. A symlink sitting at the canonical path but
// pointing elsewhere resolves away from it and is refused; a link elsewhere
// pointing at the canonical binary resolves to it and is accepted. Any
// lookup failure refuses (fail-closed).
func CurrentExecutableEligible() bool {
	exe, err := os.Executable()
	if err != nil {
		return false
	}
	resolved, err := filepath.EvalSymlinks(exe)
	if err != nil {
		return false
	}
	return ExecutableEligible(resolved)
}
