// Package executor runs an apt upgrade for an apt_upgrade job and captures its
// combined output. It never reboots; it only reports whether a reboot became
// required (report-only -- see docs/decisions.md, "Updates").
package executor

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os/exec"
	"sort"
	"strings"
	"syscall"
	"time"

	"cadence/agent/internal/apterr"
	"cadence/agent/internal/healthcheck"
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

const (
	preCheckTimeout       = 10 * time.Minute
	aptUpgradeTimeout     = 30 * time.Minute
	postCheckTimeout      = 5 * time.Minute
	defaultLockWait       = 120 * time.Second
	maximumLockWait       = time.Hour
	defaultLockPollPeriod = time.Second
)

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

	// HeldPackages is the set Cadence actually holds after this run's
	// reconciliation (may differ from what was requested if apt-mark failed
	// on a stale name). Becomes the server's known_held_packages for the
	// next job. Same never-omitted convention as HeldConflicts.
	HeldPackages []string

	// PreChecks and PostChecks describe host validation separately from the
	// apt action outcome. PostChecks is nil when the pre-check phase blocked
	// the action. HealthStatus is derived only from completed post-checks.
	PreChecks    *healthcheck.Phase
	PostChecks   *healthcheck.Phase
	HealthStatus healthcheck.HealthStatus
}

// CheckOptions controls the thresholds and injectable operating-system probes
// used around an upgrade. Zero values select conservative defaults.
type CheckOptions struct {
	MinimumAvailableBytes     uint64
	BootMinimumAvailableBytes uint64
	LockWait                  time.Duration

	Runner            healthcheck.Runner
	DiskProbe         healthcheck.DiskProbe
	LockProbe         healthcheck.LockProbe
	RebootProbe       func() bool
	LockPollingPeriod time.Duration
}

// DefaultCheckOptions returns the policy used when an older server supplies no
// health-check settings.
func DefaultCheckOptions() CheckOptions {
	return CheckOptions{
		MinimumAvailableBytes:     healthcheck.DefaultMinimumAvailableBytes,
		BootMinimumAvailableBytes: healthcheck.DefaultBootMinimumAvailableBytes,
		LockWait:                  defaultLockWait,
		Runner:                    healthcheck.ExecRunner{},
		DiskProbe:                 healthcheck.ProbeFilesystem,
		LockProbe:                 healthcheck.ProbePackageManagerLocks,
		RebootProbe:               rebootcheck.Pending,
		LockPollingPeriod:         defaultLockPollPeriod,
	}
}

// CheckOptionsFromThresholds overlays non-zero server settings on the agent
// defaults. The lock wait is capped before conversion to time.Duration.
func CheckOptionsFromThresholds(minimumBytes, bootMinimumBytes, lockWaitSeconds uint64) CheckOptions {
	options := DefaultCheckOptions()
	if minimumBytes > 0 {
		options.MinimumAvailableBytes = minimumBytes
	}
	if bootMinimumBytes > 0 {
		options.BootMinimumAvailableBytes = bootMinimumBytes
	}
	if lockWaitSeconds > 0 {
		maximumSeconds := uint64(maximumLockWait / time.Second)
		if lockWaitSeconds > maximumSeconds {
			lockWaitSeconds = maximumSeconds
		}
		options.LockWait = time.Duration(lockWaitSeconds) * time.Second
	}
	return options
}

func (o CheckOptions) withDefaults() CheckOptions {
	defaults := DefaultCheckOptions()
	if o.MinimumAvailableBytes == 0 {
		o.MinimumAvailableBytes = defaults.MinimumAvailableBytes
	}
	if o.BootMinimumAvailableBytes == 0 {
		o.BootMinimumAvailableBytes = defaults.BootMinimumAvailableBytes
	}
	if o.LockWait <= 0 {
		o.LockWait = defaults.LockWait
	} else if o.LockWait > maximumLockWait {
		o.LockWait = maximumLockWait
	}
	if o.Runner == nil {
		o.Runner = defaults.Runner
	}
	if o.DiskProbe == nil {
		o.DiskProbe = defaults.DiskProbe
	}
	if o.LockProbe == nil {
		o.LockProbe = defaults.LockProbe
	}
	if o.RebootProbe == nil {
		o.RebootProbe = defaults.RebootProbe
	}
	if o.LockPollingPeriod <= 0 {
		o.LockPollingPeriod = defaults.LockPollingPeriod
	}
	return o
}

var errPreChecks = errors.New("upgrade blocked by pre-checks")

