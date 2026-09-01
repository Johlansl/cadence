package rebootcheck

import "testing"

func TestFlavour(t *testing.T) {
	cases := map[string]string{
		"6.1.0-52-amd64":       "-amd64",
		"6.1.0-52-cloud-amd64": "-amd64",
		"6.12.9-1-rt-amd64":    "-amd64",
		"noflavour":            "",
		"":                     "",
	}
	for in, want := range cases {
		if got := flavour(in); got != want {
			t.Errorf("flavour(%q) = %q, want %q", in, got, want)
		}
	}
}
