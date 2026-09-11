// Command agent has four one-shot modes. Three are driven by systemd timers:
//
//	cadence-agent                    collect package/OS state, POST a report,
//	                                 and run a job if one is piggybacked
//	cadence-agent -poll              just ask the server for a pending job and
//	                                 run it (fast path, no collection)
//	cadence-agent -health-check-boot ask the server to create-and-claim a
//	                                 health_check job for this boot and run it
//	                                 if one is returned
//	cadence-agent -enroll ...        exchange a manually entered, one-time code
//	                                 for HMAC and mTLS credentials
//
// Communication stays outbound-only; -poll and -health-check-boot are short
// polls, not long polls. -poll and -health-check-boot are mutually exclusive.
package main

import (
	"context"
	"flag"
	"fmt"
	"io"
	"log"
	"os"
	"time"

	"cadence/agent/internal/apterr"
	"cadence/agent/internal/client"
	"cadence/agent/internal/collector"
	"cadence/agent/internal/config"
	"cadence/agent/internal/enrollment"
	"cadence/agent/internal/executor"
	"cadence/agent/internal/logging"
	"cadence/agent/internal/reboot"
	"cadence/agent/internal/renewal"
	"cadence/agent/internal/report"
)

// agentVersion is the fallback version for dev / untagged builds. Release
// builds override it with the git tag via
// -ldflags "-X main.agentVersion=<version>" (see scripts/publish-agent.sh).
// Keep this literal in step with the newest agent/CHANGELOG.md heading.
var agentVersion = "0.14.0"

// Run-phase timeouts. Each systemd unit's TimeoutStartSec MUST comfortably
// exceed the sum of the timeouts on its path, or systemd SIGKILLs the whole
// cgroup mid apt-get / dpkg:
//
//	cadence-agent.service       reportTimeout + jobTimeout + postJobReportTimeout
//	cadence-agent-poll.service  jobTimeout + postJobReportTimeout
const (
	reportTimeout        = 10 * time.Minute // collect + POST /reports
	jobTimeout           = 60 * time.Minute // checks + apt action + bounded dpkg recovery
	postJobReportTimeout = 5 * time.Minute  // the fresh report sent after a job
	dryRunTimeout        = 5 * time.Minute  // apt-get update + -s dist-upgrade
	healthCheckTimeout   = 5 * time.Minute  // dpkg/apt/disk/service/reboot probes only
)

func main() {
	log.SetFlags(0)
	log.SetPrefix("cadence-agent: ")

	pollOnly := flag.Bool("poll", false,
		"check for a pending job and run it, without collecting or reporting packages")
	healthCheckBoot := flag.Bool("health-check-boot", false,
		"ask the server to create-and-claim a health_check job for this boot and run it if one is returned")
	enroll := flag.Bool("enroll", false, "enroll this host using a code read from standard input")
	enrollServer := flag.String("enroll-server", "", "HTTPS dashboard URL used only for enrollment")
	enrollCA := flag.String("enroll-ca", "", "fingerprint-verified server CA file")
	enrollDirectory := flag.String("enroll-directory", "/etc/cadence", "credential output directory")
	showVersion := flag.Bool("version", false, "print the agent version and exit")
	flag.Parse()

	if *showVersion {
		fmt.Println(agentVersion)
		return
	}
	modeCount := 0
	for _, selected := range []bool{*pollOnly, *healthCheckBoot, *enroll} {
		if selected {
			modeCount++
		}
	}
	if modeCount > 1 {
		logging.Error("agent run failed", "err", "-poll, -health-check-boot and -enroll are mutually exclusive")
		os.Exit(1)
	}
	if *enroll {
		if err := runEnrollment(*enrollServer, *enrollCA, *enrollDirectory); err != nil {
			logging.Error("enrollment failed", "err", err)
			os.Exit(1)
		}
		return
	}

	if err := run(*pollOnly, *healthCheckBoot); err != nil {
		logging.Error("agent run failed", "err", err)
		os.Exit(1)
	}
}

