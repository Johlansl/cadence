// Package executor runs an apt upgrade for an apt_upgrade job and captures its
// combined output. It never reboots; it only reports whether a reboot became
// required (report-only -- see docs/decisions.md, "Updates").
package executor

import (
	"bytes"
	"context"
	"fmt"
	"os/exec"
	"sort"
	"strings"
	"syscall"
	"time"

	"cadence/agent/internal/apterr"
	"cadence/agent/internal/holds"
	"cadence/agent/internal/procenv"
	"cadence/agent/internal/rebootcheck"
)

// maxLogBytes bounds Result.Log so a large dist-upgrade cannot produce an
// unbounded POST body. The head and tail are kept, the middle elided.
const maxLogBytes = 128 * 1024

func capLog(s string) string {
	if len(s) <= maxLogBytes {
		return s
	}
	half := maxLogBytes / 2
	omitted := len(s) - 2*half
	return s[:half] +
		fmt.Sprintf("\n\n[cadence] ... %d bytes of output elided ...\n\n", omitted) +
		s[len(s)-half:]
}

// dpkgConfigureTimeout bounds the `dpkg --configure -a` recovery run. It runs
// under a fresh context, not the upgrade's, so recovery still happens when the
// upgrade failed *because* its own deadline expired.
const dpkgConfigureTimeout = 10 * time.Minute

// aptWaitDelay is how long to wait, after SIGTERM on context cancel, before
// SIGKILL and giving up on the command's output -- so an orphaned grandchild
// holding the output pipe can't wedge the run.
const aptWaitDelay = 30 * time.Second

// aptLockRetryWaits is the backoff before re-running dist-upgrade when another
// process (unattended-upgrades) holds the apt lock. A var so tests can shorten it.
var aptLockRetryWaits = []time.Duration{0, 15 * time.Second, 30 * time.Second, 60 * time.Second}

type Result struct {
	ExitCode       int
	Log            string
	RebootRequired bool
	Err            error // non-nil if the command could not run or was killed

	// Set only when the run failed. FailureCategory is one of the apterr
	// categories; FailureSummary is a single line lifted from the output.
	FailureCategory string
	FailureSummary  string

	// HeldConflicts names the packages, among the ones this run held, apt
	// showed real evidence of skipping or blocking on -- nil when reconciled
	// holds caused no visible problem (or none were held at all), never
	// silently omitted just because the run happened to succeed.
	HeldConflicts []string
}

func aptEnv() []string {
	return procenv.For(
		"LC_ALL=C",
		"LANG=C",
		"DEBIAN_FRONTEND=noninteractive",
		"APT_LISTCHANGES_FRONTEND=none",
		"NEEDRESTART_MODE=a", // let needrestart restart services without prompting
	)
}

// aptCommand builds an apt/dpkg command with a stable env and a graceful
// cancel: on context deadline/cancel it gets SIGTERM (letting dpkg finish the
// package it is mid-way through), then SIGKILL after aptWaitDelay.
func aptCommand(ctx context.Context, out *bytes.Buffer, name string, args ...string) *exec.Cmd {
	cmd := exec.CommandContext(ctx, name, args...)
	cmd.Env = aptEnv()
	cmd.Stdout, cmd.Stderr = out, out
	cmd.Cancel = func() error { return cmd.Process.Signal(syscall.SIGTERM) }
	cmd.WaitDelay = aptWaitDelay
	return cmd
}

// RunAptUpgrade refreshes the package lists, reconciles dpkg's hold state to
// excludedPackages, then runs a non-interactive `apt-get dist-upgrade -y`,
// retrying while another process holds the apt lock. If the upgrade still
// fails it runs `dpkg --configure -a` (under a fresh context) so a
// half-applied transaction is left as consistent as possible. Output from
// every command is captured in execution order.
func RunAptUpgrade(ctx context.Context, excludedPackages []string) Result {
	var out bytes.Buffer

	// Refresh lists so the upgrade reflects current apt state. Non-fatal: a
	// failure here just means we apply whatever apt already knows.
	if err := aptCommand(ctx, &out, "apt-get", "update").Run(); err != nil {
		out.WriteString("\n[cadence] apt-get update failed; using existing lists\n")
	}

	heldNames := reconcileHolds(ctx, &out, excludedPackages)

	res, lastAttemptOutput := runDistUpgrade(ctx, &out)

	if res.Err != nil {
		out.WriteString("\n[cadence] apt failed; running dpkg --configure -a\n")
		fixCtx, cancel := context.WithTimeout(context.Background(), dpkgConfigureTimeout)
		_ = aptCommand(fixCtx, &out, "dpkg", "--configure", "-a").Run()
		cancel()
	}

	if res.ExitCode != 0 || res.Err != nil {
		classifyFailure(ctx, &res, lastAttemptOutput, out.String())
	}
	res.HeldConflicts = holds.Correlate(lastAttemptOutput, heldNames)

	res.Log = capLog(out.String())
	res.RebootRequired = rebootcheck.Pending()
	return res
}

