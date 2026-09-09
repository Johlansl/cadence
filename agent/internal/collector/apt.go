package collector

import (
	"context"
	"sort"
	"strings"
	"time"

	"cadence/agent/internal/apterr"
	"cadence/agent/internal/aptsim"
	"cadence/agent/internal/logging"
	"cadence/agent/internal/report"
)

func aptUpdate(ctx context.Context) error {
	_, err := runCommand(ctx, "apt-get", "update")
	return err
}

// aptRetryWaits is the delay before attempts 2..N of the dist-upgrade
// simulation when the apt lock is held. A package var so tests can shorten it.
var aptRetryWaits = []time.Duration{0, 5 * time.Second, 15 * time.Second, 30 * time.Second}

// dpkgRepairTimeout bounds the one-shot `dpkg --configure -a` on the report
// path so a wedged dpkg can't hang the whole report.
const dpkgRepairTimeout = 5 * time.Minute

// pendingUpdatesWithRetry runs `apt-get -s dist-upgrade`. It repairs a
// half-configured dpkg state once (`dpkg --configure -a`) and retries, and it
// retries a few times while another process holds the apt lock
// (unattended-upgrades). Any other error fails fast.
func pendingUpdatesWithRetry(ctx context.Context) (map[string]aptsim.Inst, error) {
	updates, err := pendingUpdates(ctx)
	if err == nil {
		return updates, nil
	}

	// A dpkg transaction left half-applied (a killed apt, a crash) makes every
	// simulation fail until it is finished. Self-heal once instead of erroring
	// on every report until an operator intervenes.
	if apterr.IsInterrupted(err.Error()) {
		logging.Warn("dpkg is in an interrupted state, running dpkg --configure -a")
		if rerr := repairInterruptedDpkg(ctx); rerr != nil {
			logging.Warn("dpkg --configure -a failed", "err", rerr)
			return nil, err
		}
		if updates, err = pendingUpdates(ctx); err == nil {
			return updates, nil
		}
	}

	if !apterr.IsLockHeld(err.Error()) {
		return nil, err
	}
	for i := 1; i < len(aptRetryWaits); i++ {
		select {
		case <-time.After(aptRetryWaits[i]):
		case <-ctx.Done():
			return nil, ctx.Err()
		}
		logging.Warn("apt lock held, retrying dist-upgrade simulation")
		if updates, err = pendingUpdates(ctx); err == nil {
			return updates, nil
		}
		if !apterr.IsLockHeld(err.Error()) {
			return nil, err
		}
	}
	return nil, err
}

// repairInterruptedDpkg finishes an interrupted dpkg transaction, bounded by
// its own timeout (a child of ctx so it never outlives the report attempt).
func repairInterruptedDpkg(ctx context.Context) error {
	rctx, cancel := context.WithTimeout(ctx, dpkgRepairTimeout)
	defer cancel()
	_, err := runCommand(rctx, "dpkg", "--configure", "-a")
	return err
}

// pendingUpdates parses `apt-get -s dist-upgrade` into a map of pending
// `Inst` lines keyed by name+architecture. Only `Inst` lines matter for the
// periodic report; removals, kept-back packages and newly pulled dependencies
// are the dry-run path's concern (see internal/aptsim).
func pendingUpdates(ctx context.Context) (map[string]aptsim.Inst, error) {
	out, err := runCommand(ctx, "apt-get", "-s", "dist-upgrade")
	if err != nil {
		return nil, err
	}

	updates := make(map[string]aptsim.Inst)
	for _, line := range strings.Split(string(out), "\n") {
		if !strings.HasPrefix(line, "Inst ") {
			continue
		}
		if u, ok := aptsim.ParseInst(line); ok {
			updates[pkgKey(u.Name, u.Arch)] = u
		}
	}
	return updates, nil
}

// mergeUpdates folds pending updates into the installed set. Packages that
// appear in apt output but are not installed (new dependencies pulled by a
// dist-upgrade) are ignored: an update is always relative to an installed
// package. The result is sorted for deterministic payloads.
func mergeUpdates(installed map[string]report.Package, updates map[string]aptsim.Inst) []report.Package {
	// Fallback index for apt lines that carried no architecture.
	byName := make(map[string]aptsim.Inst, len(updates))
	for _, u := range updates {
		if u.Arch == "" {
			byName[u.Name] = u
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

func applyUpdate(p *report.Package, u aptsim.Inst) {
	candidate := u.Candidate
	p.CandidateVersion = &candidate
	p.IsSecurityUpdate = u.IsSecurity
	if u.Origin != "" {
		origin := u.Origin
		p.UpdateOrigin = &origin
	}
}
