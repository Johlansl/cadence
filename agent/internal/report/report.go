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

// Response is the body returned by POST /api/v1/reports. Only the piggybacked
// job is of interest to the agent; the rest is ignored.
type Response struct {
	Job *JobHandoff `json:"job"`
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
