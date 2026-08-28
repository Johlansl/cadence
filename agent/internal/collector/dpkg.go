package collector

import (
	"context"
	"strings"

	"cadence/agent/internal/report"
)

// pkgKey identifies a package across dpkg and apt output.
func pkgKey(name, arch string) string { return name + "\t" + arch }

// installedPackages returns the currently installed packages keyed by
// name+architecture, using the exact dpkg-query format from the design brief
// (CLAUDE.md section 7).
func installedPackages(ctx context.Context) (map[string]report.Package, error) {
	out, err := runCommand(ctx, "dpkg-query", "-W",
		"-f=${Package}\t${Architecture}\t${Version}\n")
	if err != nil {
		return nil, err
	}

	pkgs := make(map[string]report.Package)
	for _, line := range strings.Split(string(out), "\n") {
		if line == "" {
			continue
		}
		fields := strings.Split(line, "\t")
		if len(fields) != 3 {
			continue
		}
		name, arch, version := fields[0], fields[1], fields[2]
		// An empty version means the package is known to dpkg but was never
		// unpacked (e.g. purge leftovers); not an installed package.
		if name == "" || version == "" {
			continue
		}
		pkgs[pkgKey(name, arch)] = report.Package{
			Name:             name,
			Architecture:     arch,
			InstalledVersion: version,
		}
	}
	return pkgs, nil
}
