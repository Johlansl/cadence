// Package executor runs an apt upgrade for an apt_upgrade job and captures its
// combined output. It never reboots; it only reports whether a reboot became
// required (report-only, per CLAUDE.md V1 scope).
package executor

import (
	"bytes"
	"context"
	"os"
	"os/exec"

	"cadence/agent/internal/rebootcheck"
)

type Result struct {
	ExitCode       int
	Log            string
	RebootRequired bool
	Err            error // non-nil if the command could not run or was killed
}

func aptEnv() []string {
	return append(os.Environ(),
		"LC_ALL=C",
		"LANG=C",
		"DEBIAN_FRONTEND=noninteractive",
		"APT_LISTCHANGES_FRONTEND=none",
		"NEEDRESTART_MODE=a", // let needrestart restart services without prompting
	)
}

// RunAptUpgrade refreshes the package lists, then runs a non-interactive
// `apt-get dist-upgrade -y`. If the upgrade fails it runs `dpkg --configure -a`
// so a half-applied transaction is left as consistent as possible. Output from
// all three commands is captured in execution order.
func RunAptUpgrade(ctx context.Context) Result {
	var out bytes.Buffer

	// Refresh lists so the upgrade reflects current apt state. Non-fatal: a
	// failure here just means we apply whatever apt already knows.
	upd := exec.CommandContext(ctx, "apt-get", "update")
	upd.Env = aptEnv()
	upd.Stdout, upd.Stderr = &out, &out
	if err := upd.Run(); err != nil {
		out.WriteString("\n[cadence] apt-get update failed; using existing lists\n")
	}

	cmd := exec.CommandContext(ctx, "apt-get",
		"-o", "Dpkg::Options::=--force-confdef",
		"-o", "Dpkg::Options::=--force-confold",
		"-y", "dist-upgrade",
	)
	cmd.Env = aptEnv()
	cmd.Stdout, cmd.Stderr = &out, &out

	err := cmd.Run()
	res := Result{}
	if err != nil {
		res.Err = err
		if exitErr, ok := err.(*exec.ExitError); ok {
			res.ExitCode = exitErr.ExitCode()
		} else {
			res.ExitCode = -1 // failed to start, or context deadline/cancel
		}

		// Best-effort: leave dpkg in a configured state.
		out.WriteString("\n[cadence] apt failed; running dpkg --configure -a\n")
		fix := exec.CommandContext(ctx, "dpkg", "--configure", "-a")
		fix.Env = aptEnv()
		fix.Stdout, fix.Stderr = &out, &out
		_ = fix.Run()
	}

	res.Log = out.String()
	res.RebootRequired = rebootcheck.Pending()
	return res
}
