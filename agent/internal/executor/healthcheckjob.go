package executor

import (
	"context"

	"cadence/agent/internal/healthcheck"
)

// HealthCheckOutcome is what RunHealthCheck reports back for a health_check
// job. It mirrors the fields of Result that a pure-read job can produce: no
// apt action, no held-package fields, no pre-checks -- just a post-check
// phase and the health_status derived from it.
type HealthCheckOutcome struct {
	// Status is "succeeded" | "failed". A health_check has no apt action, so
	// this is "failed" only if the checks themselves could not run at all;
	// none of the reused healthcheck.* probes bubble a Go error today (each
	// absorbs its own failure into Result.Status), so in practice this is
	// always "succeeded" -- the interesting signal lives entirely in
	// PostChecks / HealthStatus, the same separation apt_upgrade keeps
	// between its action status and derived health.
	Status          string
	ExitCode        int
	Log             string
	FailureCategory string
	FailureSummary  string

	RebootRequired bool
	PostChecks     *healthcheck.Phase
	HealthStatus   healthcheck.HealthStatus
}

// RunHealthCheck reruns the same read-only checks as an apt_upgrade's
// post-check phase (dpkg audit, apt dependencies, disk space, failed
// services, reboot required) with no upgrade attached, and derives a fresh
// health_status. It never mutates the host: no apt-mark, no dpkg, no
// dist-upgrade.
//
// Failed services uses healthcheck.FailedServices (a currently-failed
// service is a non-blocking "warning"), not FailedServicesAfter: a
// standalone health_check has no pre-run baseline within the same job to
// diff against, and reporting every already-broken service as freshly
// "failed" would manufacture a false "just broke" signal nothing in this job
// caused.
func RunHealthCheck(ctx context.Context, options CheckOptions) HealthCheckOutcome {
	options = options.withDefaults()
	rebootRequired := options.RebootProbe()

	checks := []healthcheck.Result{
		healthcheck.DPKGAudit(ctx, options.Runner),
		healthcheck.APTDependencies(ctx, options.Runner),
		healthcheck.DiskSpace([]healthcheck.DiskTarget{
			{Path: "/var", MinimumAvailableBytes: options.MinimumAvailableBytes},
			{Path: "/boot", MinimumAvailableBytes: options.BootMinimumAvailableBytes},
			{Path: "/boot/efi", MinimumAvailableBytes: options.BootMinimumAvailableBytes},
		}, options.DiskProbe),
		healthcheck.FailedServices(ctx, options.Runner),
		healthcheck.RebootRequired(rebootRequired),
	}
	post := healthcheck.NewPhase(checks...)

	return HealthCheckOutcome{
		Status:         "succeeded",
		ExitCode:       0,
		Log:            capLog(formatCheckLog("check", post)),
		RebootRequired: rebootRequired,
		PostChecks:     &post,
		HealthStatus:   healthcheck.HealthFromPostChecks(post.Checks),
	}
}
