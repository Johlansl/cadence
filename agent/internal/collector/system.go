package collector

import (
	"bufio"
	"os"
	"strings"
)

// readOSRelease parses /etc/os-release and returns (family, name, version).
// family defaults to "debian" and is taken from the ID field when present.
func readOSRelease() (family, name, version string) {
	family = "debian"

	f, err := os.Open("/etc/os-release")
	if err != nil {
		return family, "", ""
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
	return family, kv["NAME"], kv["VERSION_ID"]
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

// rebootRequired reports whether /var/run/reboot-required exists (Debian/Ubuntu).
func rebootRequired() bool {
	_, err := os.Stat("/var/run/reboot-required")
	return err == nil
}
