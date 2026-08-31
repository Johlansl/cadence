package main

import (
	"strings"
	"testing"
)

func TestRebootDecision(t *testing.T) {
	tests := []struct {
		name           string
		status         string
		rebootRequired bool
		enableReboot   bool
		mode           string
		wantReboot     bool
		wantLog        string // substring; "" means the suffix must be empty
	}{
		{"job failed", "failed", true, true, "auto", false, ""},
		{"no reboot required", "succeeded", false, true, "auto", false, ""},
		{"auto + enabled -> reboot", "succeeded", true, true, "auto", true, "-> rebooting"},
		{"auto + kill-switch off", "succeeded", true, false, "auto", false, "CADENCE_ENABLE_REBOOT=false"},
		{"mode never", "succeeded", true, true, "never", false, `mode is "never"`},
		{"mode prompt", "succeeded", true, true, "prompt", false, `mode is "prompt"`},
		{"empty mode treated as never", "succeeded", true, true, "", false, `mode is "never"`},
		{"failed job with reboot mode auto and kill-switch off", "failed", true, false, "auto", false, ""},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got, suffix := rebootDecision(tc.status, tc.rebootRequired, tc.enableReboot, tc.mode)
			if got != tc.wantReboot {
				t.Errorf("willReboot = %v, want %v", got, tc.wantReboot)
			}
			if tc.wantLog == "" {
				if suffix != "" {
					t.Errorf("log suffix = %q, want empty", suffix)
				}
				return
			}
			if !strings.Contains(suffix, tc.wantLog) {
				t.Errorf("log suffix %q does not contain %q", suffix, tc.wantLog)
			}
		})
	}
}
