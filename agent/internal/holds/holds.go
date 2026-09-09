// Package holds implements the agent side of package exclusion / hold
// policies (roadmap item 3): validating the package names the server sends,
// diffing them against dpkg's actual hold state to reconcile it, and
// correlating a dist-upgrade run's output back to the exact names this agent
// held, so a genuine hold/dependency conflict can be reported explicitly
// instead of guessed at.
package holds

import (
	"regexp"
	"sort"
	"strings"

	"cadence/agent/internal/apterr"
)

// packageName matches a syntactically valid Debian package name (Debian
// Policy 5.6.7): lowercase letters and digits, then letters, digits, '+',
// '-', '.'; at least two characters. The server only ever sends names it read
// back from this host's own report, so this should never reject anything in
// practice -- it is the client-side half of the calc/exec split (the server
// resolves patterns to names; the agent still never passes an unvalidated
// string on to a command).
var packageName = regexp.MustCompile(`^[a-z0-9][a-z0-9+.-]+$`)

// Valid reports whether name is a syntactically valid Debian package name.
func Valid(name string) bool { return packageName.MatchString(name) }

// Filter splits names into the ones that pass Valid and the ones that do not.
// rejected is for logging: an invalid name is never silently dropped without
// a trace, and never passed to a command.
func Filter(names []string) (valid, rejected []string) {
	for _, n := range names {
		if Valid(n) {
			valid = append(valid, n)
		} else {
			rejected = append(rejected, n)
		}
	}
	return valid, rejected
}

// ParseShowHold parses `apt-mark showhold` output: one package name per line.
func ParseShowHold(stdout string) []string {
	var names []string
	for _, line := range strings.Split(stdout, "\n") {
		if line = strings.TrimSpace(line); line != "" {
			names = append(names, line)
		}
	}
	return names
}

// Diff computes what to hold / unhold to reconcile current dpkg hold state
// toward want. Results are sorted for deterministic logging and tests.
func Diff(current, want []string) (toHold, toUnhold []string) {
	currentSet := toSet(current)
	wantSet := toSet(want)
	for n := range wantSet {
		if !currentSet[n] {
			toHold = append(toHold, n)
		}
	}
	for n := range currentSet {
		if !wantSet[n] {
			toUnhold = append(toUnhold, n)
		}
	}
	sort.Strings(toHold)
	sort.Strings(toUnhold)
	return toHold, toUnhold
}

func toSet(names []string) map[string]bool {
	m := make(map[string]bool, len(names))
	for _, n := range names {
		m[n] = true
	}
	return m
}

// keptBack matches apt's own notice that it did not upgrade something it
// otherwise could have -- the strongest available signal that a hold changed
// the outcome of a run (as opposed to just quietly not being considered,
// which is the exclusion working exactly as configured and not something an
// operator needs flagged).
var keptBack = regexp.MustCompile(`(?i)have been kept back`)

// Correlate reports which of heldNames are named in dist-upgrade's own
// output, but only when that output shows real evidence of a problem (apt's
// "kept back" notice, or the dependency-conflict markers apterr.IsDpkgError
// already matches). Absent either signal, Correlate returns nil: a hold that
// simply, silently did its job leaves no trace here, by design. Only names
// this agent actually held are ever returned -- a name whose apt-mark hold
// itself failed was never really held, so it cannot be blamed for anything.
func Correlate(output string, heldNames []string) []string {
	if len(heldNames) == 0 {
		return nil
	}
	if !keptBack.MatchString(output) && !apterr.IsDpkgError(output) {
		return nil
	}
	var out []string
	for _, name := range heldNames {
		if mentions(output, name) {
			out = append(out, name)
		}
	}
	sort.Strings(out)
	return out
}

// mentions reports whether word appears in s at a word boundary, so a held
// "curl" does not spuriously match output naming "libcurl4".
func mentions(s, word string) bool {
	re := regexp.MustCompile(`\b` + regexp.QuoteMeta(word) + `\b`)
	return re.MatchString(s)
}