func runEnrollment(serverURL, caFile, directory string) error {
	if serverURL == "" || caFile == "" {
		return fmt.Errorf("-enroll-server and -enroll-ca are required with -enroll")
	}
	fmt.Fprint(os.Stderr, "Enrollment code: ")
	input, err := io.ReadAll(io.LimitReader(os.Stdin, 512))
	if err != nil {
		return fmt.Errorf("reading enrollment code: %w", err)
	}
	hostname, err := os.Hostname()
	if err != nil {
		return fmt.Errorf("reading hostname: %w", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	result, err := enrollment.Enroll(ctx, serverURL, caFile, string(input), hostname, 30*time.Second)
	if err != nil {
		return err
	}
	if err := enrollment.WriteCredentials(directory, caFile, result); err != nil {
		return err
	}
	fmt.Fprintf(os.Stderr, "enrolled host %s; credentials written to %s\n", result.HostID, directory)
	return nil
}

func run(pollOnly, healthCheckBoot bool) error {
	cfg, err := config.Load()
	if err != nil {
		return err
	}
	if cfg.ClientCertFile != "" {
		ctx, cancel := context.WithTimeout(context.Background(), cfg.HTTPTimeout)
		renewed, renewErr := renewal.RunIfNeeded(ctx, cfg)
		cancel()
		if renewErr != nil {
			return fmt.Errorf("renewing client certificate: %w", renewErr)
		}
		if renewed {
			logging.Info("client certificate renewed; new credentials activate on next run")
		}
	}
	c := client.New(cfg.ServerURL, cfg.Token, cfg.HTTPTimeout)
	if cfg.ClientCertFile != "" {
		c, err = client.NewMTLS(
			cfg.ServerURL,
			cfg.Token,
			cfg.ClientCertFile,
			cfg.ClientKeyFile,
			cfg.ServerCAFile,
			cfg.HTTPTimeout,
		)
		if err != nil {
			return err
		}
	}

	if healthCheckBoot {
		ctx, cancel := context.WithTimeout(context.Background(), cfg.HTTPTimeout)
		defer cancel()

		job, err := c.ClaimHealthCheckJob(ctx)
		if err != nil {
			return err
		}
		if job == nil {
			return nil // host busy with another job, or nothing to do
		}
		return runJob(cfg, c, job)
	}

	if pollOnly {
		ctx, cancel := context.WithTimeout(context.Background(), cfg.HTTPTimeout)
		defer cancel()

		job, err := c.ClaimNextJob(ctx)
		if err != nil {
			return err
		}
		if job == nil {
			return nil // nothing pending; stay quiet
		}
		return runJob(cfg, c, job)
	}

	// Full path: collect, report, then run a piggybacked job if there is one.
	reportCtx, cancel := context.WithTimeout(context.Background(), reportTimeout)
	defer cancel()

	rep, err := collector.Collect(reportCtx, agentVersion, cfg.RunAptUpdate)
	if err != nil {
		return err
	}

	job, err := c.SendReport(reportCtx, rep)
	if err != nil {
		return err
	}

	updates, security := rep.Counts()
	logging.Info("report sent",
		"server", cfg.ServerURL, "host", rep.Hostname, "packages", len(rep.Packages),
		"updates", updates, "security", security, "reboot_required", rep.RebootRequired)

	if job == nil {
		return nil
	}
	return runJob(cfg, c, job)
}

func runJob(cfg config.Config, c *client.Client, job *report.JobHandoff) error {
	logging.Info("job received", "job_id", job.ID, "job_type", job.JobType)

	submit := func(result client.JobResult) error {
		ctx, cancel := context.WithTimeout(context.Background(), cfg.HTTPTimeout)
		defer cancel()
		return c.SubmitJobResult(ctx, job.ID, result)
	}
	// refused reports a job the agent declined before running anything: the
	// category is fixed, the reason is the message itself.
	refused := func(msg string) error {
		return submit(client.JobResult{
			Status: "failed", ExitCode: 0, Log: msg,
			FailureCategory: apterr.CategoryAgentRefused, FailureSummary: msg,
		})
	}

	// Dedicated reboot job (reboot_policy "prompt" / a "reboot now" from the
	// dashboard): no upgrade, just reboot. Report success before issuing it so
	// the job never hangs in "running".
	if job.JobType == "reboot" {
		if !cfg.EnableReboot {
			return refused("reboot is disabled on this host (CADENCE_ENABLE_REBOOT=false)")
		}
		logging.Info("dedicated reboot job -> systemctl --no-block reboot", "job_id", job.ID)
		if err := submit(client.JobResult{
			Status: "succeeded", ExitCode: 0,
			Log:            "[cadence] reboot requested via dedicated job -> systemctl --no-block reboot\n",
			RebootRequired: true,
		}); err != nil {
			return fmt.Errorf("submitting job result: %w", err)
		}
		rctx, rcancel := context.WithTimeout(context.Background(), cfg.HTTPTimeout)
		defer rcancel()
		if err := reboot.Issue(rctx); err != nil {
			return fmt.Errorf("issuing reboot: %w", err)
		}
		logging.Info("reboot queued", "job_id", job.ID)
		return nil
	}

	// Dry-run: a pure-read "what would apt_upgrade do" preview. Placed before
	// the CADENCE_ENABLE_UPGRADES gate on purpose -- it changes nothing on the
	// host (no apt-mark, no dpkg, no -y upgrade), so previewing pending changes
	// is useful even where real upgrades are switched off.
	if job.JobType == "apt_dry_run" {
		dctx, dcancel := context.WithTimeout(context.Background(), dryRunTimeout)
		defer dcancel()

		logging.Info("running apt-get -s dist-upgrade (dry run)", "job_id", job.ID)
		res := executor.RunAptDryRun(dctx, job.ExcludedPackages())
		logging.Info("dry run finished", "job_id", job.ID, "status", res.Status)

		if err := submit(client.JobResult{
			Status: res.Status, ExitCode: res.ExitCode, Log: res.Log,
			FailureCategory: res.FailureCategory, FailureSummary: res.FailureSummary,
			DryRun: res.DryRun,
		}); err != nil {
			return fmt.Errorf("submitting job result: %w", err)
		}
		if res.Status == "failed" {
			return fmt.Errorf("dry-run job %s failed", job.ID)
		}
		return nil
	}

	// Health check: reruns the same post-check style probes as an
	// apt_upgrade's post-check phase, with no upgrade attached. Placed
	// before the CADENCE_ENABLE_UPGRADES gate for the same reason as
	// apt_dry_run -- it changes nothing on the host. Reached either from a
	// manually triggered dashboard job (via the normal report/next-job
	// piggyback) or from -health-check-boot.
	if job.JobType == "health_check" {
		hctx, hcancel := context.WithTimeout(context.Background(), healthCheckTimeout)
		defer hcancel()

		settings := job.HealthCheckSettings()
		checkOptions := executor.CheckOptionsFromThresholds(
			settings.MinimumAvailableBytes, settings.BootMinimumAvailableBytes, settings.LockWaitSeconds,
		)
		logging.Info("running health check", "job_id", job.ID)
		res := executor.RunHealthCheck(hctx, checkOptions)
		logging.Info("health check finished", "job_id", job.ID, "health_status", res.HealthStatus)

		if err := submit(client.JobResult{
			Status: res.Status, ExitCode: res.ExitCode, Log: res.Log,
			RebootRequired:  res.RebootRequired,
			FailureCategory: res.FailureCategory, FailureSummary: res.FailureSummary,
			PostChecks: res.PostChecks, HealthStatus: res.HealthStatus,
		}); err != nil {
			return fmt.Errorf("submitting job result: %w", err)
		}
		if res.Status == "failed" {
			return fmt.Errorf("health-check job %s failed", job.ID)
		}
		return nil
	}

	if !cfg.EnableUpgrades {
		logging.Warn("upgrades disabled on this host, reporting job as failed", "job_id", job.ID)
		return refused("upgrades are disabled on this host (CADENCE_ENABLE_UPGRADES=false)")
	}
	if job.JobType != "apt_upgrade" {
		return refused("unsupported job type: " + job.JobType)
	}

	ctx, cancel := context.WithTimeout(context.Background(), jobTimeout)
	defer cancel()

	settings := job.HealthCheckSettings()
	checkOptions := executor.CheckOptionsFromThresholds(
		settings.MinimumAvailableBytes,
		settings.BootMinimumAvailableBytes,
		settings.LockWaitSeconds,
	)
	logging.Info("running apt-get dist-upgrade", "job_id", job.ID)
	res := executor.RunAptUpgrade(ctx, job.ExcludedPackages(), job.KnownHeldPackages(), checkOptions)

	status := "succeeded"
	if res.ExitCode != 0 || res.Err != nil {
		status = "failed"
	}
	logging.Info("apt-get dist-upgrade finished",
		"job_id", job.ID, "status", status, "exit_code", res.ExitCode,
		"health_status", res.HealthStatus, "reboot_required", res.RebootRequired)

	// Decide about the reboot. The server already resolved the effective mode
	// (per-job override, else host reboot_policy) into params.reboot.
	willReboot, logSuffix := rebootDecision(status, res.RebootRequired, cfg.EnableReboot, job.RebootMode())
	logText := res.Log + logSuffix

	if err := submit(client.JobResult{
		Status: status, ExitCode: res.ExitCode, Log: logText,
		RebootRequired:  res.RebootRequired,
		FailureCategory: res.FailureCategory, FailureSummary: res.FailureSummary,
		HeldConflicts: res.HeldConflicts, HeldPackages: res.HeldPackages,
		PreChecks: res.PreChecks, PostChecks: res.PostChecks, HealthStatus: res.HealthStatus,
	}); err != nil {
		return fmt.Errorf("submitting job result: %w", err)
	}

	// Push a fresh report right away so the dashboard reflects the new package
	// state (usually "up to date") without waiting for the next scheduled run.
	// Best-effort: the job already succeeded/failed on its own. Do this before
	// any reboot so the server has the update before the host goes down.
	reportAfterJob(cfg, c)

	if status == "failed" {
		return fmt.Errorf("job %s failed (apt exit %d)", job.ID, res.ExitCode)
	}

	if willReboot {
		logging.Info("reboot required and mode=auto -> systemctl --no-block reboot", "job_id", job.ID)
		rctx, rcancel := context.WithTimeout(context.Background(), cfg.HTTPTimeout)
		defer rcancel()
		if err := reboot.Issue(rctx); err != nil {
			return fmt.Errorf("issuing reboot: %w", err)
		}
		logging.Info("reboot queued", "job_id", job.ID)
	}
	return nil
}

// rebootDecision resolves whether a finished apt_upgrade job should trigger a
// reboot, and the explanatory line to append to the job log. `mode` is the
// server-resolved reboot mode (params.reboot, else host reboot_policy); "" is
// treated as "never".
func rebootDecision(status string, rebootRequired, enableReboot bool, mode string) (willReboot bool, logSuffix string) {
	if mode == "" {
		mode = "never"
	}
	if status != "succeeded" || !rebootRequired {
		return false, ""
	}
	switch {
	case mode != "auto":
		return false, fmt.Sprintf("\n[cadence] reboot required; reboot mode is %q -> not rebooting\n", mode)
	case !enableReboot:
		return false, "\n[cadence] reboot required and reboot mode is \"auto\", but CADENCE_ENABLE_REBOOT=false -> not rebooting\n"
	default:
		return true, "\n[cadence] reboot required and reboot mode is \"auto\" -> rebooting via systemctl --no-block reboot\n"
	}
}

// reportAfterJob collects and sends one report. Failures are logged, not
// propagated: the job it follows has already been recorded.
func reportAfterJob(cfg config.Config, c *client.Client) {
	ctx, cancel := context.WithTimeout(context.Background(), postJobReportTimeout)
	defer cancel()

	rep, err := collector.Collect(ctx, agentVersion, cfg.RunAptUpdate)
	if err != nil {
		logging.Warn("post-job report: collect failed", "err", err)
		return
	}
	if err := c.SendPostJobReport(ctx, rep); err != nil {
		logging.Warn("post-job report: send failed", "err", err)
		return
	}
	updates, security := rep.Counts()
	logging.Info("post-job report sent",
		"updates", updates, "security", security, "reboot_required", rep.RebootRequired)
}
