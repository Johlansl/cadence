// Package config loads the agent configuration from environment variables.
// No config file and no flags: the agent is a single static binary, configured
// by the environment (and, in production, by its systemd unit).
package config

import (
	"fmt"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"
)

type Config struct {
	ServerURL      string        // CADENCE_SERVER_URL, trailing slash stripped
	Token          string        // CADENCE_TOKEN
	RunAptUpdate   bool          // CADENCE_RUN_APT_UPDATE (default false)
	EnableUpgrades bool          // CADENCE_ENABLE_UPGRADES (default true)
	EnableReboot   bool          // CADENCE_ENABLE_REBOOT (default true); kill-switch, wins over policy
	HTTPTimeout    time.Duration // CADENCE_HTTP_TIMEOUT_SECONDS (default 30s)
}

// Load reads and validates the configuration.
func Load() (Config, error) {
	var cfg Config
	var missing []string

	cfg.ServerURL = strings.TrimRight(strings.TrimSpace(os.Getenv("CADENCE_SERVER_URL")), "/")
	if cfg.ServerURL == "" {
		missing = append(missing, "CADENCE_SERVER_URL")
	} else if u, err := url.Parse(cfg.ServerURL); err != nil ||
		(u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return Config{}, fmt.Errorf("CADENCE_SERVER_URL is not a valid http(s) URL: %q", cfg.ServerURL)
	}

	cfg.Token = strings.TrimSpace(os.Getenv("CADENCE_TOKEN"))
	if cfg.Token == "" {
		missing = append(missing, "CADENCE_TOKEN")
	}

	if len(missing) > 0 {
		return Config{}, fmt.Errorf("missing required environment variable(s): %s",
			strings.Join(missing, ", "))
	}

	if v := strings.TrimSpace(os.Getenv("CADENCE_RUN_APT_UPDATE")); v != "" {
		b, err := strconv.ParseBool(v)
		if err != nil {
			return Config{}, fmt.Errorf("CADENCE_RUN_APT_UPDATE must be a boolean, got %q", v)
		}
		cfg.RunAptUpdate = b
	}

	cfg.EnableUpgrades = true
	if v := strings.TrimSpace(os.Getenv("CADENCE_ENABLE_UPGRADES")); v != "" {
		b, err := strconv.ParseBool(v)
		if err != nil {
			return Config{}, fmt.Errorf("CADENCE_ENABLE_UPGRADES must be a boolean, got %q", v)
		}
		cfg.EnableUpgrades = b
	}

	cfg.EnableReboot = true
	if v := strings.TrimSpace(os.Getenv("CADENCE_ENABLE_REBOOT")); v != "" {
		b, err := strconv.ParseBool(v)
		if err != nil {
			return Config{}, fmt.Errorf("CADENCE_ENABLE_REBOOT must be a boolean, got %q", v)
		}
		cfg.EnableReboot = b
	}

	cfg.HTTPTimeout = 30 * time.Second
	if v := strings.TrimSpace(os.Getenv("CADENCE_HTTP_TIMEOUT_SECONDS")); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n <= 0 {
			return Config{}, fmt.Errorf("CADENCE_HTTP_TIMEOUT_SECONDS must be a positive integer, got %q", v)
		}
		cfg.HTTPTimeout = time.Duration(n) * time.Second
	}

	return cfg, nil
}
