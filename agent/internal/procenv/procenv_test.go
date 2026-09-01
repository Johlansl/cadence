package procenv

import (
	"os"
	"slices"
	"strings"
	"testing"
)

func TestForStripsCadenceVars(t *testing.T) {
	t.Setenv("CADENCE_TOKEN", "secret-token")
	t.Setenv("CADENCE_SERVER_URL", "https://example.invalid")
	t.Setenv("PATH", "/usr/bin")

	env := For("LC_ALL=C")

	for _, kv := range env {
		if strings.HasPrefix(kv, "CADENCE_") {
			t.Fatalf("CADENCE_ variable leaked into child env: %q", kv)
		}
	}
	if !slices.Contains(env, "PATH=/usr/bin") {
		t.Errorf("a non-CADENCE variable (PATH) was dropped")
	}
	if !slices.Contains(env, "LC_ALL=C") {
		t.Errorf("override LC_ALL=C is missing")
	}
}

func TestForKeepsOverrideOrderLast(t *testing.T) {
	env := For("LC_ALL=C", "DEBIAN_FRONTEND=noninteractive")
	got := env[len(env)-2:]
	want := []string{"LC_ALL=C", "DEBIAN_FRONTEND=noninteractive"}
	if !slices.Equal(got, want) {
		t.Errorf("overrides not appended in order: got %v want %v", got, want)
	}
}

func TestForDoesNotMutateProcessEnv(t *testing.T) {
	t.Setenv("CADENCE_TOKEN", "x")
	_ = For()
	if os.Getenv("CADENCE_TOKEN") != "x" {
		t.Errorf("For() mutated the process environment")
	}
}
