package healthcheck

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"sort"
	"strings"
	"syscall"
	"time"
	"unicode/utf8"

	"cadence/agent/internal/procenv"
)

const (
	DefaultMinimumAvailableBytes     uint64 = 1024 * 1024 * 1024
	DefaultBootMinimumAvailableBytes uint64 = 200 * 1024 * 1024
	maxEvidenceItems                        = 100
	maxSummaryBytes                         = 500
)

// DefaultDiskTargets covers the filesystems apt and bootloader updates most
// commonly write to. Missing optional paths are ignored.
var DefaultDiskTargets = []DiskTarget{
	{Path: "/var", MinimumAvailableBytes: DefaultMinimumAvailableBytes},
	{Path: "/boot", MinimumAvailableBytes: DefaultBootMinimumAvailableBytes},
	{Path: "/boot/efi", MinimumAvailableBytes: DefaultBootMinimumAvailableBytes},
}

// DefaultLockPaths is the set of apt and dpkg advisory locks that must clear
// before an upgrade starts.
var DefaultLockPaths = []string{
	"/var/lib/dpkg/lock-frontend",
	"/var/lib/dpkg/lock",
	"/var/lib/apt/lists/lock",
	"/var/cache/apt/archives/lock",
}

// Runner executes a command and returns its combined standard output and
// standard error. Implementations must honor ctx.
type Runner interface {
	Run(ctx context.Context, name string, args ...string) (string, error)
}

// ExecRunner runs checks with a stable non-interactive locale.
type ExecRunner struct{}

func (ExecRunner) Run(ctx context.Context, name string, args ...string) (string, error) {
	cmd := exec.CommandContext(ctx, name, args...)
	cmd.Env = procenv.For("LC_ALL=C", "LANG=C", "DEBIAN_FRONTEND=noninteractive")
	out, err := cmd.CombinedOutput()
	return string(out), err
}

// DiskTarget defines a path to inspect and its minimum free-space threshold.
type DiskTarget struct {
	Path                  string
	MinimumAvailableBytes uint64
}

// Filesystem is the measured filesystem identity and available capacity.
type Filesystem struct {
	Device         uint64
	AvailableBytes uint64
}

// FilesystemResult is the bounded evidence returned by the disk check.
type FilesystemResult struct {
	Paths                 []string `json:"paths"`
	AvailableBytes        uint64   `json:"available_bytes"`
	MinimumAvailableBytes uint64   `json:"minimum_available_bytes"`
}

// DiskProbe resolves a path to the filesystem on which it resides.
type DiskProbe func(path string) (Filesystem, error)

// ProbeFilesystem measures a path using statfs.
func ProbeFilesystem(path string) (Filesystem, error) {
	info, err := os.Stat(path)
	if err != nil {
		return Filesystem{}, err
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		return Filesystem{}, errors.New("filesystem identity unavailable")
	}
	var fs syscall.Statfs_t
	if err := syscall.Statfs(path, &fs); err != nil {
		return Filesystem{}, err
	}
	return Filesystem{
		Device:         uint64(stat.Dev),
		AvailableBytes: fs.Bavail * uint64(fs.Bsize),
	}, nil
}

// DiskSpace checks free space once per filesystem. When several configured
// paths share a filesystem, the highest applicable threshold wins.
func DiskSpace(targets []DiskTarget, probe DiskProbe) Result {
	type measurement struct {
		fs      Filesystem
		paths   []string
		minimum uint64
	}
	byDevice := make(map[uint64]*measurement)
	var probeErrors []string

	for _, target := range targets {
		fs, err := probe(target.Path)
		if errors.Is(err, os.ErrNotExist) {
			continue
		}
		if err != nil {
			probeErrors = appendBounded(probeErrors, fmt.Sprintf("%s: %v", target.Path, err))
			continue
		}
		m := byDevice[fs.Device]
		if m == nil {
			m = &measurement{fs: fs}
			byDevice[fs.Device] = m
		}
		m.paths = append(m.paths, target.Path)
		if target.MinimumAvailableBytes > m.minimum {
			m.minimum = target.MinimumAvailableBytes
		}
	}

	measurements := make([]*measurement, 0, len(byDevice))
	for _, m := range byDevice {
		sort.Strings(m.paths)
		measurements = append(measurements, m)
	}
	sort.Slice(measurements, func(i, j int) bool {
		return strings.Join(measurements[i].paths, "\x00") < strings.Join(measurements[j].paths, "\x00")
	})

	result := Result{Name: CheckDiskSpace, Status: StatusPassed, Summary: "sufficient disk space"}
	var low int
	for _, m := range measurements {
		result.Details.Filesystems = append(result.Details.Filesystems, FilesystemResult{
			Paths:                 m.paths,
			AvailableBytes:        m.fs.AvailableBytes,
			MinimumAvailableBytes: m.minimum,
		})
		if m.fs.AvailableBytes < m.minimum {
			low++
		}
	}
	result.Details.Problems = probeErrors
	if low > 0 {
		result.Status = StatusFailed
		result.Summary = fmt.Sprintf("%d filesystem(s) below the free-space threshold", low)
		return result
	}
	if len(probeErrors) > 0 || len(measurements) == 0 {
		result.Status = StatusUnknown
		result.Summary = "could not verify disk space"
	}
	return result
}

