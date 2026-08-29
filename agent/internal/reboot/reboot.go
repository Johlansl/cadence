// Package reboot issues a system reboot after an upgrade that requires one.
// It is only ever called once the job result has been reported, so the server
// never sees the job stuck in "running".
package reboot

import (
	"context"
	"fmt"
	"os/exec"
)

// Issue asks systemd to reboot without blocking: the call returns immediately
// and the shutdown proceeds, so the agent process can exit cleanly.
func Issue(ctx context.Context) error {
	out, err := exec.CommandContext(ctx, "systemctl", "--no-block", "reboot").CombinedOutput()
	if err != nil {
		return fmt.Errorf("systemctl --no-block reboot: %w: %s", err, out)
	}
	return nil
}
