//go:build !linux

package upgrade

// freeBytes is unknown off Linux: the install proceeds without the
// best-effort space precheck (write errors still fail safely).
func freeBytes(_ string) (uint64, bool) {
	return 0, false
}
