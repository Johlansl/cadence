// Package executor runs an apt upgrade for an apt_upgrade job and captures its
// combined output. It never reboots; it only reports whether a reboot became
// required (report-only, per CLAUDE.md V1 scope).
package executor

import (
	"bytes"
	"context"
	"os"
	"os/exec"
)

type Result struct {
	ExitCode       int
	Log            string
	RebootRequired bool
	Err            error // non-nil if the command could not run or was killed
}

// RunAptUpgrade runs a non-interactive `apt-get dist-upgrade -y`. It does NOT
// run `apt-get update` first: it applies what apt already knows, so the outcome
// matches what the dashboard showed. Package-list freshness is controlled on
// the collection side via CADENCE_RUN_APT_UPDATE.
func RunAptUpgrade(ctx context.Context) Result {
	cmd := exec.CommandContext(ctx, "apt-get",
		"-o", "Dpkg::Options::=--force-confdef",
		"-o", "Dpkg::Options::=--force-confold",
		"-y", "dist-upgrade",
	)
	cmd.Env = append(os.Environ(),
		"LC_ALL=C",
		"LANG=C",
		"DEBIAN_FRONTEND=noninteractive",
		"APT_LISTCHANGES_FRONTEND=none",
		"NEEDRESTART_MODE=a", // let needrestart restart services without prompting
	)

	// Combined stream so the log reads in execution order.
	var out bytes.Buffer
	cmd.Stdout = &out
	cmd.Stderr = &out

	err := cmd.Run()
	res := Result{Log: out.String(), RebootRequired: rebootRequired()}
	if err != nil {
		res.Err = err
		if exitErr, ok := err.(*exec.ExitError); ok {
			res.ExitCode = exitErr.ExitCode()
		} else {
			res.ExitCode = -1 // failed to start, or context deadline/cancel
		}
	}
	return res
}

func rebootRequired() bool {
	_, err := os.Stat("/var/run/reboot-required")
	return err == nil
}
