// Command agent has two one-shot modes, both driven by systemd timers:
//
//	cadence-agent          collect package/OS state, POST a report, and run a
//	                       job if one is piggybacked on the response
//	cadence-agent -poll    just ask the server for a pending job and run it
//	                       (fast path, no collection)
//
// Communication stays outbound-only; -poll is a short poll, not a long poll.
package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"os"
	"time"

	"cadence/agent/internal/apterr"
	"cadence/agent/internal/client"
	"cadence/agent/internal/collector"
	"cadence/agent/internal/config"
	"cadence/agent/internal/executor"
	"cadence/agent/internal/logging"
	"cadence/agent/internal/reboot"
	"cadence/agent/internal/report"
)

// agentVersion is the fallback version for dev / untagged builds. Release
// builds override it with the git tag via
// -ldflags "-X main.agentVersion=<version>" (see scripts/publish-agent.sh).
// Keep this literal in step with the newest agent/CHANGELOG.md heading.
var agentVersion = "0.10.0"

// Run-phase timeouts. Each systemd unit's TimeoutStartSec MUST comfortably
// exceed the sum of the timeouts on its path, or systemd SIGKILLs the whole
// cgroup mid apt-get / dpkg:
//
//	cadence-agent.service       reportTimeout + jobTimeout + postJobReportTimeout
//	cadence-agent-poll.service  jobTimeout + postJobReportTimeout
const (
	reportTimeout        = 10 * time.Minute // collect + POST /reports
	jobTimeout           = 30 * time.Minute // apt-get dist-upgrade for a job
	postJobReportTimeout = 5 * time.Minute  // the fresh report sent after a job
)

func main() {
	log.SetFlags(0)
	log.SetPrefix("cadence-agent: ")

	pollOnly := flag.Bool("poll", false,
		"check for a pending job and run it, without collecting or reporting packages")
	showVersion := flag.Bool("version", false, "print the agent version and exit")
	flag.Parse()

	if *showVersion {
		fmt.Println(agentVersion)
		return
	}

	if err := run(*pollOnly); err != nil {
		logging.Error("agent run failed", "err", err)
		os.Exit(1)
	}
}

func run(pollOnly bool) error {
	cfg, err := config.Load()
	if err != nil {
		return err
	}
	c := client.New(cfg.ServerURL, cfg.Token, cfg.HTTPTimeout)

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

	// failCat / failSummary are sent only for a failed job, "" on success.
	// heldConflicts is nil except on the apt_upgrade path.
	submit := func(status string, exitCode int, logText string, reboot bool,
		failCat, failSummary string, heldConflicts []string) error {
		ctx, cancel := context.WithTimeout(context.Background(), cfg.HTTPTimeout)
		defer cancel()
		return c.SubmitJobResult(ctx, job.ID, client.JobResult{
			Status:          status,
			ExitCode:        exitCode,
			Log:             logText,
			RebootRequired:  reboot,
			FailureCategory: failCat,
			FailureSummary:  failSummary,
			HeldConflicts:   heldConflicts,
		})
	}
	// refused reports a job the agent declined before running anything: the
	// category is fixed, the reason is the message itself.
	refused := func(msg string) error {
		return submit("failed", 0, msg, false, apterr.CategoryAgentRefused, msg, nil)
	}

	// Dedicated reboot job (reboot_policy "prompt" / a "reboot now" from the
	// dashboard): no upgrade, just reboot. Report success before issuing it so
	// the job never hangs in "running".
	if job.JobType == "reboot" {
		if !cfg.EnableReboot {
			return refused("reboot is disabled on this host (CADENCE_ENABLE_REBOOT=false)")
		}
		logging.Info("dedicated reboot job -> systemctl --no-block reboot", "job_id", job.ID)
		if err := submit("succeeded", 0,
			"[cadence] reboot requested via dedicated job -> systemctl --no-block reboot\n",
			true, "", "", nil); err != nil {
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

	if !cfg.EnableUpgrades {
		logging.Warn("upgrades disabled on this host, reporting job as failed", "job_id", job.ID)
		return refused("upgrades are disabled on this host (CADENCE_ENABLE_UPGRADES=false)")
	}
	if job.JobType != "apt_upgrade" {
		return refused("unsupported job type: " + job.JobType)
	}

	ctx, cancel := context.WithTimeout(context.Background(), jobTimeout)
	defer cancel()

	logging.Info("running apt-get dist-upgrade", "job_id", job.ID)
	res := executor.RunAptUpgrade(ctx, job.ExcludedPackages())

	status := "succeeded"
	if res.ExitCode != 0 || res.Err != nil {
		status = "failed"
	}
	logging.Info("apt-get dist-upgrade finished",
		"job_id", job.ID, "status", status, "exit_code", res.ExitCode,
		"reboot_required", res.RebootRequired)

	// Decide about the reboot. The server already resolved the effective mode
	// (per-job override, else host reboot_policy) into params.reboot.
	willReboot, logSuffix := rebootDecision(status, res.RebootRequired, cfg.EnableReboot, job.RebootMode())
	logText := res.Log + logSuffix

	if err := submit(status, res.ExitCode, logText, res.RebootRequired,
		res.FailureCategory, res.FailureSummary, res.HeldConflicts); err != nil {
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
	if _, err := c.SendReport(ctx, rep); err != nil {
		logging.Warn("post-job report: send failed", "err", err)
		return
	}
	updates, security := rep.Counts()
	logging.Info("post-job report sent",
		"updates", updates, "security", security, "reboot_required", rep.RebootRequired)
}
