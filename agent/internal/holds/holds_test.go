package holds

import (
	"reflect"
	"testing"
)

func TestValid(t *testing.T) {
	valid := []string{"docker-ce", "linux-image-6.1.0-amd64", "postgresql-14", "nvidia-driver-535", "g++"}
	for _, s := range valid {
		if !Valid(s) {
			t.Errorf("Valid(%q) = false, want true", s)
		}
	}
	invalid := []string{
		"", "a", "-docker", "Docker-CE", "rm -rf /", "$(id)", "docker;ls", "docker`id`",
	}
	for _, s := range invalid {
		if Valid(s) {
			t.Errorf("Valid(%q) = true, want false", s)
		}
	}
}

func TestFilter(t *testing.T) {
	valid, rejected := Filter([]string{"docker-ce", "rm -rf /", "postgresql-14", ""})
	if !reflect.DeepEqual(valid, []string{"docker-ce", "postgresql-14"}) {
		t.Errorf("valid = %v", valid)
	}
	if !reflect.DeepEqual(rejected, []string{"rm -rf /", ""}) {
		t.Errorf("rejected = %v", rejected)
	}
}

func TestParseShowHold(t *testing.T) {
	got := ParseShowHold("docker-ce\npostgresql-14\n\n")
	want := []string{"docker-ce", "postgresql-14"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("ParseShowHold() = %v, want %v", got, want)
	}
	if got := ParseShowHold(""); got != nil {
		t.Errorf("ParseShowHold(\"\") = %v, want nil", got)
	}
	if got := ParseShowHold("  \n  \n"); got != nil {
		t.Errorf("ParseShowHold(blank) = %v, want nil", got)
	}
}

func TestDiff(t *testing.T) {
	cases := []struct {
		name                 string
		current, want        []string
		wantHold, wantUnhold []string
	}{
		{
			name: "nothing held, nothing wanted",
		},
		{
			name:     "new policy, nothing held yet",
			want:     []string{"docker-ce", "postgresql-14"},
			wantHold: []string{"docker-ce", "postgresql-14"},
		},
		{
			name:       "policy removed, everything currently held",
			current:    []string{"docker-ce", "postgresql-14"},
			wantUnhold: []string{"docker-ce", "postgresql-14"},
		},
		{
			name:       "one added, one removed, one unchanged",
			current:    []string{"docker-ce", "postgresql-14"},
			want:       []string{"docker-ce", "nvidia-driver-535"},
			wantHold:   []string{"nvidia-driver-535"},
			wantUnhold: []string{"postgresql-14"},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			toHold, toUnhold := Diff(tc.current, tc.want)
			if !reflect.DeepEqual(toHold, tc.wantHold) {
				t.Errorf("toHold = %v, want %v", toHold, tc.wantHold)
			}
			if !reflect.DeepEqual(toUnhold, tc.wantUnhold) {
				t.Errorf("toUnhold = %v, want %v", toUnhold, tc.wantUnhold)
			}
		})
	}
}

func TestCorrelateKeptBack(t *testing.T) {
	// A real apt-get dist-upgrade "kept back" block.
	output := `Reading package lists...
Building dependency tree...
Calculating upgrade...
The following packages have been kept back:
  docker-ce nvidia-driver-535
0 upgraded, 0 newly installed, 0 to remove and 2 not upgraded.
`
	got := Correlate(output, []string{"docker-ce", "postgresql-14"})
	want := []string{"docker-ce"} // postgresql-14 was held but never mentioned
	if !reflect.DeepEqual(got, want) {
		t.Errorf("Correlate() = %v, want %v", got, want)
	}
}

func TestCorrelateDpkgErrorMentioningAHeldPackage(t *testing.T) {
	output := "The following packages have unmet dependencies:\n" +
		" libfoo : Depends: postgresql-14 (>= 14.10) but 14.9 is to be installed\n" +
		"E: Unable to correct problems, you have held broken packages."
	got := Correlate(output, []string{"postgresql-14", "docker-ce"})
	want := []string{"postgresql-14"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("Correlate() = %v, want %v", got, want)
	}
}

func TestCorrelateNoEvidenceMeansNil(t *testing.T) {
	output := "Reading package lists...\n0 upgraded, 1 newly installed, 0 to remove.\n"
	if got := Correlate(output, []string{"docker-ce"}); got != nil {
		t.Errorf("Correlate() = %v, want nil", got)
	}
}

func TestCorrelateKeptBackButHeldNameNotMentionedMeansNil(t *testing.T) {
	// Something was kept back, but it has nothing to do with what we held --
	// must not blame an unrelated held package.
	output := "The following packages have been kept back:\n  some-other-package\n"
	if got := Correlate(output, []string{"docker-ce"}); got != nil {
		t.Errorf("Correlate() = %v, want nil", got)
	}
}

func TestCorrelateNoHeldNamesMeansNil(t *testing.T) {
	output := "The following packages have been kept back:\n  docker-ce\n"
	if got := Correlate(output, nil); got != nil {
		t.Errorf("Correlate() = %v, want nil", got)
	}
}

func TestMentionsRespectsWordBoundary(t *testing.T) {
	if mentions("installing libcurl4 now", "curl") {
		t.Error("mentions() matched \"curl\" inside \"libcurl4\", want a word-boundary match only")
	}
	if !mentions("installing curl now", "curl") {
		t.Error("mentions() did not match a standalone \"curl\"")
	}
}
