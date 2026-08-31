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
)

// Issue reboots the host. It prefers `systemctl --no-block reboot` (returns
// immediately so the agent can exit cleanly) and falls back to
// `shutdown -r now` on a host without systemd.
func Issue(ctx context.Context) error {
	out, err := exec.CommandContext(ctx, "systemctl", "--no-block", "reboot").CombinedOutput()
	if err == nil {
		return nil
	}
	first := fmt.Errorf("systemctl --no-block reboot: %w: %s", err, bytes.TrimSpace(out))

	out2, err2 := exec.CommandContext(ctx, "shutdown", "-r", "now").CombinedOutput()
	if err2 == nil {
		return nil
	}
	return errors.Join(first, fmt.Errorf("shutdown -r now: %w: %s", err2, bytes.TrimSpace(out2)))
}
