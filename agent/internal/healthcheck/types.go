// Package healthcheck runs bounded, structured checks around package upgrades.
// It does not repair the host or restart services.
package healthcheck

// Status is the outcome of one check or an aggregate phase.
type Status string

const (
	StatusPassed  Status = "passed"
	StatusWarning Status = "warning"
	StatusFailed  Status = "failed"
	StatusUnknown Status = "unknown"
)

// HealthStatus is the host health derived from post-upgrade checks.
type HealthStatus string

const (
	HealthHealthy   HealthStatus = "healthy"
	HealthDegraded  HealthStatus = "degraded"
	HealthUnhealthy HealthStatus = "unhealthy"
	HealthUnknown   HealthStatus = "unknown"
)

const (
	CheckDiskSpace       = "disk_space"
	CheckPackageLocks    = "package_manager_locks"
	CheckDPKGAudit       = "dpkg_audit"
	CheckAPTDependencies = "apt_dependencies"
	CheckPackageIndexes  = "package_indexes"
	CheckFailedServices  = "failed_services"
	CheckRebootRequired  = "reboot_required"
)

// Result is one bounded, machine-readable check result.
type Result struct {
	Name    string  `json:"name"`
	Status  Status  `json:"status"`
	Summary string  `json:"summary"`
	Details Details `json:"details"`
}

// Details carries the small, check-specific evidence useful to an operator.
// Fields irrelevant to a check are omitted.
type Details struct {
	Filesystems      []FilesystemResult `json:"filesystems,omitempty"`
	Locks            []HeldLock         `json:"locks,omitempty"`
	Problems         []string           `json:"problems,omitempty"`
	Services         []string           `json:"services,omitempty"`
	NewServices      []string           `json:"new_services,omitempty"`
	ExistingServices []string           `json:"existing_services,omitempty"`
	StrictMode       *bool              `json:"strict_mode,omitempty"`
	Required         *bool              `json:"required,omitempty"`
}

// Phase is the aggregate outcome and ordered checks for one side of an upgrade.
type Phase struct {
	Status Status   `json:"status"`
	Checks []Result `json:"checks"`
}

// NewPhase computes an aggregate with failed taking precedence over unknown,
// then warning. An empty phase is unknown rather than optimistically passed.
func NewPhase(checks ...Result) Phase {
	phase := Phase{Status: aggregateStatus(checks), Checks: checks}
	if phase.Checks == nil {
		phase.Checks = []Result{}
	}
	return phase
}

func aggregateStatus(checks []Result) Status {
	if len(checks) == 0 {
		return StatusUnknown
	}
	worst := StatusPassed
	for _, check := range checks {
		switch check.Status {
		case StatusFailed:
			return StatusFailed
		case StatusUnknown:
			worst = StatusUnknown
		case StatusWarning:
			if worst == StatusPassed {
				worst = StatusWarning
			}
		case StatusPassed:
		default:
			worst = StatusUnknown
		}
	}
	return worst
}

// HealthFromPostChecks translates post-check severity into host health.
func HealthFromPostChecks(checks []Result) HealthStatus {
	switch aggregateStatus(checks) {
	case StatusPassed:
		return HealthHealthy
	case StatusWarning:
		return HealthDegraded
	case StatusFailed:
		return HealthUnhealthy
	default:
		return HealthUnknown
	}
}
