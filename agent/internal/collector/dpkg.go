package collector

import (
	"context"
	"strings"

	"cadence/agent/internal/report"
)

// pkgKey identifies a package across dpkg and apt output.
func pkgKey(name, arch string) string { return name + "\t" + arch }

// installedPackages returns the currently installed packages keyed by
// name+architecture. Based on the dpkg-query format from the design brief
// (CLAUDE.md section 7), plus ${db:Status-Status} so packages that are removed
// but not purged ("config-files", still carry a version) or mid-transaction
// are excluded -- same check rebootcheck already makes.
func installedPackages(ctx context.Context) (map[string]report.Package, error) {
	out, err := runCommand(ctx, "dpkg-query", "-W",
		"-f=${Package}\t${Architecture}\t${Version}\t${db:Status-Status}\n")
	if err != nil {
		return nil, err
	}

	pkgs := make(map[string]report.Package)
	for _, line := range strings.Split(string(out), "\n") {
		if line == "" {
			continue
		}
		fields := strings.Split(line, "\t")
		if len(fields) != 4 {
			continue
		}
		name, arch, version, status := fields[0], fields[1], fields[2], fields[3]
		if name == "" || version == "" || status != "installed" {
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
