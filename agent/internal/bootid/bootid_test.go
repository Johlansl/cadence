package bootid

import (
	"os"
	"path/filepath"
	"testing"
)

func TestReadFrom(t *testing.T) {
	dir := t.TempDir()
	valid := filepath.Join(dir, "boot_id")
	if err := os.WriteFile(valid, []byte("8c8d5f7e-3b2a-4c1d-9e6f-0123456789ab\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	blank := filepath.Join(dir, "blank")
	if err := os.WriteFile(blank, []byte("  \n"), 0o644); err != nil {
		t.Fatal(err)
	}

	tests := []struct {
		name string
		path string
		want string
	}{
		{"valid is trimmed", valid, "8c8d5f7e-3b2a-4c1d-9e6f-0123456789ab"},
		{"missing reads empty", filepath.Join(dir, "nope"), ""},
		{"blank reads empty", blank, ""},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := ReadFrom(tc.path); got != tc.want {
				t.Errorf("ReadFrom(%q) = %q, want %q", tc.path, got, tc.want)
			}
		})
	}
}
