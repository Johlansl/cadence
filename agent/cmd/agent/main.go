// Command agent collects the local package / OS state, posts a single report to
// the Cadence server, and -- if the server hands back a pending job -- runs it
// and reports the result, then exits. Scheduling is external (a systemd timer).
package main

import (
	"context"
	"fmt"
	"log"
	"os"
	"time"

	"cadence/agent/internal/client"
	"cadence/agent/internal/collector"
	"cadence/agent/internal/config"
	"cadence/agent/internal/executor"
	"cadence/agent/internal/report"
)

// agentVersion is sent to the server and bumped by hand per release.
const agentVersion = "0.2.0"

func main() {
	log.SetFlags(0)
	log.SetPrefix("cadence-agent: ")

	if err := run(); err != nil {
		log.Printf("error: %v", err)
		os.Exit(1)
	}
}

func run() error {
	cfg, err := config.Load()
	if err != nil {
		return err
	}

	c := client.New(cfg.ServerURL, cfg.Token, cfg.HTTPTimeout)

	// Safety ceiling for collection + report.
	reportCtx, cancel := context.WithTimeout(context.Background(), 10*time.Minute)
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
	log.Printf("report sent to %s: host=%s packages=%d updates=%d security=%d reboot_required=%v",
		cfg.ServerURL, rep.Hostname, len(rep.Packages), updates, security, rep.RebootRequired)

	if job == nil {
		return nil
	}
	return runJob(cfg, c, job)
}

func runJob(cfg config.Config, c *client.Client, job *report.JobHandoff) error {
	log.Printf("job received: id=%s type=%s", job.ID, job.JobType)

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

	if !cfg.EnableUpgrades {
		log.Printf("upgrades are disabled on this host; reporting job as failed")
		return submit("failed", 0,
			"upgrades are disabled on this host (CADENCE_ENABLE_UPGRADES=false)", false)
	}
	if job.JobType != "apt_upgrade" {
		return submit("failed", 0, "unsupported job type: "+job.JobType, false)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Minute)
	defer cancel()

	log.Printf("running apt-get dist-upgrade ...")
	res := executor.RunAptUpgrade(ctx)

	status := "succeeded"
	if res.ExitCode != 0 || res.Err != nil {
		status = "failed"
	}
	log.Printf("job %s %s: apt exit=%d reboot_required=%v", job.ID, status, res.ExitCode, res.RebootRequired)

	if err := submit(status, res.ExitCode, res.Log, res.RebootRequired); err != nil {
		return fmt.Errorf("submitting job result: %w", err)
	}
	if status == "failed" {
		return fmt.Errorf("job %s failed (apt exit %d)", job.ID, res.ExitCode)
	}
	return nil
}
