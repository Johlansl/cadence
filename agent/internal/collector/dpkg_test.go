package collector

import (
	"context"
	"os"
	"path/filepath"
	"testing"
)

// fakeDpkgQuery puts a `dpkg-query` on PATH that prints `output` verbatim
// (the -f template is ignored -- output is already in the expected layout).
func fakeDpkgQuery(t *testing.T, output string) {
	t.Helper()
	dir := t.TempDir()
	script := "#!/bin/sh\ncat <<'CADENCE_EOF'\n" + output + "CADENCE_EOF\n"
	if err := os.WriteFile(filepath.Join(dir, "dpkg-query"), []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
}

func TestInstalledPackagesKeepsOnlyInstalledStatus(t *testing.T) {
	fakeDpkgQuery(t, ""+
		"bash\tamd64\t5.2-1\tinstalled\tbash\n"+
		"libssl3\tamd64\t3.0.11-1\tinstalled\topenssl\n"+
		"curl\tamd64\t7.88-1\tinstalled\n"+ // 4-column: older layout, still accepted
		"oldpkg\tamd64\t1.0-1\tconfig-files\toldpkg\n"+ // removed, not purged
		"nothere\tamd64\t\tnot-installed\tnothere\n"+ // never installed
		"broken\tamd64\t2.0\thalf-configured\tbroken\n"+ // mid-transaction
		"tooshort\tamd64\t1.0\n") // malformed: 3 fields

	pkgs, err := installedPackages(context.Background())
	if err != nil {
		t.Fatal(err)
	}

	if len(pkgs) != 3 {
		t.Fatalf("want 3 installed packages, got %d: %v", len(pkgs), pkgs)
	}
	for _, want := range []string{
		pkgKey("bash", "amd64"), pkgKey("libssl3", "amd64"), pkgKey("curl", "amd64"),
	} {
		if _, ok := pkgs[want]; !ok {
			t.Errorf("missing %q", want)
		}
	}
	for _, skip := range []string{
		pkgKey("oldpkg", "amd64"), pkgKey("nothere", "amd64"),
		pkgKey("broken", "amd64"), pkgKey("tooshort", "amd64"),
	} {
		if _, ok := pkgs[skip]; ok {
			t.Errorf("%q should have been excluded", skip)
		}
	}
	if got := pkgs[pkgKey("bash", "amd64")].InstalledVersion; got != "5.2-1" {
		t.Errorf("bash version = %q, want 5.2-1", got)
	}
}

func TestInstalledPackagesReadsSourcePackage(t *testing.T) {
	fakeDpkgQuery(t, ""+
		"libssl3\tamd64\t3.0.11-1\tinstalled\topenssl\n"+
		"tar\tamd64\t1.34-1\tinstalled\n") // 4-column: no source column

	pkgs, err := installedPackages(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if got := pkgs[pkgKey("libssl3", "amd64")].SourcePackage; got != "openssl" {
		t.Errorf("libssl3 source = %q, want openssl", got)
	}
	if got := pkgs[pkgKey("tar", "amd64")].SourcePackage; got != "" {
		t.Errorf("tar source = %q, want empty (4-column tolerance)", got)
	}
}
