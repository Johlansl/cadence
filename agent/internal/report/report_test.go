package report

import (
	"encoding/json"
	"reflect"
	"testing"
)

func TestJobHandoffRebootMode(t *testing.T) {
	cases := []struct {
		name   string
		params string
		want   string
	}{
		{"auto", `{"reboot":"auto"}`, "auto"},
		{"never", `{"reboot":"never"}`, "never"},
		{"absent", `{}`, ""},
		{"nil params", ``, ""},
		{"other keys", `{"foo":1}`, ""},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			j := JobHandoff{ID: "x", JobType: "apt_upgrade"}
			if c.params != "" {
				j.Params = json.RawMessage(c.params)
			}
			if got := j.RebootMode(); got != c.want {
				t.Fatalf("RebootMode() = %q, want %q", got, c.want)
			}
		})
	}
}

func TestJobHandoffExcludedPackages(t *testing.T) {
	cases := []struct {
		name   string
		params string
		want   []string
	}{
		{"a list", `{"excluded_packages":["docker-ce","postgresql-14"]}`, []string{"docker-ce", "postgresql-14"}},
		{"empty list", `{"excluded_packages":[]}`, nil},
		{"absent", `{}`, nil},
		{"nil params", ``, nil},
		{"other keys", `{"reboot":"auto"}`, nil},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			j := JobHandoff{ID: "x", JobType: "apt_upgrade"}
			if c.params != "" {
				j.Params = json.RawMessage(c.params)
			}
			got := j.ExcludedPackages()
			if len(got) == 0 && len(c.want) == 0 {
				return // both nil/empty, fine either way
			}
			if !reflect.DeepEqual(got, c.want) {
				t.Fatalf("ExcludedPackages() = %v, want %v", got, c.want)
			}
		})
	}
}

func TestJobHandoffKnownHeldPackages(t *testing.T) {
	cases := []struct {
		name   string
		params string
		want   []string
	}{
		{"a list", `{"known_held_packages":["docker-ce","postgresql-14"]}`, []string{"docker-ce", "postgresql-14"}},
		{"empty list", `{"known_held_packages":[]}`, nil},
		{"absent", `{}`, nil},
		{"nil params", ``, nil},
		{"other keys", `{"excluded_packages":["docker-ce"]}`, nil},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			j := JobHandoff{ID: "x", JobType: "apt_upgrade"}
			if c.params != "" {
				j.Params = json.RawMessage(c.params)
			}
			got := j.KnownHeldPackages()
			if len(got) == 0 && len(c.want) == 0 {
				return // both nil/empty, fine either way
			}
			if !reflect.DeepEqual(got, c.want) {
				t.Fatalf("KnownHeldPackages() = %v, want %v", got, c.want)
			}
		})
	}
}