// reconcileHolds aligns dpkg's hold state with excludedPackages, the exact
// package names the server resolved for this host (roadmap item 3). Every
// apt_upgrade run reconciles from scratch: hold whatever should be held,
// unhold anything currently held that fell off the list, so `apt-mark
// showhold` always reflects current policy, never an orphan from an
// interrupted or superseded run. Returns the names actually held afterward.
// Best-effort throughout: a hold/unhold problem is logged into out and never
// fails the job -- the upgrade itself is the point of the run.
func reconcileHolds(ctx context.Context, out *bytes.Buffer, excludedPackages []string) []string {
	valid, rejected := holds.Filter(excludedPackages)
	for _, name := range rejected {
		fmt.Fprintf(out, "\n[cadence] ignoring invalid excluded-package name from server: %q\n", name)
	}

	// Always check for an orphaned hold, even when valid is empty (a policy
	// removed since the last run): apt-mark showhold is a cheap local dpkg
	// query, no network involved.
	var showhold bytes.Buffer
	var current []string
	if err := aptCommand(ctx, &showhold, "apt-mark", "showhold").Run(); err != nil {
		out.WriteString("\n[cadence] apt-mark showhold failed; assuming nothing is currently held\n")
	} else {
		current = holds.ParseShowHold(showhold.String())
	}

	toHold, toUnhold := holds.Diff(current, valid)
	held := applyMark(ctx, out, "hold", toHold)
	unheld := applyMark(ctx, out, "unhold", toUnhold)
	if len(toHold) > 0 || len(toUnhold) > 0 {
		fmt.Fprintf(out, "\n[cadence] policy: holding %d package(s), releasing %d\n", len(held), len(unheld))
	}

	// Final held set = names already held and still wanted, plus names newly
	// held this run.
	final := make(map[string]bool, len(current)+len(held))
	validSet := make(map[string]bool, len(valid))
	for _, n := range valid {
		validSet[n] = true
	}
	for _, n := range current {
		if validSet[n] {
			final[n] = true
		}
	}
	for _, n := range held {
		final[n] = true
	}
	names := make([]string, 0, len(final))
	for n := range final {
		names = append(names, n)
	}
	sort.Strings(names)
	return names
}

// applyMark runs `apt-mark <verb> <names...>` and returns the names it
// actually applied to. It tries the whole batch first; if that fails (e.g.
// one name is stale -- present in the server's resolved list but no longer
// installed locally, possible if the inventory is slightly out of date), it
// retries one name at a time so a single bad name cannot block the rest, and
// logs each individual failure into out.
func applyMark(ctx context.Context, out *bytes.Buffer, verb string, names []string) []string {
	if len(names) == 0 {
		return nil
	}
	args := append([]string{verb}, names...)
	if aptCommand(ctx, out, "apt-mark", args...).Run() == nil {
		return names
	}

	var applied []string
	for _, name := range names {
		var attempt bytes.Buffer
		if aptCommand(ctx, &attempt, "apt-mark", verb, name).Run() == nil {
			applied = append(applied, name)
		} else {
			fmt.Fprintf(out, "\n[cadence] apt-mark %s %s failed: %s\n",
				verb, name, strings.TrimSpace(attempt.String()))
		}
	}
	return applied
}

// classifyFailure fills res.FailureCategory / res.FailureSummary. It classifies
// the isolated output of the dist-upgrade attempt that failed (so an earlier
// retry's held-lock line cannot mislabel a job that ultimately failed for
// another reason). Only when that is inconclusive does it fall back to the full
// captured log, which also covers a failure whose signal is in the `apt-get
// update` phase or the `dpkg --configure -a` recovery run.
func classifyFailure(ctx context.Context, res *Result, failingOutput, fullLog string) {
	deadline := ctx.Err() == context.DeadlineExceeded
	cat, summary := apterr.Classify(failingOutput, deadline)
	if !deadline && cat == apterr.CategoryUnknown {
		if c2, s2 := apterr.Classify(stripAnnotations(fullLog), false); c2 != apterr.CategoryUnknown {
			cat, summary = c2, s2
		}
	}
	res.FailureCategory = cat
	res.FailureSummary = summary
}

// stripAnnotations drops the "[cadence] ..." breadcrumb lines this package
// writes into the log, so the fallback classification sees only real apt/dpkg
// output (one breadcrumb mentions `dpkg --configure -a` verbatim, which would
// otherwise trip apterr's interrupted-state pattern).
func stripAnnotations(s string) string {
	lines := strings.Split(s, "\n")
	kept := lines[:0]
	for _, ln := range lines {
		if strings.HasPrefix(strings.TrimSpace(ln), "[cadence]") {
			continue
		}
		kept = append(kept, ln)
	}
	return strings.Join(kept, "\n")
}

// runDistUpgrade runs `apt-get -y dist-upgrade`, retrying on a held apt lock.
// Every attempt's output is appended to out in order. The second return value
// is the isolated output of the final attempt -- success or failure, empty
// only when the context is cancelled before any attempt runs -- used for
// failure classification and for correlating a hold to the outcome.
func runDistUpgrade(ctx context.Context, out *bytes.Buffer) (Result, string) {
	var res Result
	last := len(aptLockRetryWaits) - 1
	for i, wait := range aptLockRetryWaits {
		if wait > 0 {
			out.WriteString("\n[cadence] apt lock held, retrying dist-upgrade\n")
			select {
			case <-time.After(wait):
			case <-ctx.Done():
				return Result{ExitCode: -1, Err: ctx.Err()}, ""
			}
		}

		var attempt bytes.Buffer
		err := aptCommand(ctx, &attempt, "apt-get",
			"-o", "Dpkg::Options::=--force-confdef",
			"-o", "Dpkg::Options::=--force-confold",
			"-y", "dist-upgrade",
		).Run()
		out.Write(attempt.Bytes())

		if err == nil {
			return Result{}, attempt.String()
		}

		res = Result{Err: err, ExitCode: -1} // -1: failed to start, or killed
		if exitErr, ok := err.(*exec.ExitError); ok {
			res.ExitCode = exitErr.ExitCode()
		}

		// Only a held lock is worth another attempt; a real package failure
		// (or an expired deadline) is final.
		if i == last || !apterr.IsLockHeld(attempt.String()) {
			return res, attempt.String()
		}
	}
	return res, ""
}
