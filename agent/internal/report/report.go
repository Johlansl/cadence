// Package report defines the JSON payload the agent sends to the Cadence server
// (POST /api/v1/reports) and the shape of the response. See
// docs/architecture.md, "Communication model", for the wire contract.
package report

import "encoding/json"

// Package is one installed package, optionally carrying a pending update
// (CandidateVersion / UpdateOrigin / IsSecurityUpdate set).
type Package struct {
	Name             string  `json:"name"`
	Architecture     string  `json:"architecture"`
	InstalledVersion string  `json:"installed_version"`
	CandidateVersion *string `json:"candidate_version"`
	IsSecurityUpdate bool    `json:"is_security_update"`
	UpdateOrigin     *string `json:"update_origin"`
	// SourcePackage is the Debian source package name (from dpkg's
	// ${source:Package}); omitted when unknown. The server uses it to link
	// security advisories precisely for library binaries.
	SourcePackage string `json:"source_package,omitempty"`
}

// Report is the full body of a single agent report.
type Report struct {
	AgentVersion   string    `json:"agent_version"`
	Hostname       string    `json:"hostname"`
	FQDN           *string   `json:"fqdn"`
	OSFamily       string    `json:"os_family"`
	OSName         *string   `json:"os_name"`
	OSVersion      *string   `json:"os_version"`
	OSCodename     *string   `json:"os_codename"`
	PackageManager string    `json:"package_manager"`
	RebootRequired bool      `json:"reboot_required"`
	Packages       []Package `json:"packages"`
	// ClaimJob is normally omitted, which lets the server preserve its default
	// of handing back one pending job. A post-job inventory report sets it to
	// false so it cannot claim a second job that this one-shot run will not
	// execute.
	ClaimJob *bool `json:"claim_job,omitempty"`
}

// JobHandoff is a job the server wants this host to run, delivered in the
// response to a report (piggyback -- see docs/architecture.md).
type JobHandoff struct {
	ID      string          `json:"id"`
	JobType string          `json:"job_type"`
	Params  json.RawMessage `json:"params"`
}

// RebootMode returns params.reboot ("auto" | "never" | ""). The server resolves
// the effective value (per-job override, else the host's reboot_policy) before
// handing the job off, so the agent does not need the host policy itself.
func (j JobHandoff) RebootMode() string {
	if len(j.Params) == 0 {
		return ""
	}
	var p struct {
		Reboot string `json:"reboot"`
	}
	_ = json.Unmarshal(j.Params, &p)
	return p.Reboot
}

// ExcludedPackages returns params.excluded_packages: the exact package names
// the server resolved from the operator's exclusion rules for this host
// (roadmap item 3). The server never sends a raw pattern, only names it read
// back from this host's own report; nil on absence or malformed JSON.
func (j JobHandoff) ExcludedPackages() []string {
	if len(j.Params) == 0 {
		return nil
	}
	var p struct {
		ExcludedPackages []string `json:"excluded_packages"`
	}
	_ = json.Unmarshal(j.Params, &p)
	return p.ExcludedPackages
}

// KnownHeldPackages returns params.known_held_packages: the packages the
// server last recorded Cadence itself holding on this host (from the most
// recent completed apt_upgrade job's held_packages result), used as the
// reconciliation baseline instead of a live `apt-mark showhold` read, so a
// hold Cadence has never itself recorded is never touched. Nil on absence or
// malformed JSON.
func (j JobHandoff) KnownHeldPackages() []string {
	if len(j.Params) == 0 {
		return nil
	}
	var p struct {
		KnownHeldPackages []string `json:"known_held_packages"`
	}
	_ = json.Unmarshal(j.Params, &p)
	return p.KnownHeldPackages
}

// Response is the body returned by POST /api/v1/reports. Only the piggybacked
// job is of interest to the agent; the rest is ignored.
type Response struct {
	Job *JobHandoff `json:"job"`
}

// DryRunPkg is one package line in an apt_dry_run preview (roadmap item 4).
// InstalledVersion is empty for a newly pulled dependency; CandidateVersion
// is empty for a removal.
type DryRunPkg struct {
	Name             string `json:"name"`
	Architecture     string `json:"architecture"`
	InstalledVersion string `json:"installed_version"`
	CandidateVersion string `json:"candidate_version"`
	IsSecurityUpdate bool   `json:"is_security_update"`
}

// DryRun is the structured result of an apt_dry_run job: what a real
// apt_upgrade would do right now, computed from `apt-get -s dist-upgrade`
// without changing anything on the host. Every list is always present (never
// null) so a consumer can rely on the shape; an empty list means nothing in
// that category. Excluded holds names dropped from Updated/NewlyInstalled
// because they match an active Cadence exclusion rule (even one no real run
// has applied as a hold yet); HeldInPlace holds names apt itself kept back
// because of a hold already on the box.
type DryRun struct {
	Updated        []DryRunPkg `json:"updated"`
	NewlyInstalled []DryRunPkg `json:"newly_installed"`
	Removed        []DryRunPkg `json:"removed"`
	KeptBack       []string    `json:"kept_back"`
	Excluded       []string    `json:"excluded"`
	HeldInPlace    []string    `json:"held_in_place"`
}

// Counts returns the number of packages with a pending update, and how many of
// those are flagged as security updates. Used for logging only; the server
// computes its own counters.
func (r Report) Counts() (updates, security int) {
	for _, p := range r.Packages {
		if p.CandidateVersion == nil {
			continue
		}
		updates++
		if p.IsSecurityUpdate {
			security++
		}
	}
	return updates, security
}
