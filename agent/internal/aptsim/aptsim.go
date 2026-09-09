// Package aptsim parses the output of `apt-get -s dist-upgrade` (and the
// equivalent action lines apt prints on a real run). It is shared by two
// callers with different needs: the periodic report collector, which only
// looks at `Inst` lines for already-installed packages, and the dry-run
// executor, which needs the whole picture (removals, newly pulled
// dependencies, and packages apt kept back).
package aptsim

import (
	"regexp"
	"strings"
)

// Inst is one parsed `Inst` line. OldVersion is empty when apt would install
// the package fresh (a dependency pulled in by the upgrade) and non-empty when
// it is an upgrade of an already-installed package.
type Inst struct {
	Name       string
	Arch       string
	OldVersion string
	Candidate  string
	Origin     string
	IsSecurity bool
}

// Remv is one parsed `Remv` line: a package apt would remove.
type Remv struct {
	Name       string
	OldVersion string
}

// Simulation is the full parse of one `apt-get -s dist-upgrade` output.
type Simulation struct {
	Inst     []Inst
	Remv     []Remv
	KeptBack []string
}

// instLine matches:  Inst <name> [<old>]? (<candidate> <origin...> [<arch>])
// The old-version bracket is optional and captured (empty when absent, which
// is how a freshly pulled dependency is told apart from an upgrade).
var instLine = regexp.MustCompile(`^Inst\s+(\S+)\s+(?:\[([^\]]*)\]\s+)?\(([^)]*)\)`)

// remvLine matches:  Remv <name> [<old>]?
var remvLine = regexp.MustCompile(`^Remv\s+(\S+)(?:\s+\[([^\]]*)\])?`)

// trailingArch matches the "[amd64]" / "[all]" suffix inside the parentheses.
var trailingArch = regexp.MustCompile(`\s*\[([^\]]+)\]\s*$`)

// keptBackHeader is apt's own notice that opens the list of packages it did
// not upgrade even though it could have.
var keptBackHeader = regexp.MustCompile(`(?i)the following packages have been kept back:`)

// ParseInst parses one `Inst` line. It returns ok=false for any line that is
// not a parseable `Inst` line.
//
// The security flag is a heuristic: apt has no explicit "security" marker, so
// we look for the substring "security" in the package's origin string (e.g.
// "Debian-Security:12/stable-security"). Documented as a heuristic, not truth.
func ParseInst(line string) (Inst, bool) {
	m := instLine.FindStringSubmatch(line)
	if m == nil {
		return Inst{}, false
	}
	name := m[1]
	oldVersion := strings.TrimSpace(m[2])
	inner := strings.TrimSpace(m[3])

	arch := ""
	if loc := trailingArch.FindStringSubmatchIndex(inner); loc != nil {
		arch = inner[loc[2]:loc[3]]
		inner = strings.TrimSpace(inner[:loc[0]])
	}

	fields := strings.Fields(inner)
	if len(fields) == 0 {
		return Inst{}, false
	}
	candidate := fields[0]
	origin := strings.TrimSpace(strings.TrimPrefix(inner, candidate))

	return Inst{
		Name:       name,
		Arch:       arch,
		OldVersion: oldVersion,
		Candidate:  candidate,
		Origin:     origin,
		IsSecurity: strings.Contains(strings.ToLower(origin), "security"),
	}, true
}

// ParseRemv parses one `Remv` line. It returns ok=false for any other line.
func ParseRemv(line string) (Remv, bool) {
	m := remvLine.FindStringSubmatch(line)
	if m == nil {
		return Remv{}, false
	}
	return Remv{Name: m[1], OldVersion: strings.TrimSpace(m[2])}, true
}

// ParseKeptBack returns the package names in apt's "have been kept back:"
// block. apt prints them on one or more whitespace-indented continuation
// lines; the first non-indented line ends the block.
func ParseKeptBack(output string) []string {
	var names []string
	inBlock := false
	for _, ln := range strings.Split(output, "\n") {
		if keptBackHeader.MatchString(ln) {
			inBlock = true
			continue
		}
		if !inBlock {
			continue
		}
		if ln == "" || (ln[0] != ' ' && ln[0] != '\t') {
			inBlock = false
			continue
		}
		names = append(names, strings.Fields(ln)...)
	}
	return names
}

// Parse folds a whole `apt-get -s dist-upgrade` output into a Simulation.
func Parse(output string) Simulation {
	var sim Simulation
	for _, ln := range strings.Split(output, "\n") {
		switch {
		case strings.HasPrefix(ln, "Inst "):
			if v, ok := ParseInst(ln); ok {
				sim.Inst = append(sim.Inst, v)
			}
		case strings.HasPrefix(ln, "Remv "):
			if v, ok := ParseRemv(ln); ok {
				sim.Remv = append(sim.Remv, v)
			}
		}
	}
	sim.KeptBack = ParseKeptBack(output)
	return sim
}
