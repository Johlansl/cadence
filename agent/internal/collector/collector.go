// Package collector gathers the local package and OS state via dpkg / apt and
// assembles the report payload.
package collector

import (
	"bytes"
	"context"
	"fmt"
	"log"
	"os"
	"os/exec"
	"strings"

	"cadence/agent/internal/report"
)

// Collect gathers everything the server needs for one report.
func Collect(ctx context.Context, agentVersion string, runAptUpdate bool) (report.Report, error) {
	installed, err := installedPackages(ctx)
	if err != nil {
		return report.Report{}, fmt.Errorf("listing installed packages: %w", err)
	}

	if runAptUpdate {
		if err := aptUpdate(ctx); err != nil {
			// Non-fatal: proceed with whatever apt already knows locally.
			log.Printf("warning: apt-get update failed, using existing package lists: %v", err)
		}
	}

	updates, err := pendingUpdates(ctx)
	if err != nil {
		return report.Report{}, fmt.Errorf("simulating dist-upgrade: %w", err)
	}

	osFamily, osName, osVersion := readOSRelease()
	host, fqdn := hostnameInfo()

	return report.Report{
		AgentVersion:   agentVersion,
		Hostname:       host,
		FQDN:           fqdn,
		OSFamily:       osFamily,
		OSName:         strOrNil(osName),
		OSVersion:      strOrNil(osVersion),
		PackageManager: "apt",
		RebootRequired: rebootRequired(),
		Packages:       mergeUpdates(installed, updates),
	}, nil
}

// runCommand runs an external command with a stable, locale-independent
// environment so its output can be parsed reliably.
func runCommand(ctx context.Context, name string, args ...string) ([]byte, error) {
	cmd := exec.CommandContext(ctx, name, args...)
	cmd.Env = append(os.Environ(), "LC_ALL=C", "LANG=C", "DEBIAN_FRONTEND=noninteractive")

	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	if err := cmd.Run(); err != nil {
		if msg := strings.TrimSpace(stderr.String()); msg != "" {
			return stdout.Bytes(), fmt.Errorf("%s %s: %w: %s",
				name, strings.Join(args, " "), err, msg)
		}
		return stdout.Bytes(), fmt.Errorf("%s %s: %w", name, strings.Join(args, " "), err)
	}
	return stdout.Bytes(), nil
}

func strOrNil(s string) *string {
	if s == "" {
		return nil
	}
	return &s
}
