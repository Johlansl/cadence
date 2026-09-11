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
	ClientCertFile string        // CADENCE_CLIENT_CERT_FILE (optional legacy compatibility)
	ClientKeyFile  string        // CADENCE_CLIENT_KEY_FILE
	ServerCAFile   string        // CADENCE_SERVER_CA_FILE
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

	cfg.ClientCertFile = strings.TrimSpace(os.Getenv("CADENCE_CLIENT_CERT_FILE"))
	cfg.ClientKeyFile = strings.TrimSpace(os.Getenv("CADENCE_CLIENT_KEY_FILE"))
	cfg.ServerCAFile = strings.TrimSpace(os.Getenv("CADENCE_SERVER_CA_FILE"))
	tlsValues := []string{cfg.ClientCertFile, cfg.ClientKeyFile, cfg.ServerCAFile}
	tlsSet := 0
	for _, value := range tlsValues {
		if value != "" {
			tlsSet++
		}
	}
	if tlsSet != 0 && tlsSet != len(tlsValues) {
		return Config{}, fmt.Errorf("CADENCE_CLIENT_CERT_FILE, CADENCE_CLIENT_KEY_FILE and CADENCE_SERVER_CA_FILE must be set together")
	}
	if tlsSet > 0 {
		u, _ := url.Parse(cfg.ServerURL)
		if u.Scheme != "https" {
			return Config{}, fmt.Errorf("CADENCE_SERVER_URL must use https when client TLS credentials are configured")
		}
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
