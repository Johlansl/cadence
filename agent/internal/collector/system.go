package collector

import (
	"bufio"
	"os"
	"strings"

	"cadence/agent/internal/rebootcheck"
)

// readOSRelease parses /etc/os-release and returns
// (family, name, version, codename). family defaults to "debian" and is taken
// from the ID field when present; codename is VERSION_CODENAME verbatim
// ("bookworm", "trixie") and is "" when the file omits it.
func readOSRelease() (family, name, version, codename string) {
	return readOSReleaseFrom("/etc/os-release")
}

// readOSReleaseFrom is readOSRelease with the path injected, for testing.
func readOSReleaseFrom(path string) (family, name, version, codename string) {
	family = "debian"

	f, err := os.Open(path)
	if err != nil {
		return family, "", "", ""
	}
	defer f.Close()

	kv := map[string]string{}
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		k, v, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		kv[strings.TrimSpace(k)] = strings.Trim(strings.TrimSpace(v), `"'`)
	}
	if v := kv["ID"]; v != "" {
		family = strings.ToLower(v)
	}
	return family, kv["NAME"], kv["VERSION_ID"], kv["VERSION_CODENAME"]
}

// hostnameInfo returns (shortHostname, fqdn). fqdn is nil unless the OS hostname
// is already qualified (contains a dot). The agent never forces a DNS lookup.
func hostnameInfo() (string, *string) {
	h, err := os.Hostname()
	if err != nil || h == "" {
		return "unknown", nil
	}
	if i := strings.IndexByte(h, '.'); i > 0 {
		fqdn := h
		return h[:i], &fqdn
	}
	return h, nil
}

// rebootRequired reports whether the host needs a reboot -- the
// /var/run/reboot-required marker, or a newer installed kernel. See
// rebootcheck.
func rebootRequired() bool {
	return rebootcheck.Pending()
}
