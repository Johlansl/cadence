package config

import (
	"testing"
	"time"
)

func clearEnv(t *testing.T) {
	t.Helper()
	for _, k := range []string{
		"CADENCE_SERVER_URL", "CADENCE_TOKEN", "CADENCE_RUN_APT_UPDATE",
		"CADENCE_ENABLE_UPGRADES", "CADENCE_ENABLE_REBOOT", "CADENCE_HTTP_TIMEOUT_SECONDS",
	} {
		t.Setenv(k, "")
	}
}

func TestLoadDefaults(t *testing.T) {
	clearEnv(t)
	t.Setenv("CADENCE_SERVER_URL", "https://cadence.lan/")
	t.Setenv("CADENCE_TOKEN", "  tok  ")

	cfg, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.ServerURL != "https://cadence.lan" {
		t.Errorf("ServerURL = %q (trailing slash / spaces not trimmed)", cfg.ServerURL)
	}
	if cfg.Token != "tok" {
		t.Errorf("Token = %q", cfg.Token)
	}
	if cfg.RunAptUpdate {
		t.Error("RunAptUpdate should default to false")
	}
	if !cfg.EnableUpgrades || !cfg.EnableReboot {
		t.Error("EnableUpgrades / EnableReboot should default to true")
	}
	if cfg.HTTPTimeout != 30*time.Second {
		t.Errorf("HTTPTimeout = %v", cfg.HTTPTimeout)
	}
}

func TestLoadMissingRequired(t *testing.T) {
	clearEnv(t)
	if _, err := Load(); err == nil {
		t.Fatal("expected an error when SERVER_URL and TOKEN are unset")
	}
}

func TestLoadRejectsBadValues(t *testing.T) {
	cases := map[string]map[string]string{
		"bad url":     {"CADENCE_SERVER_URL": "ftp://x", "CADENCE_TOKEN": "t"},
		"no host":     {"CADENCE_SERVER_URL": "https://", "CADENCE_TOKEN": "t"},
		"bad bool":    {"CADENCE_SERVER_URL": "http://x", "CADENCE_TOKEN": "t", "CADENCE_RUN_APT_UPDATE": "yes-please"},
		"bad timeout": {"CADENCE_SERVER_URL": "http://x", "CADENCE_TOKEN": "t", "CADENCE_HTTP_TIMEOUT_SECONDS": "-1"},
	}
	for name, env := range cases {
		t.Run(name, func(t *testing.T) {
			clearEnv(t)
			for k, v := range env {
				t.Setenv(k, v)
			}
			if _, err := Load(); err == nil {
				t.Fatalf("%s: expected an error", name)
			}
		})
	}
}

func TestLoadParsesOptionals(t *testing.T) {
	clearEnv(t)
	t.Setenv("CADENCE_SERVER_URL", "http://x")
	t.Setenv("CADENCE_TOKEN", "t")
	t.Setenv("CADENCE_RUN_APT_UPDATE", "true")
	t.Setenv("CADENCE_ENABLE_UPGRADES", "false")
	t.Setenv("CADENCE_ENABLE_REBOOT", "0")
	t.Setenv("CADENCE_HTTP_TIMEOUT_SECONDS", "12")

	cfg, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if !cfg.RunAptUpdate || cfg.EnableUpgrades || cfg.EnableReboot {
		t.Errorf("optional bools not parsed: %+v", cfg)
	}
	if cfg.HTTPTimeout != 12*time.Second {
		t.Errorf("HTTPTimeout = %v", cfg.HTTPTimeout)
	}
}
