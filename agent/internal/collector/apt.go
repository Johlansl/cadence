package collector

import (
	"context"
	"regexp"
	"sort"
	"strings"

	"cadence/agent/internal/report"
)

// pendingUpdate is a parsed `Inst` line from `apt-get -s dist-upgrade`.
type pendingUpdate struct {
	name       string
	arch       string
	candidate  string
	origin     string
	isSecurity bool
}

func aptUpdate(ctx context.Context) error {
	_, err := runCommand(ctx, "apt-get", "update")
	return err
}

// instLine matches:  Inst <name> [<old>]? (<candidate> <origin...> [<arch>])
var instLine = regexp.MustCompile(`^Inst\s+(\S+)\s+(?:\[[^\]]*\]\s+)?\(([^)]*)\)`)

// trailingArch matches the "[amd64]" / "[all]" suffix inside the parentheses.
var trailingArch = regexp.MustCompile(`\s*\[([^\]]+)\]\s*$`)

// parseInstLine parses one line of `apt-get -s dist-upgrade` output. It returns
// ok=false for any line that is not a parseable `Inst` line.
//
// The security flag is a heuristic: apt has no explicit "security" marker, so we
// look for the substring "security" in the package's origin string (e.g.
// "Debian-Security:12/stable-security"). Documented as a heuristic, not truth.
func parseInstLine(line string) (pendingUpdate, bool) {
	m := instLine.FindStringSubmatch(line)
	if m == nil {
		return pendingUpdate{}, false
	}
	name := m[1]
	inner := strings.TrimSpace(m[2])

	arch := ""
	if loc := trailingArch.FindStringSubmatchIndex(inner); loc != nil {
		arch = inner[loc[2]:loc[3]]
		inner = strings.TrimSpace(inner[:loc[0]])
	}

	fields := strings.Fields(inner)
	if len(fields) == 0 {
		return pendingUpdate{}, false
	}
	candidate := fields[0]
	origin := strings.TrimSpace(strings.TrimPrefix(inner, candidate))

	return pendingUpdate{
		name:       name,
		arch:       arch,
		candidate:  candidate,
		origin:     origin,
		isSecurity: strings.Contains(strings.ToLower(origin), "security"),
	}, true
}

// pendingUpdates parses `apt-get -s dist-upgrade` into a map keyed by
// name+architecture.
func pendingUpdates(ctx context.Context) (map[string]pendingUpdate, error) {
	out, err := runCommand(ctx, "apt-get", "-s", "dist-upgrade")
	if err != nil {
		return nil, err
	}

	updates := make(map[string]pendingUpdate)
	for _, line := range strings.Split(string(out), "\n") {
		if !strings.HasPrefix(line, "Inst ") {
			continue
		}
		if u, ok := parseInstLine(line); ok {
			updates[pkgKey(u.name, u.arch)] = u
		}
	}
	return updates, nil
}

// mergeUpdates folds pending updates into the installed set. Packages that
// appear in apt output but are not installed (new dependencies pulled by a
// dist-upgrade) are ignored: an update is always relative to an installed
// package. The result is sorted for deterministic payloads.
func mergeUpdates(installed map[string]report.Package, updates map[string]pendingUpdate) []report.Package {
	// Fallback index for apt lines that carried no architecture.
	byName := make(map[string]pendingUpdate, len(updates))
	for _, u := range updates {
		if u.arch == "" {
			byName[u.name] = u
		}
	}

	out := make([]report.Package, 0, len(installed))
	for key, p := range installed {
		if u, ok := updates[key]; ok {
			applyUpdate(&p, u)
		} else if u, ok := byName[p.Name]; ok {
			applyUpdate(&p, u)
		}
		out = append(out, p)
	}

	sort.Slice(out, func(i, j int) bool {
		if out[i].Name != out[j].Name {
			return out[i].Name < out[j].Name
		}
		return out[i].Architecture < out[j].Architecture
	})
	return out
}

func applyUpdate(p *report.Package, u pendingUpdate) {
	candidate := u.candidate
	p.CandidateVersion = &candidate
	p.IsSecurityUpdate = u.isSecurity
	if u.origin != "" {
		origin := u.origin
		p.UpdateOrigin = &origin
	}
}
