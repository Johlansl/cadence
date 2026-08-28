package collector

import (
	"testing"

	"cadence/agent/internal/report"
)

func TestParseInstLine(t *testing.T) {
	cases := []struct {
		name string
		line string
		want pendingUpdate
		ok   bool
	}{
		{
			name: "fresh install, no old version, single origin",
			line: "Inst cowsay (3.03+dfsg2-8 Debian:13.6/stable [all])",
			want: pendingUpdate{
				name: "cowsay", arch: "all", candidate: "3.03+dfsg2-8",
				origin: "Debian:13.6/stable", isSecurity: false,
			},
			ok: true,
		},
		{
			name: "upgrade with old version and multiple security origins",
			line: "Inst libssl3 [3.0.11-1~deb12u2] (3.0.14-1~deb12u2 Debian:12/stable-security, Debian-Security:12/stable-security [amd64])",
			want: pendingUpdate{
				name: "libssl3", arch: "amd64", candidate: "3.0.14-1~deb12u2",
				origin:     "Debian:12/stable-security, Debian-Security:12/stable-security",
				isSecurity: true,
			},
			ok: true,
		},
		{
			name: "plain stable upgrade",
			line: "Inst base-files [12.4+deb12u5] (12.4+deb12u7 Debian:12/stable [amd64])",
			want: pendingUpdate{
				name: "base-files", arch: "amd64", candidate: "12.4+deb12u7",
				origin: "Debian:12/stable", isSecurity: false,
			},
			ok: true,
		},
		{
			name: "stable-updates, no old version bracket",
			line: "Inst tzdata (2024b-0+deb12u1 Debian:12/stable-updates [all])",
			want: pendingUpdate{
				name: "tzdata", arch: "all", candidate: "2024b-0+deb12u1",
				origin: "Debian:12/stable-updates", isSecurity: false,
			},
			ok: true,
		},
		{
			name: "security kernel upgrade",
			line: "Inst linux-image-amd64 [6.1.0-10] (6.1.0-11 Debian-Security:12/stable-security [amd64])",
			want: pendingUpdate{
				name: "linux-image-amd64", arch: "amd64", candidate: "6.1.0-11",
				origin: "Debian-Security:12/stable-security", isSecurity: true,
			},
			ok: true,
		},
		{
			name: "Conf line is ignored",
			line: "Conf cowsay (3.03+dfsg2-8 Debian:13.6/stable [all])",
			ok:   false,
		},
		{
			name: "Remv line is ignored",
			line: "Remv oldpkg [1.0-1]",
			ok:   false,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, ok := parseInstLine(tc.line)
			if ok != tc.ok {
				t.Fatalf("ok = %v, want %v", ok, tc.ok)
			}
			if !ok {
				return
			}
			if got != tc.want {
				t.Errorf("parseInstLine(%q)\n got  %+v\n want %+v", tc.line, got, tc.want)
			}
		})
	}
}

func TestMergeUpdatesIgnoresNonInstalled(t *testing.T) {
	installed := map[string]report.Package{
		pkgKey("openssl", "amd64"): {Name: "openssl", Architecture: "amd64", InstalledVersion: "3.0.11-1"},
		pkgKey("bash", "amd64"):    {Name: "bash", Architecture: "amd64", InstalledVersion: "5.2-2"},
	}
	updates := map[string]pendingUpdate{
		pkgKey("openssl", "amd64"): {name: "openssl", arch: "amd64", candidate: "3.0.14-1", origin: "Debian-Security:12/stable-security", isSecurity: true},
		// New dependency, not installed -> must be ignored.
		pkgKey("libnew", "amd64"): {name: "libnew", arch: "amd64", candidate: "1.0-1", origin: "Debian:12/stable"},
	}

	got := mergeUpdates(installed, updates)
	if len(got) != 2 {
		t.Fatalf("got %d packages, want 2 (%+v)", len(got), got)
	}
	if got[0].Name != "bash" || got[1].Name != "openssl" {
		t.Fatalf("not sorted by name: %+v", got)
	}
	if got[0].CandidateVersion != nil {
		t.Errorf("bash should have no candidate, got %v", *got[0].CandidateVersion)
	}
	if got[1].CandidateVersion == nil || *got[1].CandidateVersion != "3.0.14-1" || !got[1].IsSecurityUpdate {
		t.Errorf("openssl update not applied: %+v", got[1])
	}
}
