// Package report defines the JSON payload the agent sends to the Cadence server
// (POST /api/v1/reports). See CLAUDE.md section 5 for the wire contract.
package report

// Package is one installed package, optionally carrying a pending update
// (CandidateVersion / UpdateOrigin / IsSecurityUpdate set).
type Package struct {
	Name             string  `json:"name"`
	Architecture     string  `json:"architecture"`
	InstalledVersion string  `json:"installed_version"`
	CandidateVersion *string `json:"candidate_version"`
	IsSecurityUpdate bool    `json:"is_security_update"`
	UpdateOrigin     *string `json:"update_origin"`
}

// Report is the full body of a single agent report.
type Report struct {
	AgentVersion   string    `json:"agent_version"`
	Hostname       string    `json:"hostname"`
	FQDN           *string   `json:"fqdn"`
	OSFamily       string    `json:"os_family"`
	OSName         *string   `json:"os_name"`
	OSVersion      *string   `json:"os_version"`
	PackageManager string    `json:"package_manager"`
	RebootRequired bool      `json:"reboot_required"`
	Packages       []Package `json:"packages"`
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
