// Package apterr classifies apt-get / dpkg failure output so the collector and
// the executor can react the same way: retry a transient held lock, and repair
// a half-configured dpkg state.
package apterr

import "regexp"

// lockHeld matches "another process holds the apt/dpkg lock" -- usually
// unattended-upgrades running concurrently. Transient: worth retrying.
var lockHeld = regexp.MustCompile(
	`(?i)could not get lock|dpkg frontend lock|unable to (?:acquire|lock)` +
		`|is another process using it|resource temporarily unavailable`,
)

// interrupted matches "dpkg was interrupted, you must manually run
// 'dpkg --configure -a'". Repairable with exactly that command.
var interrupted = regexp.MustCompile(
	`(?i)dpkg was interrupted|dpkg --configure -a`,
)

// IsLockHeld reports whether s (apt/dpkg output or an error string) indicates a
// transient held lock.
func IsLockHeld(s string) bool { return lockHeld.MatchString(s) }

// IsInterrupted reports whether s indicates a half-configured dpkg state that
// `dpkg --configure -a` can repair.
func IsInterrupted(s string) bool { return interrupted.MatchString(s) }