// RunAptUpgrade validates the host, runs the apt action only when every
// blocking pre-check passes, then validates the resulting host state. A clean
// apt exit and post-upgrade health are deliberately independent outcomes.
func RunAptUpgrade(ctx context.Context, excludedPackages, knownHeldPackages []string, options CheckOptions) Result {
	options = options.withDefaults()

	preCtx, preCancel := context.WithTimeout(ctx, preCheckTimeout)
	pre := runPreChecks(preCtx, options)
	preErr := preCtx.Err()
	preCancel()
	if blocking := firstBlockingCheck(pre.Checks); blocking != nil {
		category := preCheckFailureCategory(*blocking)
		if errors.Is(preErr, context.DeadlineExceeded) {
			category = apterr.CategoryTimeout
		}
		return Result{
			ExitCode:        -1,
			Err:             errPreChecks,
			FailureCategory: category,
			FailureSummary:  blocking.Summary,
			PreChecks:       &pre,
			HealthStatus:    healthcheck.HealthUnknown,
			Log: capLog(formatCheckLog("pre-check", pre) +
				"[cadence] apt upgrade blocked by pre-checks\n"),
		}
	}

	actionCtx, actionCancel := context.WithTimeout(ctx, aptUpgradeTimeout)
	result := runAptUpgradeAction(actionCtx, excludedPackages, knownHeldPackages)
	actionCancel()
	result.PreChecks = &pre

	postCtx, postCancel := context.WithTimeout(ctx, postCheckTimeout)
	post, rebootRequired := runPostChecks(postCtx, options, failedServicesBaseline(pre.Checks))
	postCancel()
	result.PostChecks = &post
	result.HealthStatus = healthcheck.HealthFromPostChecks(post.Checks)
	result.RebootRequired = rebootRequired
	result.Log = capLog(formatCheckLog("pre-check", pre) + result.Log + formatCheckLog("post-check", post))
	return result
}

func runPreChecks(ctx context.Context, options CheckOptions) healthcheck.Phase {
	targets := []healthcheck.DiskTarget{
		{Path: "/var", MinimumAvailableBytes: options.MinimumAvailableBytes},
		{Path: "/boot", MinimumAvailableBytes: options.BootMinimumAvailableBytes},
		{Path: "/boot/efi", MinimumAvailableBytes: options.BootMinimumAvailableBytes},
	}

	checks := make([]healthcheck.Result, 0, 6)
	add := func(result healthcheck.Result) bool {
		checks = append(checks, result)
		return result.Status == healthcheck.StatusFailed || result.Status == healthcheck.StatusUnknown
	}
	remaining := []string{
		healthcheck.CheckPackageLocks,
		healthcheck.CheckDPKGAudit,
		healthcheck.CheckAPTDependencies,
		healthcheck.CheckFailedServices,
		healthcheck.CheckPackageIndexes,
	}
	if add(healthcheck.DiskSpace(targets, options.DiskProbe)) {
		return phaseWithSkipped(checks, remaining)
	}

	lockCtx, lockCancel := context.WithTimeout(ctx, options.LockWait)
	locks := healthcheck.PackageManagerLocks(lockCtx, healthcheck.DefaultLockPaths, options.LockPollingPeriod, options.LockProbe)
	lockCancel()
	if add(locks) {
		return phaseWithSkipped(checks, remaining[1:])
	}
	if add(healthcheck.DPKGAudit(ctx, options.Runner)) {
		return phaseWithSkipped(checks, remaining[2:])
	}
	if add(healthcheck.APTDependencies(ctx, options.Runner)) {
		return phaseWithSkipped(checks, remaining[3:])
	}
	if add(healthcheck.FailedServices(ctx, options.Runner)) {
		return phaseWithSkipped(checks, remaining[4:])
	}
	add(healthcheck.PackageIndexes(ctx, options.Runner))
	return healthcheck.NewPhase(checks...)
}

func phaseWithSkipped(checks []healthcheck.Result, names []string) healthcheck.Phase {
	for _, name := range names {
		checks = append(checks, healthcheck.Result{
			Name: name, Status: healthcheck.StatusSkipped,
			Summary: "not run after an earlier blocking check",
		})
	}
	return healthcheck.NewPhase(checks...)
}

func runPostChecks(ctx context.Context, options CheckOptions, failedServices []string) (healthcheck.Phase, bool) {
	rebootRequired := options.RebootProbe()
	checks := []healthcheck.Result{
		healthcheck.DPKGAudit(ctx, options.Runner),
		healthcheck.APTDependencies(ctx, options.Runner),
		healthcheck.DiskSpace([]healthcheck.DiskTarget{
			{Path: "/var", MinimumAvailableBytes: options.MinimumAvailableBytes},
			{Path: "/boot", MinimumAvailableBytes: options.BootMinimumAvailableBytes},
			{Path: "/boot/efi", MinimumAvailableBytes: options.BootMinimumAvailableBytes},
		}, options.DiskProbe),
		healthcheck.FailedServicesAfter(ctx, options.Runner, failedServices),
		healthcheck.RebootRequired(rebootRequired),
	}
	return healthcheck.NewPhase(checks...), rebootRequired
}

