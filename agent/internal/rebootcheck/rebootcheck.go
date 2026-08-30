// Package rebootcheck decides whether the host needs a reboot, without
// depending on update-notifier-common / reboot-notifier being installed
// (update-notifier-common was dropped from Debian 13).
//
// A reboot is pending when /var/run/reboot-required exists (the classic
// signal, when a helper created it) OR an installed kernel image of the
// running flavour is strictly newer than the running kernel.
package rebootcheck

import (
	"context"
	"os"
	"os/exec"
	"strings"
	"time"
)

// Pending reports whether the host needs a reboot.
func Pending() bool {
	if _, err := os.Stat("/var/run/reboot-required"); err == nil {
		return true
	}
	return kernelPending()
}

func run(name string, args ...string) string {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, name, args...)
	cmd.Env = append(os.Environ(), "LC_ALL=C", "LANG=C")
	out, err := cmd.Output()
	if err != nil {
		return ""
	}
	return string(out)
}

// flavour returns the trailing "-amd64" / "-cloud-amd64" style suffix of a
// kernel version string, or "" if there is none.
func flavour(kver string) string {
	i := strings.LastIndexByte(kver, '-')
	if i < 0 {
		return ""
	}
	return kver[i:]
}

func kernelPending() bool {
	running := strings.TrimSpace(run("uname", "-r")) // e.g. 6.1.0-52-amd64
	fl := flavour(running)
	if running == "" || fl == "" {
		return false // can't tell -- don't cry wolf
	}

	out := run("dpkg-query", "-W", "-f=${Package} ${Version} ${db:Status-Status}\n", "linux-image-*")
	var runningVer string
	type kpkg struct{ ver string }
	var candidates []kpkg
	for _, line := range strings.Split(out, "\n") {
		f := strings.Fields(line)
		if len(f) < 3 || f[2] != "installed" {
			continue
		}
		suffix := strings.TrimPrefix(f[0], "linux-image-")
		if suffix == f[0] || suffix == "" || suffix[0] < '0' || suffix[0] > '9' {
			continue // not linux-image-<version>-..., or the meta package
		}
		if flavour(suffix) != fl {
			continue // different flavour (cloud / rt / ...)
		}
		if suffix == running {
			runningVer = f[1]
			continue
		}
		candidates = append(candidates, kpkg{ver: f[1]})
	}
	if runningVer == "" {
		return false // running kernel has no matching package -- unknown
	}
	for _, k := range candidates {
		if dpkgVersionGreater(k.ver, runningVer) {
			return true
		}
	}
	return false
}

func dpkgVersionGreater(a, b string) bool {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	return exec.CommandContext(ctx, "dpkg", "--compare-versions", a, "gt", b).Run() == nil
}
