// Command agent collects the local package / OS state and posts a single report
// to the Cadence server, then exits. Scheduling is external (a systemd timer).
package main

import (
	"context"
	"log"
	"os"
	"time"

	"cadence/agent/internal/client"
	"cadence/agent/internal/collector"
	"cadence/agent/internal/config"
)

// agentVersion is sent to the server and bumped by hand per release.
const agentVersion = "0.1.0"

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

	// Safety ceiling for the whole run; external commands honour this context.
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Minute)
	defer cancel()

	rep, err := collector.Collect(ctx, agentVersion, cfg.RunAptUpdate)
	if err != nil {
		return err
	}

	if err := client.New(cfg.ServerURL, cfg.Token, cfg.HTTPTimeout).SendReport(ctx, rep); err != nil {
		return err
	}

	updates, security := rep.Counts()
	log.Printf("report sent to %s: host=%s packages=%d updates=%d security=%d reboot_required=%v",
		cfg.ServerURL, rep.Hostname, len(rep.Packages), updates, security, rep.RebootRequired)
	return nil
}
