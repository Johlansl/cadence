package report

import (
	"encoding/json"
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
