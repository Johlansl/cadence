package aptsim

import (
	"reflect"
	"testing"
)

func TestParseInst(t *testing.T) {
	cases := []struct {
		name string
		line string
		want Inst
		ok   bool
	}{
		{
			name: "fresh install, no old version, single origin",
			line: "Inst cowsay (3.03+dfsg2-8 Debian:13.6/stable [all])",
			want: Inst{
				Name: "cowsay", Arch: "all", OldVersion: "", Candidate: "3.03+dfsg2-8",
				Origin: "Debian:13.6/stable", IsSecurity: false,
			},
			ok: true,
		},
		{
			name: "upgrade with old version and multiple security origins",
			line: "Inst libssl3 [3.0.11-1~deb12u2] (3.0.14-1~deb12u2 Debian:12/stable-security, Debian-Security:12/stable-security [amd64])",
			want: Inst{
				Name: "libssl3", Arch: "amd64", OldVersion: "3.0.11-1~deb12u2",
				Candidate:  "3.0.14-1~deb12u2",
				Origin:     "Debian:12/stable-security, Debian-Security:12/stable-security",
				IsSecurity: true,
			},
			ok: true,
		},
		{
			name: "plain stable upgrade",
			line: "Inst base-files [12.4+deb12u5] (12.4+deb12u7 Debian:12/stable [amd64])",
			want: Inst{
				Name: "base-files", Arch: "amd64", OldVersion: "12.4+deb12u5",
				Candidate: "12.4+deb12u7", Origin: "Debian:12/stable", IsSecurity: false,
			},
			ok: true,
		},
		{
			name: "newly pulled dependency, no old-version bracket",
			line: "Inst libfoo1 (1.2.3-1 Debian:12/stable [amd64])",
			want: Inst{
				Name: "libfoo1", Arch: "amd64", OldVersion: "", Candidate: "1.2.3-1",
				Origin: "Debian:12/stable", IsSecurity: false,
			},
			ok: true,
		},
		{
			name: "security kernel upgrade",
			line: "Inst linux-image-amd64 [6.1.0-10] (6.1.0-11 Debian-Security:12/stable-security [amd64])",
			want: Inst{
				Name: "linux-image-amd64", Arch: "amd64", OldVersion: "6.1.0-10",
				Candidate: "6.1.0-11", Origin: "Debian-Security:12/stable-security", IsSecurity: true,
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
			got, ok := ParseInst(tc.line)
			if ok != tc.ok {
				t.Fatalf("ok = %v, want %v", ok, tc.ok)
			}
			if !ok {
				return
			}
			if got != tc.want {
				t.Errorf("ParseInst(%q)\n got  %+v\n want %+v", tc.line, got, tc.want)
			}
		})
	}
}

func TestParseRemv(t *testing.T) {
	cases := []struct {
		name string
		line string
		want Remv
		ok   bool
	}{
		{
			name: "removal with version bracket",
			line: "Remv obsolete-pkg [2.1-3]",
			want: Remv{Name: "obsolete-pkg", OldVersion: "2.1-3"},
			ok:   true,
		},
		{
			name: "removal with a trailing dependency note",
			line: "Remv libold1 [1.0-1] [libnew1:amd64 ]",
			want: Remv{Name: "libold1", OldVersion: "1.0-1"},
			ok:   true,
		},
		{
			name: "removal with no version bracket",
			line: "Remv weird-pkg",
			want: Remv{Name: "weird-pkg", OldVersion: ""},
			ok:   true,
		},
		{
			name: "Inst line is not a removal",
			line: "Inst cowsay (3.03 Debian:13/stable [all])",
			ok:   false,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, ok := ParseRemv(tc.line)
			if ok != tc.ok {
				t.Fatalf("ok = %v, want %v", ok, tc.ok)
			}
			if !ok {
				return
			}
			if got != tc.want {
				t.Errorf("ParseRemv(%q)\n got  %+v\n want %+v", tc.line, got, tc.want)
			}
		})
	}
}

func TestParseKeptBack(t *testing.T) {
	output := `Reading package lists...
Building dependency tree...
Calculating upgrade...
The following packages have been kept back:
  docker-ce docker-ce-cli
  linux-image-amd64
0 upgraded, 0 newly installed, 0 to remove and 3 not upgraded.
`
	got := ParseKeptBack(output)
	want := []string{"docker-ce", "docker-ce-cli", "linux-image-amd64"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("ParseKeptBack\n got  %v\n want %v", got, want)
	}
}

func TestParseKeptBackAbsent(t *testing.T) {
	if got := ParseKeptBack("0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n"); got != nil {
		t.Errorf("want nil, got %v", got)
	}
}

func TestParse(t *testing.T) {
	output := `Reading package lists...
Building dependency tree...
The following packages will be REMOVED:
  obsolete-lib
The following NEW packages will be installed:
  libfoo1
The following packages will be upgraded:
  base-files openssl
Inst base-files [12.4+deb12u5] (12.4+deb12u7 Debian:12/stable [amd64])
Inst libfoo1 (1.2.3-1 Debian:12/stable [amd64])
Inst openssl [3.0.11-1] (3.0.14-1 Debian-Security:12/stable-security [amd64])
Remv obsolete-lib [4.5-6]
The following packages have been kept back:
  held-pkg
Conf base-files (12.4+deb12u7 Debian:12/stable [amd64])
`
	sim := Parse(output)

	if len(sim.Inst) != 3 {
		t.Fatalf("Inst: want 3, got %d (%+v)", len(sim.Inst), sim.Inst)
	}
	if sim.Inst[0].Name != "base-files" || sim.Inst[0].OldVersion != "12.4+deb12u5" {
		t.Errorf("Inst[0] = %+v", sim.Inst[0])
	}
	if sim.Inst[1].Name != "libfoo1" || sim.Inst[1].OldVersion != "" {
		t.Errorf("Inst[1] (new dep) = %+v", sim.Inst[1])
	}
	if !sim.Inst[2].IsSecurity {
		t.Errorf("Inst[2] should be flagged security: %+v", sim.Inst[2])
	}
	if len(sim.Remv) != 1 || sim.Remv[0].Name != "obsolete-lib" || sim.Remv[0].OldVersion != "4.5-6" {
		t.Errorf("Remv = %+v", sim.Remv)
	}
	if !reflect.DeepEqual(sim.KeptBack, []string{"held-pkg"}) {
		t.Errorf("KeptBack = %v", sim.KeptBack)
	}
}
