// Package bootid reads the Linux boot identifier: the kernel-generated UUID
// in /proc/sys/kernel/random/boot_id, stable for the whole boot and different
// after every reboot. The server compares it across contacts to tell whether
// a host came back from the reboot it was asked to perform.
package bootid

import (
	"os"
	"strings"
)

// Path to the kernel boot identifier.
const Path = "/proc/sys/kernel/random/boot_id"

// Read returns this boot's identifier, or "" when it cannot be read (a
// non-Linux host, a restricted container). Callers omit an empty value so the
// payload stays byte-identical to agents without boot support.
func Read() string {
	return ReadFrom(Path)
}

// ReadFrom is Read with the path injected, for testing.
func ReadFrom(path string) string {
	raw, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(raw))
}