func firstBlockingCheck(checks []healthcheck.Result) *healthcheck.Result {
	for i := range checks {
		if checks[i].Status == healthcheck.StatusFailed || checks[i].Status == healthcheck.StatusUnknown {
			return &checks[i]
		}
	}
	return nil
}

func preCheckFailureCategory(check healthcheck.Result) string {
	if check.Status == healthcheck.StatusUnknown {
		return apterr.CategoryAgentRefused
	}
	switch check.Name {
	case healthcheck.CheckDiskSpace:
		return apterr.CategoryDiskFull
	case healthcheck.CheckPackageLocks:
		return apterr.CategoryAptLocked
	case healthcheck.CheckDPKGAudit, healthcheck.CheckAPTDependencies:
		return apterr.CategoryDpkgError
	case healthcheck.CheckPackageIndexes:
		return apterr.CategoryNetworkOrRepo
	default:
		return apterr.CategoryAgentRefused
	}
}

func failedServicesBaseline(checks []healthcheck.Result) []string {
	for _, check := range checks {
		if check.Name == healthcheck.CheckFailedServices {
			return check.Details.Services
		}
	}
	return nil
}

func formatCheckLog(label string, phase healthcheck.Phase) string {
	var out strings.Builder
	for _, check := range phase.Checks {
		fmt.Fprintf(&out, "[cadence] %s %s: %s: %s\n", label, check.Name, check.Status, check.Summary)
	}
	return out.String()
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

// runAptUpgradeAction reconciles dpkg's hold state to excludedPackages against
// knownHeldPackages, then runs a non-interactive
// `apt-get dist-upgrade -y`, retrying while another process holds the apt
// lock. If the upgrade still fails it runs `dpkg --configure -a` (under a
// fresh context) so a half-applied transaction is left as consistent as
// possible. Output from every command is captured in execution order.
func runAptUpgradeAction(ctx context.Context, excludedPackages, knownHeldPackages []string) Result {
	var out bytes.Buffer

	heldNames := reconcileHolds(ctx, &out, excludedPackages, knownHeldPackages)

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
	res.HeldPackages = heldNames

	res.Log = capLog(out.String())
	return res
}

// reconcileHolds aligns dpkg's hold state with excludedPackages, using
// knownHeldPackages -- the set the server last recorded Cadence itself
// holding for this host -- as the reconciliation baseline, NOT a live
// `apt-mark showhold` read. This is deliberate: apt-mark showhold reports
// every held package on the box, including ones a third party (the operator
// by hand, unattended-upgrades, a distro default) put there for its own
// reasons -- diffing against it would unhold them the moment they are not in
// excludedPackages, which is not Cadence's call to make. A hold neither known
// nor wanted is left untouched. Returns the names Cadence actually holds
// after this run (fed back to the server as the next known_held_packages).
// Best-effort throughout: a hold/unhold problem is logged into out and never
// fails the job -- the upgrade itself is the point of the run.
func reconcileHolds(ctx context.Context, out *bytes.Buffer, excludedPackages, knownHeldPackages []string) []string {
	valid, rejected := holds.Filter(excludedPackages)
	for _, name := range rejected {
		fmt.Fprintf(out, "\n[cadence] ignoring invalid excluded-package name from server: %q\n", name)
	}
	known, knownRejected := holds.Filter(knownHeldPackages)
	for _, name := range knownRejected {
		fmt.Fprintf(out, "\n[cadence] ignoring invalid known-held-package name from server: %q\n", name)
	}

	// Informational only, never fed into the diff below: lets an operator
	// reading the job log notice e.g. a third-party hold in passing.
	var showhold bytes.Buffer
	if err := aptCommand(ctx, &showhold, "apt-mark", "showhold").Run(); err == nil {
		fmt.Fprintf(out, "\n[cadence] apt-mark currently reports %d package(s) on hold (informational)\n",
			len(holds.ParseShowHold(showhold.String())))
	}

	toHold, toUnhold := holds.Diff(known, valid)
	held := applyMark(ctx, out, "hold", toHold)
	unheld := applyMark(ctx, out, "unhold", toUnhold)
	if len(toHold) > 0 || len(toUnhold) > 0 {
		fmt.Fprintf(out, "\n[cadence] policy: holding %d package(s), releasing %d\n", len(held), len(unheld))
	}

	// Final Cadence-managed held set = names already known-held and still
	// wanted, plus names newly held this run.
	final := make(map[string]bool, len(known)+len(held))
	validSet := make(map[string]bool, len(valid))
	for _, n := range valid {
		validSet[n] = true
	}
	for _, n := range known {
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
