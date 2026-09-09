package executor

import (
	"bytes"
	"context"
	"os/exec"
	"sort"

	"cadence/agent/internal/apterr"
	"cadence/agent/internal/aptsim"
	"cadence/agent/internal/holds"
	"cadence/agent/internal/report"
)

// DryRunOutcome is what RunAptDryRun reports back for an apt_dry_run job. It
// mirrors the fields of Result that a pure-read job can produce: no reboot, no
// held sets, but the same failure classification as a real upgrade.
type DryRunOutcome struct {
	Status          string // "succeeded" | "failed"
	ExitCode        int
	Log             string
	FailureCategory string
	FailureSummary  string

	// DryRun is the parsed preview, set only when the simulation succeeded.
	// nil on failure (the log and FailureCategory carry the why).
	DryRun *report.DryRun
}

// RunAptDryRun refreshes the package lists and runs `apt-get -s dist-upgrade`,
// then parses the simulation into a structured preview of what a real
// apt_upgrade would do. It NEVER changes anything on the host: no
// apt-mark hold/unhold, no dpkg call, no `-y` upgrade. The three commands it
// runs (`apt-get update`, `apt-get -s dist-upgrade`, `apt-mark showhold`) all
// have fixed argument vectors.
//
// excludedPackages is the server's already-resolved exclusion list for this
// host. It is used ONLY to filter the parsed result (a name matching an active
// exclusion is moved to DryRun.Excluded); it is never passed to a command.
func RunAptDryRun(ctx context.Context, excludedPackages []string) DryRunOutcome {
	var out bytes.Buffer

	// Refresh lists so the preview matches what a real run would see right now.
	// Non-fatal: only touches apt's index cache, never installed packages.
	if err := aptCommand(ctx, &out, "apt-get", "update").Run(); err != nil {
		out.WriteString("\n[cadence] apt-get update failed; simulating against existing lists\n")
	}

	var sim bytes.Buffer
	err := aptCommand(ctx, &sim, "apt-get", "-s", "dist-upgrade").Run()
	out.Write(sim.Bytes())

	if err != nil {
		res := DryRunOutcome{Status: "failed", ExitCode: -1, Log: capLog(out.String())}
		if exitErr, ok := err.(*exec.ExitError); ok {
			res.ExitCode = exitErr.ExitCode()
		}
		deadline := ctx.Err() == context.DeadlineExceeded
		cat, summary := apterr.Classify(sim.String(), deadline)
		if !deadline && cat == apterr.CategoryUnknown {
			if c2, s2 := apterr.Classify(stripAnnotations(out.String()), false); c2 != apterr.CategoryUnknown {
				cat, summary = c2, s2
			}
		}
		res.FailureCategory = cat
		res.FailureSummary = summary
		return res
	}

	dr := buildDryRun(aptsim.Parse(sim.String()), excludedPackages)

	// What apt itself keeps back because of a hold already on the box (an
	// operator's, unattended-upgrades', a distro default). Read-only, and the
	// same mechanic as held_conflicts on a real apt_upgrade.
	var showhold bytes.Buffer
	if aptCommand(ctx, &showhold, "apt-mark", "showhold").Run() == nil {
		dr.HeldInPlace = holds.Correlate(sim.String(), holds.ParseShowHold(showhold.String()))
	}
	if dr.HeldInPlace == nil {
		dr.HeldInPlace = []string{}
	}

	return DryRunOutcome{
		Status:   "succeeded",
		ExitCode: 0,
		Log:      capLog(out.String()),
		DryRun:   &dr,
	}
}

// buildDryRun turns a parsed simulation into the wire preview, applying the
// exclusion filter. Every list comes back non-nil.
func buildDryRun(sim aptsim.Simulation, excludedPackages []string) report.DryRun {
	valid, _ := holds.Filter(excludedPackages)
	excluded := make(map[string]bool, len(valid))
	for _, n := range valid {
		excluded[n] = true
	}

	dr := report.DryRun{
		Updated:        []report.DryRunPkg{},
		NewlyInstalled: []report.DryRunPkg{},
		Removed:        []report.DryRunPkg{},
		KeptBack:       []string{},
		Excluded:       []string{},
		HeldInPlace:    []string{},
	}
	seenExcluded := map[string]bool{}

	for _, in := range sim.Inst {
		if excluded[in.Name] {
			if !seenExcluded[in.Name] {
				dr.Excluded = append(dr.Excluded, in.Name)
				seenExcluded[in.Name] = true
			}
			continue
		}
		pkg := report.DryRunPkg{
			Name:             in.Name,
			Architecture:     in.Arch,
			InstalledVersion: in.OldVersion,
			CandidateVersion: in.Candidate,
			IsSecurityUpdate: in.IsSecurity,
		}
		if in.OldVersion == "" {
			dr.NewlyInstalled = append(dr.NewlyInstalled, pkg)
		} else {
			dr.Updated = append(dr.Updated, pkg)
		}
	}

	for _, rm := range sim.Remv {
		dr.Removed = append(dr.Removed, report.DryRunPkg{
			Name:             rm.Name,
			InstalledVersion: rm.OldVersion,
		})
	}

	if len(sim.KeptBack) > 0 {
		dr.KeptBack = append(dr.KeptBack, sim.KeptBack...)
	}

	sortByName(dr.Updated)
	sortByName(dr.NewlyInstalled)
	sortByName(dr.Removed)
	sort.Strings(dr.KeptBack)
	sort.Strings(dr.Excluded)
	return dr
}

func sortByName(pkgs []report.DryRunPkg) {
	sort.Slice(pkgs, func(i, j int) bool {
		if pkgs[i].Name != pkgs[j].Name {
			return pkgs[i].Name < pkgs[j].Name
		}
		return pkgs[i].Architecture < pkgs[j].Architecture
	})
}
