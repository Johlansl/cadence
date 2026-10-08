package upgrade

import (
	"fmt"
	"net/url"
	"runtime"
	"strings"
)

// Artifacts is the deterministic download set for one target version,
// derived locally from the agent's own ServerURL plus version and platform.
// The job contributes the version string only: no host, port, path or
// filename ever comes from params.
type Artifacts struct {
	Binary  string // cadence-agent-<os>-<arch>
	SHA256  string // Binary + ".sha256"
	MiniSig string // Binary + ".minisig"
}

// supportedPlatforms is exactly what the publisher (PR2) emits. No second
// choice, no best effort: anything else is refused.
func supportedPlatform(goos, goarch string) bool {
	return goos == "linux" && (goarch == "amd64" || goarch == "arm64")
}

// ArtifactURLs builds the artifact set for target on goos/goarch served
// from the site behind serverURL.
//
// Topology (proven by Caddyfile + the enrollment response): the agent API
// lives on https://<site>:<agent-port> behind an mTLS listener that 404s
// anything but the API, while the published assets are served from
// https://<site>/agent/... on the plain HTTPS listener. So the derivation
// keeps the configured host, drops the agent port, forces https, and builds
// the path from fixed segments only. A non-https ServerURL (dev setups),
// userinfo, an empty host, or an unsupported platform all refuse.
func ArtifactURLs(serverURL string, target Version, goos, goarch string) (Artifacts, error) {
	var zero Artifacts
	if !supportedPlatform(goos, goarch) {
		return zero, fmt.Errorf("unsupported upgrade platform %s/%s", goos, goarch)
	}
	u, err := url.Parse(serverURL)
	if err != nil || u.Scheme != "https" || u.User != nil || u.Hostname() == "" {
		return zero, fmt.Errorf("cannot derive artifact site from server URL")
	}
	host := u.Hostname()
	if strings.Contains(host, ":") {
		host = "[" + host + "]" // bare IPv6 needs brackets in the rebuilt URL
	}
	file := "cadence-agent-" + goos + "-" + goarch
	built := url.URL{
		Scheme: "https",
		Host:   host,
		Path:   "/agent/v" + target.String() + "/" + file,
	}
	binary := built.String()
	return Artifacts{
		Binary:  binary,
		SHA256:  binary + ".sha256",
		MiniSig: binary + ".minisig",
	}, nil
}

// RuntimeArtifactURLs is ArtifactURLs for the platform this process runs on.
func RuntimeArtifactURLs(serverURL string, target Version) (Artifacts, error) {
	return ArtifactURLs(serverURL, target, runtime.GOOS, runtime.GOARCH)
}
