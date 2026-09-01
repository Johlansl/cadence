// Package reboot issues a system reboot after an upgrade that requires one.
// It is only ever called once the job result has been reported, so the server
// never sees the job stuck in "running".
package reboot

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os/exec"

	"cadence/agent/internal/procenv"
)

// Issue reboots the host. It prefers `systemctl --no-block reboot` (returns
// immediately so the agent can exit cleanly) and falls back to
// `shutdown -r now` on a host without systemd.
func Issue(ctx context.Context) error {
	systemctl := exec.CommandContext(ctx, "systemctl", "--no-block", "reboot")
	systemctl.Env = procenv.For()
	out, err := systemctl.CombinedOutput()
	if err == nil {
		return nil
	}
	first := fmt.Errorf("systemctl --no-block reboot: %w: %s", err, bytes.TrimSpace(out))

	shutdown := exec.CommandContext(ctx, "shutdown", "-r", "now")
	shutdown.Env = procenv.For()
	out2, err2 := shutdown.CombinedOutput()
	if err2 == nil {
		return nil
	}
	return errors.Join(first, fmt.Errorf("shutdown -r now: %w: %s", err2, bytes.TrimSpace(out2)))
}
