// Package procenv builds the environment for the external commands the agent
// runs (apt-get, dpkg, systemctl, shutdown).
//
// The agent is configured through CADENCE_* variables, CADENCE_TOKEN among
// them. A child process started with a nil Env inherits the agent's whole
// environment, which would expose the token via /proc/<pid>/environ and to
// every apt hook and dpkg maintainer script. Every spawn therefore goes
// through For(), which drops the CADENCE_* entries.
package procenv

import (
	"os"
	"strings"
)

// For returns the current environment with every CADENCE_* entry removed,
// followed by overrides (e.g. "LC_ALL=C"). Later entries win, which matches
// how exec.Cmd resolves duplicate keys.
func For(overrides ...string) []string {
	base := os.Environ()
	out := make([]string, 0, len(base)+len(overrides))
	for _, kv := range base {
		if strings.HasPrefix(kv, "CADENCE_") {
			continue
		}
		out = append(out, kv)
	}
	return append(out, overrides...)
}
