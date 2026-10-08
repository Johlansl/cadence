package upgrade

import (
	"strings"
	"testing"
)

func TestArtifactURLs(t *testing.T) {
	target := Version{0, 15, 0}
	cases := []struct {
		name      string
		serverURL string
		goos      string
		goarch    string
		want      string
	}{
		{
			"amd64 via agent port",
			"https://cadence.lan:8443", "linux", "amd64",
			"https://cadence.lan/agent/v0.15.0/cadence-agent-linux-amd64",
		},
		{
			"arm64 via agent port",
			"https://cadence.lan:8443", "linux", "arm64",
			"https://cadence.lan/agent/v0.15.0/cadence-agent-linux-arm64",
		},
		{
			"no port",
			"https://cadence.lan", "linux", "amd64",
			"https://cadence.lan/agent/v0.15.0/cadence-agent-linux-amd64",
		},
		{
			"custom agent port dropped",
			"https://cadence.lan:9443", "linux", "amd64",
			"https://cadence.lan/agent/v0.15.0/cadence-agent-linux-amd64",
		},
		{
			"trailing slash tolerated",
			"https://cadence.lan:8443/", "linux", "amd64",
			"https://cadence.lan/agent/v0.15.0/cadence-agent-linux-amd64",
		},
		{
			"server path query fragment ignored",
			"https://cadence.lan:8443/prefix/api?x=1#f", "linux", "amd64",
			"https://cadence.lan/agent/v0.15.0/cadence-agent-linux-amd64",
		},
		{
			"ipv4",
			"https://10.0.0.5:8443", "linux", "arm64",
			"https://10.0.0.5/agent/v0.15.0/cadence-agent-linux-arm64",
		},
		{
			"ipv6 keeps brackets",
			"https://[fd00::1]:8443", "linux", "amd64",
			"https://[fd00::1]/agent/v0.15.0/cadence-agent-linux-amd64",
		},
	}
	for _, c := range cases {
		got, err := ArtifactURLs(c.serverURL, target, c.goos, c.goarch)
		if err != nil {
			t.Errorf("%s: error: %v", c.name, err)
			continue
		}
		if got.Binary != c.want {
			t.Errorf("%s: binary = %q, want %q", c.name, got.Binary, c.want)
		}
		if got.SHA256 != c.want+".sha256" {
			t.Errorf("%s: sha256 = %q", c.name, got.SHA256)
		}
		if got.MiniSig != c.want+".minisig" {
			t.Errorf("%s: minisig = %q", c.name, got.MiniSig)
		}
	}
}

func TestArtifactURLsRejects(t *testing.T) {
	target := Version{0, 15, 0}
	urls := []string{
		"",
		"not-a-url",
		"http://cadence.lan:8443", // dev scheme: no https listener serves assets
		"ftp://cadence.lan/agent",
		"https://user@cadence.lan:8443", // userinfo never survives
		"https:///no-host",
		"https://",
	}
	for _, u := range urls {
		if got, err := ArtifactURLs(u, target, "linux", "amd64"); err == nil {
			t.Errorf("ArtifactURLs(%q) = %v, want error", u, got)
		}
	}
	platforms := [][2]string{
		{"darwin", "amd64"},
		{"windows", "amd64"},
		{"linux", "386"},
		{"linux", "riscv64"},
		{"", ""},
		{"linux", "../amd64"}, // no traversal through the platform slot
	}
	for _, p := range platforms {
		if got, err := ArtifactURLs("https://cadence.lan:8443", target, p[0], p[1]); err == nil {
			t.Errorf("ArtifactURLs(%s/%s) = %v, want error", p[0], p[1], got)
		}
	}
}

func TestArtifactURLsOnlyVersionEntersPath(t *testing.T) {
	// The builder takes a parsed canonical Version, so only its digits can
	// reach the path: no host or path fragment from params can leak in
	// (params cannot even name one), and no second spelling exists.
	got, err := ArtifactURLs("https://cadence.lan:8443", Version{1, 2, 3}, "linux", "amd64")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(got.Binary, "https://cadence.lan/agent/v1.2.3/cadence-agent-linux-amd64") {
		t.Fatalf("binary = %q", got.Binary)
	}
}

func TestRuntimeArtifactURLsUsesLocalPlatform(t *testing.T) {
	got, err := RuntimeArtifactURLs("https://cadence.lan:8443", Version{0, 15, 0})
	if err != nil {
		t.Skipf("local platform unsupported here: %v", err)
	}
	if !strings.HasSuffix(got.Binary, "cadence-agent-linux-amd64") &&
		!strings.HasSuffix(got.Binary, "cadence-agent-linux-arm64") {
		t.Errorf("unexpected runtime binary %q", got.Binary)
	}
}