// HeldLock describes one active advisory lock and, where available, its owner.
type HeldLock struct {
	Path string `json:"path"`
	PID  int    `json:"pid,omitempty"`
}

// LockProbe returns the currently held package-manager locks.
type LockProbe func(paths []string) ([]HeldLock, error)

// ProbePackageManagerLocks queries POSIX record locks without acquiring them.
func ProbePackageManagerLocks(paths []string) ([]HeldLock, error) {
	var held []HeldLock
	for _, path := range paths {
		file, err := os.OpenFile(path, os.O_RDWR, 0)
		if errors.Is(err, os.ErrNotExist) {
			continue
		}
		if err != nil {
			return nil, fmt.Errorf("open %s: %w", path, err)
		}
		lock := syscall.Flock_t{Type: syscall.F_WRLCK, Whence: 0, Start: 0, Len: 0}
		err = syscall.FcntlFlock(file.Fd(), syscall.F_GETLK, &lock)
		closeErr := file.Close()
		if err != nil {
			return nil, fmt.Errorf("inspect %s: %w", path, err)
		}
		if closeErr != nil {
			return nil, fmt.Errorf("close %s: %w", path, closeErr)
		}
		if lock.Type != syscall.F_UNLCK {
			held = appendBounded(held, HeldLock{Path: path, PID: int(lock.Pid)})
		}
	}
	return held, nil
}

// PackageManagerLocks waits until all package-manager locks clear or ctx
// expires. The caller owns the timeout, which makes the wait policy explicit.
func PackageManagerLocks(ctx context.Context, paths []string, interval time.Duration, probe LockProbe) Result {
	if interval <= 0 {
		interval = time.Second
	}
	for {
		held, err := probe(paths)
		if err != nil {
			return Result{
				Name: CheckPackageLocks, Status: StatusUnknown,
				Summary: "could not inspect package-manager locks",
				Details: Details{Problems: []string{bounded(err.Error(), maxSummaryBytes)}},
			}
		}
		if len(held) == 0 {
			return Result{Name: CheckPackageLocks, Status: StatusPassed, Summary: "package-manager locks are available"}
		}

		timer := time.NewTimer(interval)
		select {
		case <-ctx.Done():
			timer.Stop()
			return Result{
				Name: CheckPackageLocks, Status: StatusFailed,
				Summary: "package-manager locks did not clear before the deadline",
				Details: Details{Locks: held},
			}
		case <-timer.C:
		}
	}
}

// DPKGAudit verifies that dpkg does not report partially installed packages.
func DPKGAudit(ctx context.Context, runner Runner) Result {
	out, err := runner.Run(ctx, "dpkg", "--audit")
	problems := evidenceLines(out)
	if err != nil || len(problems) > 0 {
		return Result{
			Name: CheckDPKGAudit, Status: StatusFailed,
			Summary: summaryFromFailure("dpkg audit failed", out, err),
			Details: Details{Problems: problems},
		}
	}
	return Result{Name: CheckDPKGAudit, Status: StatusPassed, Summary: "dpkg audit is clean"}
}

// APTDependencies verifies apt's dependency graph without changing packages.
func APTDependencies(ctx context.Context, runner Runner) Result {
	out, err := runner.Run(ctx, "apt-get", "check")
	if err != nil {
		return Result{
			Name: CheckAPTDependencies, Status: StatusFailed,
			Summary: summaryFromFailure("apt dependency check failed", out, err),
			Details: Details{Problems: evidenceLines(out)},
		}
	}
	return Result{Name: CheckAPTDependencies, Status: StatusPassed, Summary: "apt dependencies are consistent"}
}

// PackageIndexes refreshes apt metadata in strict mode. Old apt versions that
// reject --error-on=any get one ordinary update and a warning on success.
func PackageIndexes(ctx context.Context, runner Runner) Result {
	strict := true
	out, err := runner.Run(ctx, "apt-get", "--error-on=any", "update")
	if err == nil {
		return Result{
			Name: CheckPackageIndexes, Status: StatusPassed,
			Summary: "package indexes refreshed in strict mode",
			Details: Details{StrictMode: &strict},
		}
	}
	if !strictModeUnsupported(out) {
		return Result{
			Name: CheckPackageIndexes, Status: StatusFailed,
			Summary: summaryFromFailure("package index refresh failed", out, err),
			Details: Details{Problems: evidenceLines(out), StrictMode: &strict},
		}
	}

	strict = false
	out, err = runner.Run(ctx, "apt-get", "update")
	if err != nil {
		return Result{
			Name: CheckPackageIndexes, Status: StatusFailed,
			Summary: summaryFromFailure("package index refresh failed", out, err),
			Details: Details{Problems: evidenceLines(out), StrictMode: &strict},
		}
	}
	return Result{
		Name: CheckPackageIndexes, Status: StatusWarning,
		Summary: "package indexes refreshed without strict error reporting",
		Details: Details{StrictMode: &strict},
	}
}

