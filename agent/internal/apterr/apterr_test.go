package apterr

import (
	"strings"
	"testing"
)

func TestIsLockHeld(t *testing.T) {
	held := []string{
		"E: Could not get lock /var/lib/dpkg/lock-frontend",
		"E: Unable to acquire the dpkg frontend lock (/var/lib/dpkg/lock-frontend), is another process using it?",
		"Waiting for cache lock: Resource temporarily unavailable",
		"could not get lock /var/lib/apt/lists/lock",
	}
	for _, s := range held {
		if !IsLockHeld(s) {
			t.Errorf("IsLockHeld(%q) = false, want true", s)
		}
	}
	notHeld := []string{
		"E: Sub-process /usr/bin/dpkg returned an error code (1)",
		"E: Unable to locate package foo",
		"",
	}
	for _, s := range notHeld {
		if IsLockHeld(s) {
			t.Errorf("IsLockHeld(%q) = true, want false", s)
		}
	}
}

func TestIsInterrupted(t *testing.T) {
	yes := []string{
		"E: dpkg was interrupted, you must manually run 'dpkg --configure -a' to correct the problem.",
		"run 'dpkg --configure -a'",
	}
	for _, s := range yes {
		if !IsInterrupted(s) {
			t.Errorf("IsInterrupted(%q) = false, want true", s)
		}
	}
	no := []string{"E: Could not get lock", "some unrelated error", ""}
	for _, s := range no {
		if IsInterrupted(s) {
			t.Errorf("IsInterrupted(%q) = true, want false", s)
		}
	}
}

func TestIsDiskFull(t *testing.T) {
	yes := []string{
		"dpkg: unrecoverable fatal error, aborting:\n failed to write to '/var/lib/dpkg/updates/tmp.i': No space left on device",
		"E: You don't have enough free space in /var/cache/apt/archives/.",
		"gzip: write error: No space left on device",
	}
	for _, s := range yes {
		if !IsDiskFull(s) {
			t.Errorf("IsDiskFull(%q) = false, want true", s)
		}
	}
	no := []string{
		"E: Sub-process /usr/bin/dpkg returned an error code (1)",
		"Fetched 12.3 MB in 2s (6000 kB/s)",
		"",
	}
	for _, s := range no {
		if IsDiskFull(s) {
			t.Errorf("IsDiskFull(%q) = true, want false", s)
		}
	}
}

func TestIsNetworkOrRepo(t *testing.T) {
	yes := []string{
		"E: Failed to fetch http://deb.debian.org/debian/pool/main/x/xz-utils/xz_5.6.tar.xz  404  Not Found [IP: 1.2.3.4 80]",
		"Err:1 http://deb.debian.org/debian bookworm InRelease\n  Temporary failure resolving 'deb.debian.org'",
		"E: Unable to fetch some archives, maybe run apt-get update or try with --fix-missing?",
		"W: Failed to fetch ... Hash Sum mismatch",
		"E: Release file for http://deb.debian.org/debian/dists/bookworm/InRelease is not valid yet (invalid for another 8h 5min 12s).",
	}
	for _, s := range yes {
		if !IsNetworkOrRepo(s) {
			t.Errorf("IsNetworkOrRepo(%q) = false, want true", s)
		}
	}
	no := []string{
		"E: Sub-process /usr/bin/dpkg returned an error code (1)",
		"Reading package lists...",
		"",
	}
	for _, s := range no {
		if IsNetworkOrRepo(s) {
			t.Errorf("IsNetworkOrRepo(%q) = true, want false", s)
		}
	}
}

