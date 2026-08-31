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

	"cadence/agent/internal/client"
	"cadence/agent/internal/collector"
	"cadence/agent/internal/config"
	"cadence/agent/internal/executor"
	"cadence/agent/internal/logging"
	"cadence/agent/internal/reboot"
	"cadence/agent/internal/report"
)

// agentVersion is sent to the server and bumped by hand per release.
const agentVersion = "0.6.0"

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

	submit := func(status string, exitCode int, logText string, reboot bool) error {
		ctx, cancel := context.WithTimeout(context.Background(), cfg.HTTPTimeout)
		defer cancel()
		return c.SubmitJobResult(ctx, job.ID, client.JobResult{
			Status:         status,
			ExitCode:       exitCode,
			Log:            logText,
			RebootRequired: reboot,
		})
	}

	// Dedicated reboot job (reboot_policy "prompt" / a "reboot now" from the
	// dashboard): no upgrade, just reboot. Report success before issuing it so
	// the job never hangs in "running".
	if job.JobType == "reboot" {
		if !cfg.EnableReboot {
			return submit("failed", 0,
				"reboot is disabled on this host (CADENCE_ENABLE_REBOOT=false)", false)
		}
		logging.Info("dedicated reboot job -> systemctl --no-block reboot", "job_id", job.ID)
		if err := submit("succeeded", 0,
			"[cadence] reboot requested via dedicated job -> systemctl --no-block reboot\n", true); err != nil {
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
		return submit("failed", 0,
			"upgrades are disabled on this host (CADENCE_ENABLE_UPGRADES=false)", false)
	}
	if job.JobType != "apt_upgrade" {
		return submit("failed", 0, "unsupported job type: "+job.JobType, false)
	}

	ctx, cancel := context.WithTimeout(context.Background(), jobTimeout)
	defer cancel()

	logging.Info("running apt-get dist-upgrade", "job_id", job.ID)
	res := executor.RunAptUpgrade(ctx)

	status := "succeeded"
	if res.ExitCode != 0 || res.Err != nil {
		status = "failed"
	}
	logging.Info("apt-get dist-upgrade finished",
		"job_id", job.ID, "status", status, "exit_code", res.ExitCode,
		"reboot_required", res.RebootRequired)

	// Decide about the reboot. The server already resolved the effective mode
	// (per-job override, else host reboot_policy) into params.reboot.
	mode := job.RebootMode()
	if mode == "" {
		mode = "never"
	}
	willReboot := status == "succeeded" && res.RebootRequired && mode == "auto" && cfg.EnableReboot

	logText := res.Log
	if status == "succeeded" && res.RebootRequired {
		switch {
		case mode != "auto":
			logText += fmt.Sprintf("\n[cadence] reboot required; reboot mode is %q -> not rebooting\n", mode)
		case !cfg.EnableReboot:
			logText += "\n[cadence] reboot required and reboot mode is \"auto\", but CADENCE_ENABLE_REBOOT=false -> not rebooting\n"
		default:
			logText += "\n[cadence] reboot required and reboot mode is \"auto\" -> rebooting via systemctl --no-block reboot\n"
		}
	}

	if err := submit(status, res.ExitCode, logText, res.RebootRequired); err != nil {
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