func strictModeUnsupported(output string) bool {
	lower := strings.ToLower(output)
	return strings.Contains(lower, "--error-on") &&
		(strings.Contains(lower, "not understood") || strings.Contains(lower, "unknown option"))
}

// FailedServices snapshots systemd units in failed state. Existing failures
// are a warning, not a blocker, because this result is used as the pre-upgrade
// baseline.
func FailedServices(ctx context.Context, runner Runner) Result {
	out, err := runner.Run(ctx, "systemctl", "--failed", "--no-legend", "--plain", "--no-pager")
	if err != nil {
		return Result{
			Name: CheckFailedServices, Status: StatusUnknown,
			Summary: summaryFromFailure("could not inspect failed services", out, err),
			Details: Details{Problems: evidenceLines(out)},
		}
	}
	services := parseFailedServices(out)
	if len(services) > 0 {
		return Result{
			Name: CheckFailedServices, Status: StatusWarning,
			Summary: fmt.Sprintf("%d service(s) already failed", len(services)),
			Details: Details{Services: services},
		}
	}
	return Result{Name: CheckFailedServices, Status: StatusPassed, Summary: "no failed services"}
}

// FailedServicesAfter compares the post-upgrade failed-unit snapshot with the
// baseline. Only newly failed services make the host unhealthy.
func FailedServicesAfter(ctx context.Context, runner Runner, baseline []string) Result {
	current := FailedServices(ctx, runner)
	if current.Status == StatusUnknown {
		return current
	}

	baselineSet := make(map[string]bool, len(baseline))
	for _, service := range baseline {
		baselineSet[service] = true
	}
	var newlyFailed, stillFailed []string
	for _, service := range current.Details.Services {
		if baselineSet[service] {
			stillFailed = appendBounded(stillFailed, service)
		} else {
			newlyFailed = appendBounded(newlyFailed, service)
		}
	}
	result := Result{
		Name:    CheckFailedServices,
		Details: Details{NewServices: newlyFailed, ExistingServices: stillFailed},
	}
	switch {
	case len(newlyFailed) > 0:
		result.Status = StatusFailed
		result.Summary = fmt.Sprintf("%d service(s) newly failed", len(newlyFailed))
	case len(stillFailed) > 0:
		result.Status = StatusWarning
		result.Summary = fmt.Sprintf("%d pre-existing service failure(s) remain", len(stillFailed))
	default:
		result.Status = StatusPassed
		result.Summary = "no newly failed services"
	}
	return result
}

// RebootRequired records the reboot signal without treating it as a failure.
func RebootRequired(required bool) Result {
	if required {
		return Result{
			Name: CheckRebootRequired, Status: StatusWarning,
			Summary: "a reboot is required",
			Details: Details{Required: &required},
		}
	}
	return Result{
		Name: CheckRebootRequired, Status: StatusPassed,
		Summary: "no reboot is required",
		Details: Details{Required: &required},
	}
}

func parseFailedServices(output string) []string {
	seen := make(map[string]bool)
	var services []string
	for _, line := range strings.Split(output, "\n") {
		fields := strings.Fields(line)
		if len(fields) == 0 || seen[fields[0]] {
			continue
		}
		seen[fields[0]] = true
		services = appendBounded(services, fields[0])
	}
	sort.Strings(services)
	return services
}

func summaryFromFailure(fallback, output string, err error) string {
	if lines := evidenceLines(output); len(lines) > 0 {
		return bounded(lines[0], maxSummaryBytes)
	}
	if err != nil {
		return bounded(fallback+": "+err.Error(), maxSummaryBytes)
	}
	return fallback
}

func evidenceLines(output string) []string {
	var lines []string
	for _, line := range strings.Split(output, "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		lines = appendBounded(lines, bounded(line, maxSummaryBytes))
	}
	return lines
}

func bounded(s string, limit int) string {
	s = strings.ToValidUTF8(s, "?")
	if len(s) <= limit {
		return s
	}
	// The result, ellipsis included, must stay within limit bytes: the server
	// rejects any health-check summary or evidence line longer than this exact
	// limit (backend schemas.py, HealthCheckIn.summary / EvidenceLine), which
	// would 422 the whole job result.
	s = s[:limit-len("...")]
	for !utf8.ValidString(s) {
		s = s[:len(s)-1]
	}
	return s + "..."
}

func appendBounded[T any](items []T, item T) []T {
	if len(items) >= maxEvidenceItems {
		return items
	}
	return append(items, item)
}
