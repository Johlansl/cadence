package collector

import (
	"context"
	"strings"

	"cadence/agent/internal/report"
)

// pkgKey identifies a package across dpkg and apt output.
func pkgKey(name, arch string) string { return name + "\t" + arch }

// installedPackages returns the currently installed packages keyed by
// name+architecture. It asks dpkg-query for name/arch/version plus
// ${db:Status-Status} so packages that are removed but not purged
// ("config-files", still carry a version) or mid-transaction are excluded --
// same check rebootcheck already makes. The trailing ${source:Package} column
// carries the Debian source package name (equal to ${Package} when there is no
// Source: field) so the server can link security advisories for library
// binaries whose source name differs (libssl3 -> openssl).
func installedPackages(ctx context.Context) (map[string]report.Package, error) {
	out, err := runCommand(ctx, "dpkg-query", "-W",
		"-f=${Package}\t${Architecture}\t${Version}\t${db:Status-Status}\t${source:Package}\n")
	if err != nil {
		return nil, err
	}

	pkgs := make(map[string]report.Package)
	for _, line := range strings.Split(string(out), "\n") {
		if line == "" {
			continue
		}
		// 5 fields is the current layout; 4 is tolerated (older template, or a
		// package with no resolvable source). Anything else is malformed.
		fields := strings.Split(line, "\t")
		if len(fields) != 4 && len(fields) != 5 {
			continue
		}
		name, arch, version, status := fields[0], fields[1], fields[2], fields[3]
		if name == "" || version == "" || status != "installed" {
			continue
		}
		source := ""
		if len(fields) == 5 {
			source = fields[4]
		}
		pkgs[pkgKey(name, arch)] = report.Package{
			Name:             name,
			Architecture:     arch,
			InstalledVersion: version,
			SourcePackage:    source,
		}
	}
	return pkgs, nil
}