func TestIsDpkgError(t *testing.T) {
	yes := []string{
		"E: Sub-process /usr/bin/dpkg returned an error code (1)",
		"dpkg: error processing package libfoo:amd64 (--configure):\n installed libfoo:amd64 package post-installation script subprocess returned error exit status 1",
		"The following packages have unmet dependencies:\n foo : Depends: bar (>= 2.0) but 1.0 is to be installed",
		"E: Unmet dependencies. Try 'apt --fix-broken install' with no packages (or specify a solution).",
		"dpkg: error processing archive /var/cache/apt/archives/x.deb (--unpack):\n trying to overwrite '/usr/bin/x', which is also in package y 1.0",
	}
	for _, s := range yes {
		if !IsDpkgError(s) {
			t.Errorf("IsDpkgError(%q) = false, want true", s)
		}
	}
	no := []string{
		"E: Could not get lock /var/lib/dpkg/lock-frontend",
		"E: Failed to fetch http://... 404 Not Found",
		"Fetched 10 MB in 1s",
		"",
	}
	for _, s := range no {
		if IsDpkgError(s) {
			t.Errorf("IsDpkgError(%q) = true, want false", s)
		}
	}
}

func TestClassify(t *testing.T) {
	cases := []struct {
		name             string
		output           string
		deadlineExceeded bool
		want             string
	}{
		{
			name:             "deadline outranks the text",
			output:           "dpkg: error processing package libfoo (--configure)",
			deadlineExceeded: true,
			want:             CategoryTimeout,
		},
		{
			name:   "disk full outranks a cascaded dpkg error",
			output: "dpkg: error processing package libfoo\ndpkg: unrecoverable fatal error, aborting:\n No space left on device",
			want:   CategoryDiskFull,
		},
		{
			name:   "held lock on the final attempt",
			output: "E: Could not get lock /var/lib/dpkg/lock-frontend. It is held by process 4242 (unattended-upgr)",
			want:   CategoryAptLocked,
		},
		{
			name:   "mirror unreachable during the download phase",
			output: "E: Failed to fetch http://deb.debian.org/debian/... 404  Not Found\nE: Unable to fetch some archives, maybe run apt-get update",
			want:   CategoryNetworkOrRepo,
		},
		{
			name:   "package failure",
			output: "Errors were encountered while processing:\n libfoo:amd64\nE: Sub-process /usr/bin/dpkg returned an error code (1)",
			want:   CategoryDpkgError,
		},
		{
			name:   "interrupted dpkg state",
			output: "E: dpkg was interrupted, you must manually run 'dpkg --configure -a'",
			want:   CategoryDpkgError,
		},
		{
			name:   "nothing recognisable",
			output: "Reading package lists...\nBuilding dependency tree...\nSomething odd happened and the run stopped.",
			want:   CategoryUnknown,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, summary := Classify(tc.output, tc.deadlineExceeded)
			if got != tc.want {
				t.Errorf("Classify() category = %q, want %q", got, tc.want)
			}
			if summary == "" {
				t.Errorf("Classify() summary is empty, want a non-empty line")
			}
		})
	}
}

func TestSummary(t *testing.T) {
	cases := []struct {
		name string
		in   string
		want string
	}{
		{
			name: "prefers the last E: line",
			in:   "Reading package lists...\nE: first error\nsome noise\nE: Sub-process /usr/bin/dpkg returned an error code (1)\n",
			want: "E: Sub-process /usr/bin/dpkg returned an error code (1)",
		},
		{
			name: "falls back to a dpkg: error line",
			in:   "unpacking...\ndpkg: error processing package libfoo (--configure)\nmore text\n",
			want: "dpkg: error processing package libfoo (--configure)",
		},
		{
			name: "falls back to the last non-empty line",
			in:   "step one\nstep two\n\n   \n",
			want: "step two",
		},
		{
			name: "collapses internal whitespace",
			in:   "E:    Failed  to\tfetch   http://x",
			want: "E: Failed to fetch http://x",
		},
		{
			name: "empty input",
			in:   "   \n\n",
			want: "",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := Summary(tc.in); got != tc.want {
				t.Errorf("Summary() = %q, want %q", got, tc.want)
			}
		})
	}
}

func TestSummaryCapsLength(t *testing.T) {
	long := "E: " + strings.Repeat("x", 300)
	if got := Summary(long); len(got) > summaryMaxBytes {
		t.Errorf("Summary() length = %d, want <= %d", len(got), summaryMaxBytes)
	}
}
