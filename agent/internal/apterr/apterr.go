// Package apterr classifies apt-get / dpkg failure output. Two uses:
//
//   - the collector and the executor react to a transient held lock (retry) and
//     a half-configured dpkg state (`dpkg --configure -a`);
//   - the executor turns a failed job into a coarse failure category plus a
//     one-line summary for the server (roadmap item 2, job-failure
//     classification).
//
// The categories are deliberately broad. `LC_ALL=C` is forced on every apt/dpkg
// invocation so the language is stable, but the exact wording still varies with
// the apt / dpkg version on the host, so the patterns key on stable stems
// ("Failed to fetch", "No space left on device", "Sub-process /usr/bin/dpkg
// returned an error code"). Output that matches nothing lands in "unknown" with
// a useful summary, which is an accepted outcome, not a bug.
package apterr

import (
	"regexp"
	"strings"
)

// Failure categories produced by Classify. The server also knows two categories
// this package never returns: "agent_lost" (a job reaped with no result while
// the host was silent) and "agent_refused" (the agent declined a job before
// running anything); the latter is set by the command layer, hence exported
// here for a single source of the string.
const (
	CategoryAptLocked     = "apt_locked"
	CategoryNetworkOrRepo = "network_or_repo"
	CategoryDpkgError     = "dpkg_error"
	CategoryDiskFull      = "disk_full"
	CategoryTimeout       = "timeout"
	CategoryAgentRefused  = "agent_refused"
	CategoryUnknown       = "unknown"
)

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

// diskFull matches an out-of-space failure from apt, dpkg or a helper. It
// outranks dpkgError in Classify because ENOSPC cascades into dpkg errors.
var diskFull = regexp.MustCompile(
	`(?i)no space left on device|you don't have enough free space` +
		`|write error.*no space|unrecoverable fatal error.*no space`,
)

// networkOrRepo matches an unreachable mirror / failed download / stale or
// missing Release file. Kept to reachability and fetch failures; a signing-key
// problem is left to fall through to "unknown".
var networkOrRepo = regexp.MustCompile(
	`(?i)failed to fetch|could not resolve|temporary failure resolving` +
		`|connection failed|connection timed out|connection refused` +
		`|unable to connect to|cannot initiate the connection to` +
		`|could not connect to|network is unreachable` +
		`|hash sum mismatch|does not have a release file` +
		`|release file for .* is not valid yet` +
		`|unable to fetch some archives|some index files failed to download`,
)

// dpkgError is the broad "apt ran, packages failed" bucket: a dpkg processing
// error, a broken/half-configured state, or an unmet-dependency / file-overwrite
// conflict. Merged on purpose (see the package doc): the apt output for a
// dependency conflict and for a maintainer-script failure overlaps too much to
// split reliably, and the summary carries the real detail.
var dpkgError = regexp.MustCompile(
	`(?i)sub-process /usr/bin/dpkg returned an error code` +
		`|dpkg: error processing|dpkg: dependency problems prevent` +
		`|errors were encountered while processing` +
		`|unmet dependencies|the following packages have unmet dependencies` +
		`|held broken packages|unable to correct problems` +
		`|trying to overwrite .* which is also in package`,
)

// IsLockHeld reports whether s (apt/dpkg output or an error string) indicates a
// transient held lock.
func IsLockHeld(s string) bool { return lockHeld.MatchString(s) }

// IsInterrupted reports whether s indicates a half-configured dpkg state that
// `dpkg --configure -a` can repair.
func IsInterrupted(s string) bool { return interrupted.MatchString(s) }

// IsDiskFull reports whether s indicates an out-of-space failure.
func IsDiskFull(s string) bool { return diskFull.MatchString(s) }

// IsNetworkOrRepo reports whether s indicates an unreachable mirror or a failed
// package/index download.
func IsNetworkOrRepo(s string) bool { return networkOrRepo.MatchString(s) }

// IsDpkgError reports whether s indicates a dpkg processing error, a broken
// package state, or an unmet-dependency / file-overwrite conflict.
func IsDpkgError(s string) bool { return dpkgError.MatchString(s) }

// Classify maps the output of the apt/dpkg command that actually failed to a
// coarse failure category. Pass the isolated buffer of the failing command, not
// a whole run's concatenated log: an earlier retry's held-lock message must not
// classify a job whose final failure was something else. deadlineExceeded is
// the caller's own signal that the job's context deadline fired (the process
// was killed), which outranks anything in the text.
//
// Precedence, first match wins: timeout, disk_full, apt_locked,
// network_or_repo, dpkg_error, else unknown.
func Classify(output string, deadlineExceeded bool) (category, summary string) {
	if deadlineExceeded {
		return CategoryTimeout, "job exceeded its time limit and was stopped"
	}
	switch {
	case IsDiskFull(output):
		category = CategoryDiskFull
	case IsLockHeld(output):
		category = CategoryAptLocked
	case IsNetworkOrRepo(output):
		category = CategoryNetworkOrRepo
	case IsDpkgError(output) || IsInterrupted(output):
		category = CategoryDpkgError
	default:
		category = CategoryUnknown
	}
	return category, Summary(output)
}

const summaryMaxBytes = 200

// Summary picks the single most informative line from apt/dpkg output: the last
// "E: ..." line if any, else the last "dpkg: error ..." line, else the last
// non-empty line. Whitespace is collapsed to single spaces and the result is
// capped at summaryMaxBytes. Returns "" for empty/all-blank input.
func Summary(s string) string {
	var errLine, dpkgLine, lastNonEmpty string
	for _, raw := range strings.Split(s, "\n") {
		line := strings.TrimSpace(raw)
		if line == "" {
			continue
		}
		lastNonEmpty = line
		if strings.HasPrefix(line, "E: ") {
			errLine = line
		}
		if strings.HasPrefix(line, "dpkg: error") {
			dpkgLine = line
		}
	}
	pick := lastNonEmpty
	switch {
	case errLine != "":
		pick = errLine
	case dpkgLine != "":
		pick = dpkgLine
	}
	pick = strings.Join(strings.Fields(pick), " ")
	if len(pick) > summaryMaxBytes {
		pick = strings.TrimSpace(strings.ToValidUTF8(pick[:summaryMaxBytes], ""))
	}
	return pick
}
