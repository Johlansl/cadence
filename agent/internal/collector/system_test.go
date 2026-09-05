package collector

import (
	"os"
	"path/filepath"
	"testing"
)

func writeOSRelease(t *testing.T, body string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "os-release")
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestReadOSReleaseFrom(t *testing.T) {
	path := writeOSRelease(t, ""+
		"PRETTY_NAME=\"Debian GNU/Linux 12 (bookworm)\"\n"+
		"NAME=\"Debian GNU/Linux\"\n"+
		"VERSION_ID=\"12\"\n"+
		"VERSION_CODENAME=bookworm\n"+
		"ID=debian\n"+
		"# a comment\n")

	family, name, version, codename := readOSReleaseFrom(path)
	if family != "debian" {
		t.Errorf("family = %q, want debian", family)
	}
	if name != "Debian GNU/Linux" {
		t.Errorf("name = %q, want %q", name, "Debian GNU/Linux")
	}
	if version != "12" {
		t.Errorf("version = %q, want 12", version)
	}
	if codename != "bookworm" {
		t.Errorf("codename = %q, want bookworm", codename)
	}
}

func TestReadOSReleaseFromWithoutCodename(t *testing.T) {
	path := writeOSRelease(t, "NAME=\"Custom\"\nVERSION_ID=\"99\"\n")

	_, _, version, codename := readOSReleaseFrom(path)
	if version != "99" {
		t.Errorf("version = %q, want 99", version)
	}
	if codename != "" {
		t.Errorf("codename = %q, want empty", codename)
	}
}

func TestReadOSReleaseFromMissingFile(t *testing.T) {
	family, name, version, codename := readOSReleaseFrom(filepath.Join(t.TempDir(), "nope"))
	if family != "debian" || name != "" || version != "" || codename != "" {
		t.Errorf("got (%q, %q, %q, %q), want (debian, empty, empty, empty)",
			family, name, version, codename)
	}
}
