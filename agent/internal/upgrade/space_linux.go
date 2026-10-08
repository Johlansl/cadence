//go:build linux

package upgrade

import "syscall"

// freeBytes reports the bytes available to unprivileged writers on the
// filesystem holding path. Best-effort precheck input only.
func freeBytes(path string) (uint64, bool) {
	var st syscall.Statfs_t
	if err := syscall.Statfs(path, &st); err != nil {
		return 0, false
	}
	return uint64(st.Bavail) * uint64(st.Bsize), true
}
