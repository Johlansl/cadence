package upgrade

import (
	"bytes"
	"context"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"debug/elf"
	"encoding/hex"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

const (
	// Production install site. The production call path always passes
	// these two constants; tests redirect through Installer.Dir only via
	// the in-package harness, never through job params or runtime config.
	InstallDir  = "/usr/bin"
	InstallName = "cadence-agent"
	// PrevSuffix keeps one recovery generation: <name>.prev.
	PrevSuffix = ".prev"

	// maxBinaryBytes caps one artifact download: 64 MiB, far above the
	// real ~15 MB agent, explicitly bounded so a compromised server
	// cannot fill the disk with an endless stream.
	maxBinaryBytes = 64 << 20
	// maxMetaBytes caps a .sha256 / .minisig download (real ones are a
	// few hundred bytes).
	maxMetaBytes = 4096
	// maxVersionBytes caps the -version subprocess output.
	maxVersionBytes = 4096
	// localMaxDuration bounds the ENTIRE pre-commit attempt, from job
	// acceptance (Run entry, all local gates already passed) to the
	// atomic rename: downloads, verification, ELF, -version probe, .prev
	// staging. It is the local half of the distributed invariant: the
	// server cannot mark the job proof-timed-out before
	// CADENCE_UPGRADE_PROOF_TIMEOUT_SECONDS (minimum enforced
	// server-side, default 600s), so a 300s local abort always lands
	// first with margin to spare, and a failed job can never be
	// followed by a rename. A binary property of V1, never job input:
	// neither params nor the server can extend it.
	//
	// Budget (LAN reality, generous): 15 MB over a poor 1 Mb/s link is
	// ~120s; DNS+connect+TLS ~10s; SHA-256/Minisign/ELF over the bytes
	// ~1s; -version probe capped at 30s; .prev/rename/fsync ~1s. Worst
	// realistic total ~165s; 300s is ~2x headroom, still half the
	// default server timeout.
	localMaxDuration = 300 * time.Second
	// versionExecTimeout bounds the post-verification -version probe,
	// which answers instantly on a healthy binary. The global deadline
	// above wins if it fires first.
	versionExecTimeout = 30 * time.Second
	// responseHeaderTimeout fails a stalled header wait fast instead of
	// burning the whole global deadline on it. The global context still
	// aborts every request regardless.
	responseHeaderTimeout = 30 * time.Second
)

// Pre-commit failure categories for the result callback. Coarse on
// purpose; the log line carries the detail. Nothing here is ever sent
// after the rename commit: post-commit the job is PR4's to judge.
const (
	CategoryDownloadFailed     = "upgrade_download_failed"
	CategoryVerificationFailed = "upgrade_verification_failed"
	CategoryInstallFailed      = "upgrade_install_failed"
)

// clock abstracts wall time so suspend scenarios stay testable without
// suspending the test machine. Production passes nil (system clock).
type clock interface {
	Now() time.Time
}

// systemClock is the production clock: the real wall time.
type systemClock struct{}

func (systemClock) Now() time.Time { return time.Now() }

// maxBackwardSkew tolerates ordinary clock corrections (stepped NTP lands
// seconds back at most) while refusing gross incoherence. A tolerated
// backward step can stretch the wall window by under 60s, still inside
// the 120s server-side margin; anything beyond refuses outright.
const maxBackwardSkew = 60 * time.Second

// wallExceeded reports whether the wall-clock window starting at
// startUnixNano has overrun maxLocal by wallNowUnixNano. Pure wall time
// (UnixNano carries no monotonic reading): a host suspended mid-upgrade
// resumes with its wall window consumed, so suspend can never stretch a
// pre-commit attempt. Forward jumps fail closed; backward jumps beyond
// maxBackwardSkew are incoherent clocks and fail closed too.
func wallExceeded(wallNowUnixNano, wallStartUnixNano int64, maxLocal time.Duration) bool {
	elapsed := wallNowUnixNano - wallStartUnixNano
	if elapsed < -int64(maxBackwardSkew) {
		return true
	}
	return elapsed > maxLocal.Nanoseconds()
}

func (in *Installer) now() time.Time {
	if in.clock != nil {
		return in.clock.Now()
	}
	return systemClock{}.Now()
}

// Error is a categorized upgrade failure for the result callback.
type Error struct {
	Category string
	Err      error
}

// Error implements error.
func (e *Error) Error() string { return e.Category + ": " + e.Err.Error() }

// Unwrap returns the underlying cause.
func (e *Error) Unwrap() error { return e.Err }

func downloadErr(err error) *Error {
	return &Error{Category: CategoryDownloadFailed, Err: err}
}

func verifyErr(err error) *Error {
	return &Error{Category: CategoryVerificationFailed, Err: err}
}

func installErr(err error) *Error {
	return &Error{Category: CategoryInstallFailed, Err: err}
}

// Installer performs one verified in-place upgrade of the canonical agent
// binary. It writes only inside Dir (InstallDir in production): the new
// binary temp file, the .prev recovery generation, and the final rename
// target. It holds no credential, config, or unit paths and cannot name
// them: the allowlist is structural.
type Installer struct {
	// Client fetches artifacts. Production uses DownloadClient (TLS 1.2+,
	// no redirects); tests inject an httptest-backed client.
	Client *http.Client
	// Dir and Name locate the install target (InstallDir/InstallName in
	// production; a temp dir in tests).
	Dir  string
	Name string
	// MaxBinaryBytes overrides maxBinaryBytes when positive (tests).
	MaxBinaryBytes int64
	// versionProbeTimeout overrides versionExecTimeout when positive.
	// In-package tests only; production always uses the constant.
	versionProbeTimeout time.Duration
	// localDeadline overrides localMaxDuration when positive.
	// In-package tests only: no job, server, or config input can reach
	// it in production, so nothing remote can stretch the pre-commit
	// window.
	localDeadline time.Duration
	// beforeRename, when set, runs immediately before the commit gate.
	// In-package tests only: lets a deadline expire deterministically
	// inside the pre-rename window (e.g. by sleeping past it).
	beforeRename func()
	// clock supplies wall time for the suspend-proof commit gate.
	// Nil means the system clock; in-package tests inject a fake.
	clock clock

	// Fault injection for crash-boundary tests (in-package only, never a
	// production flag): a non-nil fault aborts Run with an error at that
	// point. faultAfterRename is swallowed by design: post-commit
	// failures must never turn into a failed result.
	faultBeforePrev  error
	faultAfterPrev   error
	faultAfterRename error
}

// DownloadClient builds the smallest network surface for artifact fetches:
// TLS 1.2+, the Cadence server CA when the agent has one (caFile, the same
// root that signs the :443 assets listener) else the system roots like the
// legacy agent API client, and zero redirects: a 3xx is an error, so a
// compromised server cannot turn a deterministic URL into an arbitrary
// fetch. No client certificate: the assets listener does not ask for one.
func DownloadClient(caFile string) (*http.Client, error) {
	var roots *x509.CertPool
	if caFile != "" {
		pem, err := os.ReadFile(caFile)
		if err != nil {
			return nil, fmt.Errorf("reading server CA: %w", err)
		}
		roots = x509.NewCertPool()
		if !roots.AppendCertsFromPEM(pem) {
			return nil, fmt.Errorf("server CA file contains no certificate")
		}
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.TLSClientConfig = &tls.Config{
		RootCAs:    roots,
		MinVersion: tls.VersionTLS12,
	}
	// Fail a stalled header wait fast. (DNS/connect and the TLS
	// handshake keep the default transport's 30s/10s bounds; every
	// request additionally dies with the caller's context.)
	transport.ResponseHeaderTimeout = responseHeaderTimeout
	return &http.Client{
		Transport: transport,
		CheckRedirect: func(_ *http.Request, _ []*http.Request) error {
			return fmt.Errorf("redirects are refused for upgrade artifacts")
		},
	}, nil
}

// Run downloads the deterministic artifact set, verifies it fully, and
// atomically replaces Dir/Name. urls must come from ArtifactURLs, target
// from ParseTarget; goos/goarch name the expected platform (the runtime
// values in production).
//
// Verification order is load-bearing: nothing executes before the Minisign
// signature over the exact downloaded bytes verifies (checksum first as a
// corruption screen, then signature, then ELF/arch, and only then a
// -version probe of the temp file).
//
// Time is load-bearing too: the whole pre-commit attempt runs under a
// single local deadline (localMaxDuration from Run entry). A fired
// deadline aborts HTTP, kills the probe, and gates .prev/rename, so the
// canonical binary is provably untouched; the caller submits failed as
// for any pre-commit error. The deadline is dual-clock: the monotonic
// context (cancels work) plus a wall-clock twin (UnixNano, no monotonic
// reading) that keeps running across a host suspend, so a resumed
// attempt finds its window consumed and can never rename late.
//

// Returns installed=true only after the atomic rename commit. The caller
// must then submit NOTHING: no succeeded, and crucially no failed even if
// later cosmetic cleanup errs. PR4 (or its timeout) is the only judge from
// there. Any error with installed=false happened before the commit and the
// caller submits failed with the carried category.
func (in *Installer) Run(
	ctx context.Context, urls Artifacts, target Version, goos, goarch string,
) (installed bool, err error) {
	maxBin := in.MaxBinaryBytes
	if maxBin <= 0 {
		maxBin = maxBinaryBytes
	}
	maxLocal := in.localDeadline
	if maxLocal <= 0 {
		maxLocal = localMaxDuration
	}
	fileBase := "cadence-agent-" + goos + "-" + goarch
	canonical := filepath.Join(in.Dir, in.Name)

	// The global pre-commit deadline starts here, at job acceptance.
	// Every download, the -version probe, and the .prev/rename gates
	// below share it: once it fires, HTTP aborts, the subprocess dies,
	// and no path can reach the rename. Post-commit code uses no
	// context at all: the deadline has no authority past the rename.
	ctx, cancel := context.WithTimeout(ctx, maxLocal)
	defer cancel()
	// Wall-clock twin of the monotonic deadline above: UnixNano strips
	// the monotonic reading, so unlike ctx this window keeps running
	// across a host suspend. Both must allow the commit.
	wallStartUnixNano := in.now().UTC().UnixNano()

	// 1. Fetch the binary into a same-filesystem temp file (0600,
	// unguessable name, exclusive create). Size is enforced on the wire,
	// never from Content-Length alone. Owner-exec is added now (even root
	// cannot exec a file with no x bit at all); the temp stays
	// owner-only until the 0755 finalize below, and the defer removes it
	// on every non-commit path.
	binPath, binSize, err := in.fetchFile(ctx, urls.Binary, maxBin)
	if err != nil {
		return false, err
	}
	committed := false
	defer func() {
		if !committed {
			_ = os.Remove(binPath)
		}
	}()
	if err := os.Chmod(binPath, 0o700); err != nil {
		return false, installErr(fmt.Errorf("chmod download: %w", err))
	}
	if binSize == 0 {
		return false, downloadErr(fmt.Errorf("empty binary download"))
	}

	// 2-3. Fetch and strictly parse the .sha256 sidecar (anti-corruption
	// only, never trust): exactly "<64 hex>  <filename>\n" naming this
	// binary, nothing else.
	meta, err := in.fetchBytes(ctx, urls.SHA256, maxMetaBytes)
	if err != nil {
		return false, err
	}
	wantHash, err := parseSHA256File(meta, fileBase)
	if err != nil {
		return false, verifyErr(err)
	}

	// 4-5. Recompute SHA-256 over the bytes on disk and compare.
	data, err := os.ReadFile(binPath)
	if err != nil {
		return false, installErr(fmt.Errorf("reading downloaded binary: %w", err))
	}
	if int64(len(data)) != binSize {
		return false, verifyErr(fmt.Errorf("downloaded binary changed on disk"))
	}
	sum := sha256.Sum256(data)
	if !bytes.Equal(sum[:], wantHash) {
		return false, verifyErr(fmt.Errorf("checksum mismatch"))
	}

	// 6-7. Minisign over the exact bytes, embedded root of trust: message
	// signature, key ID binding, and the trusted comment's global
	// signature. NOTHING executes before this passes.
	sigText, err := in.fetchBytes(ctx, urls.MiniSig, maxMetaBytes)
	if err != nil {
		return false, err
	}
	if _, err := verifySignature(data, sigText); err != nil {
		return false, verifyErr(err)
	}

	// 8. Local platform proof: a deterministic name is not enough against
	// a compromised server. Valid ELF, 64-bit, expected machine.
	if err := checkELF(data, goarch); err != nil {
		return false, verifyErr(err)
	}

	// Disk precheck: best effort (it cannot close the ENOSPC race; the
	// hard guarantee is structural: every write below targets temp paths
	// and errors leave the canonical binary untouched).
	if avail, ok := freeBytes(in.Dir); ok && avail < uint64(2*binSize) {
		return false, installErr(fmt.Errorf("insufficient disk space for upgrade"))
	}

	// 9. FIRST EXECUTION POINT. Only reachable with checksum, signature
	// and ELF all valid. Exactly "-version", empty environment, bounded
	// output, killed on timeout; stdout must equal the canonical target.
	probeTimeout := versionExecTimeout
	if in.versionProbeTimeout > 0 {
		probeTimeout = in.versionProbeTimeout
	}
	out, err := probeVersion(ctx, binPath, probeTimeout)
	if err != nil {
		return false, verifyErr(err)
	}
	if string(out) != target.String()+"\n" {
		return false, verifyErr(fmt.Errorf("downloaded binary reports %q, want %q",
			strings.TrimSpace(string(out)), target.String()))
	}

	if in.faultBeforePrev != nil {
		return false, installErr(in.faultBeforePrev) // crash point A
	}

	// Deadline gate: an expired global context abandons before touching
	// .prev or the canonical path. Simply abandon: no cleanup, no
	// rollback; the canonical binary was never at risk yet.
	if err := ctx.Err(); err != nil {
		return false, installErr(fmt.Errorf("local upgrade deadline exceeded: %w", err))
	}
	if wallExceeded(in.now().UTC().UnixNano(), wallStartUnixNano, maxLocal) {
		return false, installErr(fmt.Errorf("local upgrade wall-clock deadline exceeded"))
	}

	// 10. The canonical binary must exist and must not be a symlink: link
	// a symlink into .prev and recovery inherits the ambiguity.
	if err := checkCanonical(canonical); err != nil {
		return false, installErr(err)
	}

	// 11. Refresh the single .prev generation by hardlink (exact bytes, no
	// copy) staged under a temp name so the .prev update itself is atomic.
	// Only now: the new artifact is fully verified.
	if err := refreshPrev(canonical); err != nil {
		return false, installErr(err)
	}

	if in.faultAfterPrev != nil {
		return false, installErr(in.faultAfterPrev) // crash point B
	}

	// Commit gate: the last check before the rename. If the deadline
	// fired anywhere above (a stalled probe, a slow disk), abandon with
	// the canonical binary intact; .prev may already be refreshed and
	// stays coherent (it holds the still-current binary). The deadline
	// has no authority past the rename below.
	if in.beforeRename != nil {
		in.beforeRename()
	}
	if err := ctx.Err(); err != nil {
		return false, installErr(fmt.Errorf("local upgrade deadline exceeded: %w", err))
	}
	if wallExceeded(in.now().UTC().UnixNano(), wallStartUnixNano, maxLocal) {
		return false, installErr(fmt.Errorf("local upgrade wall-clock deadline exceeded"))
	}

	// 12. Finalize and COMMIT: 0755, fsync the file, atomic rename onto
	// the canonical path, fsync the directory. The canonical file is
	// never truncated or written in place; this process keeps running
	// from its open inode.
	final, err := os.OpenFile(binPath, os.O_RDWR, 0o600)
	if err != nil {
		return false, installErr(fmt.Errorf("reopening download: %w", err))
	}
	if err := final.Chmod(0o755); err != nil {
		_ = final.Close()
		return false, installErr(fmt.Errorf("chmod download: %w", err))
	}
	if err := final.Sync(); err != nil {
		_ = final.Close()
		return false, installErr(fmt.Errorf("fsync download: %w", err))
	}
	_ = final.Close()
	if err := os.Rename(binPath, canonical); err != nil {
		return false, installErr(fmt.Errorf("atomic rename: %w", err))
	}
	committed = true
	syncDir(in.Dir)         // durability best effort; the rename already committed
	_ = in.faultAfterRename // crash point C: post-commit faults are swallowed

	return true, nil
}

// fetchBytes GETs a small metadata URL into memory. Exactly 200, https,
// host present, at most max bytes; anything else is a download failure.
func (in *Installer) fetchBytes(ctx context.Context, rawURL string, max int64) ([]byte, error) {
	if err := checkArtifactURL(rawURL); err != nil {
		return nil, downloadErr(err)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
	if err != nil {
		return nil, downloadErr(fmt.Errorf("building request: %w", err))
	}
	resp, err := in.Client.Do(req)
	if err != nil {
		return nil, downloadErr(fmt.Errorf("fetching %s: %w", redactURL(rawURL), err))
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		return nil, downloadErr(fmt.Errorf("fetching %s: server returned %s",
			redactURL(rawURL), resp.Status))
	}
	data, err := io.ReadAll(io.LimitReader(resp.Body, max+1))
	if err != nil {
		return nil, downloadErr(fmt.Errorf("reading %s: %w", redactURL(rawURL), err))
	}
	if int64(len(data)) > max {
		return nil, downloadErr(fmt.Errorf("%s exceeds %d bytes", redactURL(rawURL), max))
	}
	if len(data) == 0 {
		return nil, downloadErr(fmt.Errorf("%s is empty", redactURL(rawURL)))
	}
	return data, nil
}

// fetchFile GETs the binary URL into a same-directory temp file and returns
// its path and size. Same status/scheme rules as fetchBytes; the byte cap
// is enforced while streaming.
func (in *Installer) fetchFile(ctx context.Context, rawURL string, max int64) (string, int64, error) {
	if err := checkArtifactURL(rawURL); err != nil {
		return "", 0, downloadErr(err)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
	if err != nil {
		return "", 0, downloadErr(fmt.Errorf("building request: %w", err))
	}
	resp, err := in.Client.Do(req)
	if err != nil {
		return "", 0, downloadErr(fmt.Errorf("fetching %s: %w", redactURL(rawURL), err))
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		return "", 0, downloadErr(fmt.Errorf("fetching %s: server returned %s",
			redactURL(rawURL), resp.Status))
	}
	tmp, err := os.CreateTemp(in.Dir, ".cadence-agent-upgrade-*")
	if err != nil {
		return "", 0, installErr(fmt.Errorf("creating temp file: %w", err))
	}
	tmpPath := tmp.Name()
	size, err := io.Copy(tmp, io.LimitReader(resp.Body, max+1))
	cerr := tmp.Close()
	if err != nil {
		_ = os.Remove(tmpPath)
		return "", 0, downloadErr(fmt.Errorf("reading %s: %w", redactURL(rawURL), err))
	}
	if cerr != nil {
		_ = os.Remove(tmpPath)
		return "", 0, installErr(fmt.Errorf("writing download: %w", cerr))
	}
	if size > max {
		_ = os.Remove(tmpPath)
		return "", 0, downloadErr(fmt.Errorf("binary exceeds %d bytes", max))
	}
	return tmpPath, size, nil
}

// checkArtifactURL is defense in depth on top of the PR5 builder: only
// https with a host is ever fetched, whatever the caller passes.
func checkArtifactURL(rawURL string) error {
	u, err := url.Parse(rawURL)
	if err != nil || u.Scheme != "https" || u.Hostname() == "" || u.User != nil {
		return fmt.Errorf("refusing non-https artifact URL")
	}
	return nil
}

// redactURL trims a URL for logs: the deterministic path carries no secret,
// but shorter lines read better and can never leak query tricks.
func redactURL(rawURL string) string {
	if u, err := url.Parse(rawURL); err == nil {
		return u.Host + u.Path
	}
	return "artifact"
}

// parseSHA256File strictly parses GNU sha256sum text output for one file:
// "<64 lowercase hex><two spaces><exact filename>\n", nothing else.
func parseSHA256File(data []byte, wantName string) ([]byte, error) {
	line := strings.TrimSuffix(string(data), "\n")
	if strings.Contains(line, "\n") || strings.Contains(line, "\r") {
		return nil, fmt.Errorf("checksum file must be a single line")
	}
	hashHex, name, found := strings.Cut(line, "  ")
	if !found || name != wantName {
		return nil, fmt.Errorf("checksum file must name %q", wantName)
	}
	if len(hashHex) != 64 {
		return nil, fmt.Errorf("checksum is not a SHA-256 hex digest")
	}
	if strings.ToLower(hashHex) != hashHex {
		return nil, fmt.Errorf("checksum is not lowercase hex")
	}
	hash, err := hex.DecodeString(hashHex)
	if err != nil {
		return nil, fmt.Errorf("checksum is not hex: %v", err)
	}
	return hash, nil
}

// checkELF proves the bytes are a 64-bit ELF for the expected architecture.
// stdlib parser, no execution.
func checkELF(data []byte, goarch string) error {
	var wantMachine elf.Machine
	switch goarch {
	case "amd64":
		wantMachine = elf.EM_X86_64
	case "arm64":
		wantMachine = elf.EM_AARCH64
	default:
		return fmt.Errorf("unsupported architecture %q", goarch)
	}
	f, err := elf.NewFile(bytes.NewReader(data))
	if err != nil {
		return fmt.Errorf("not a valid ELF: %v", err)
	}
	if f.Class != elf.ELFCLASS64 {
		return fmt.Errorf("not a 64-bit ELF")
	}
	if f.Machine != wantMachine {
		return fmt.Errorf("ELF machine %v is not %s", f.Machine, goarch)
	}
	return nil
}

// boundedWriter caps subprocess output: the runner fails instead of
// buffering a hostile stream.
type boundedWriter struct {
	buf     bytes.Buffer
	max     int
	overrun bool
}

func (w *boundedWriter) Write(p []byte) (int, error) {
	orig := len(p)
	room := w.max - w.buf.Len()
	if room <= 0 {
		w.overrun = true
		return orig, nil // discard, keep the process from blocking
	}
	if len(p) > room {
		w.overrun = true
		p = p[:room]
	}
	if _, err := w.buf.Write(p); err != nil {
		return 0, err
	}
	return orig, nil
}

// probeVersion runs the verified temp binary with exactly "-version":
// empty environment, killed on timeout, bounded stdout, stderr discarded
// (only stdout is version truth).
func probeVersion(ctx context.Context, path string, timeout time.Duration) ([]byte, error) {
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, path, "-version")
	cmd.Env = []string{}
	var stdout boundedWriter
	stdout.max = maxVersionBytes
	cmd.Stdout = &stdout
	if err := cmd.Run(); err != nil {
		if ctx.Err() == context.DeadlineExceeded {
			return nil, fmt.Errorf("-version probe timed out")
		}
		return nil, fmt.Errorf("-version probe failed: %v", err)
	}
	if stdout.overrun {
		return nil, fmt.Errorf("-version output exceeds %d bytes", maxVersionBytes)
	}
	return stdout.buf.Bytes(), nil
}

// checkCanonical requires the install target to exist as a real file, never
// a symlink (recovery must not inherit link ambiguity).
func checkCanonical(canonical string) error {
	fi, err := os.Lstat(canonical)
	if err != nil {
		return fmt.Errorf("canonical binary unreadable: %w", err)
	}
	if fi.Mode()&os.ModeSymlink != 0 {
		return fmt.Errorf("canonical binary is a symlink, refusing")
	}
	if !fi.Mode().IsRegular() {
		return fmt.Errorf("canonical binary is not a regular file")
	}
	return nil
}

// refreshPrev stages the current canonical binary as the single .prev
// generation: hardlink (exact bytes, no copy, no extra space) under a temp
// name, then atomic rename over .prev. Any stale staging file is removed
// first; leftovers after a crash are inert litter, never confused with .prev.
func refreshPrev(canonical string) error {
	dir := filepath.Dir(canonical)
	stage := filepath.Join(dir, ".cadence-agent.prev.tmp")
	_ = os.Remove(stage)
	if err := os.Link(canonical, stage); err != nil {
		return fmt.Errorf("staging .prev: %w", err)
	}
	if err := os.Rename(stage, canonical+PrevSuffix); err != nil {
		_ = os.Remove(stage)
		return fmt.Errorf("publishing .prev: %w", err)
	}
	return nil
}

// syncDir fsyncs a directory for rename durability. Best effort by design:
// callers invoke it only after the commit it protects.
func syncDir(dir string) {
	d, err := os.Open(dir)
	if err != nil {
		return
	}
	_ = d.Sync()
	_ = d.Close()
}
