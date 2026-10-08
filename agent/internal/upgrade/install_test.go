package upgrade

import (
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/ed25519"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/base64"
	"encoding/pem"
	"errors"
	"fmt"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	minisign "github.com/jedisct1/go-minisign"
)

// testKeypair is a fresh Minisign test key. Generated per test from stdlib
// ed25519; no committed secret exists anywhere in the repo.
type testKeypair struct {
	priv    minisign.PrivateKey
	pub     minisign.PublicKey
	pubText string // full .pub file content
}

func testKey(t *testing.T) testKeypair {
	t.Helper()
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	var sk minisign.PrivateKey
	sk.SignatureAlgorithm = [2]byte{'E', 'd'}
	if _, err := rand.Read(sk.KeyId[:]); err != nil {
		t.Fatal(err)
	}
	copy(sk.SecretKey[:], priv)
	pk := sk.PublicKey()
	raw := append([]byte{'E', 'd'}, sk.KeyId[:]...)
	raw = append(raw, pub...)
	pubText := fmt.Sprintf("untrusted comment: test key %X\n%s\n",
		sk.KeyId, base64.StdEncoding.EncodeToString(raw))
	if _, err := parsePubKey(pubText); err != nil {
		t.Fatal(err)
	}
	return testKeypair{priv: sk, pub: pk, pubText: pubText}
}

// sign produces .minisig file bytes over data. hashed selects the prehashed
// ("ED") variant the reference CLI emits by default.
func (k testKeypair) sign(t *testing.T, data []byte, hashed bool) []byte {
	t.Helper()
	sig, err := k.priv.Sign(data, minisign.SignOptions{
		TrustedComment: "test fixture",
		Hashed:         hashed,
	})
	if err != nil {
		t.Fatal(err)
	}
	return sig.Encode()
}

// fakeMain is built (with ldflags behavior switches) into the stand-in
// "new agent" binaries the installer verifies. One source, no network, no
// cgo: hermetic under GOPROXY=off.
const fakeMain = `package main

import (
	"fmt"
	"os"
	"strconv"
	"time"
)

var (
	fakeVersion = "9.9.9"
	fakeTrace   = ""
	fakeSleep   = ""
	fakeExit    = ""
)

func main() {
	if fakeTrace != "" {
		f, _ := os.OpenFile(fakeTrace, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
		if f != nil {
			_, _ = f.WriteString("exec\n")
			_ = f.Close()
		}
	}
	if fakeExit != "" {
		n, _ := strconv.Atoi(fakeExit)
		os.Exit(n)
	}
	if fakeSleep != "" {
		s, _ := strconv.Atoi(fakeSleep)
		time.Sleep(time.Duration(s) * time.Second)
	}
	if len(os.Args) == 2 && os.Args[1] == "-version" {
		fmt.Println(fakeVersion)
		return
	}
	os.Exit(3)
}
`

type fakeOpts struct {
	version string
	trace   string
	sleep   string
	exit    string
	goarch  string // "" = native
}

func buildFake(t *testing.T, opts fakeOpts) string {
	t.Helper()
	dir := t.TempDir()
	src := filepath.Join(dir, "fake.go")
	if err := os.WriteFile(src, []byte(fakeMain), 0o644); err != nil {
		t.Fatal(err)
	}
	out := filepath.Join(dir, "fake-agent")
	var flags []string
	set := func(name, v string) {
		if v != "" {
			flags = append(flags, "-X", "main."+name+"="+v)
		}
	}
	set("fakeVersion", opts.version)
	set("fakeTrace", opts.trace)
	set("fakeSleep", opts.sleep)
	set("fakeExit", opts.exit)
	args := []string{"build", "-o", out}
	if len(flags) > 0 {
		args = append(args, "-ldflags", strings.Join(flags, " "))
	}
	args = append(args, src)
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	cmd := exec.CommandContext(ctx, "go", args...)
	cmd.Dir = dir
	cmd.Env = append(os.Environ(), "CGO_ENABLED=0", "GOPROXY=off")
	if opts.goarch != "" {
		cmd.Env = append(cmd.Env, "GOARCH="+opts.goarch)
	}
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("building fake agent: %v\n%s", err, out)
	}
	return out
}

// fixture wires one installer run: install dir with an old binary, a TLS
// artifact server, and a trusted test key. The URLs are assembled by hand
// (the test server needs its ephemeral port, which the PR5 builder drops
// by design); URL derivation itself is covered by the artifact tests.
type fixture struct {
	dir    string
	old    []byte
	server *httptest.Server
	urls   Artifacts
	target Version
	key    testKeypair
	in     *Installer
}

func newFixture(t *testing.T, old []byte, files map[string][]byte, target Version) *fixture {
	t.Helper()
	dir := t.TempDir()
	oldPath := filepath.Join(dir, "cadence-agent")
	if old != nil {
		if err := os.WriteFile(oldPath, old, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	mux := http.NewServeMux()
	for path, data := range files {
		mux.HandleFunc(path, func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Length", fmt.Sprint(len(data)))
			_, _ = w.Write(data)
		})
	}
	srv := httptest.NewTLSServer(mux)
	t.Cleanup(srv.Close)
	key := testKey(t)
	swapTrustedKey(t, key.pub)
	cert := srv.Certificate()
	caFile := filepath.Join(t.TempDir(), "ca.crt")
	pemBytes := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: cert.Raw})
	if err := os.WriteFile(caFile, pemBytes, 0o644); err != nil {
		t.Fatal(err)
	}
	client, err := DownloadClient(caFile)
	if err != nil {
		t.Fatal(err)
	}
	base := srv.URL + "/agent/v" + target.String() +
		"/cadence-agent-" + runtime.GOOS + "-" + runtime.GOARCH
	return &fixture{
		dir:    dir,
		old:    old,
		server: srv,
		urls: Artifacts{
			Binary:  base,
			SHA256:  base + ".sha256",
			MiniSig: base + ".minisig",
		},
		target: target,
		key:    key,
		in:     &Installer{Client: client, Dir: dir, Name: "cadence-agent"},
	}
}

func (f *fixture) canonical(t *testing.T) []byte {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(f.dir, "cadence-agent"))
	if err != nil {
		t.Fatal(err)
	}
	return data
}

func (f *fixture) prev(t *testing.T) ([]byte, bool) {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(f.dir, "cadence-agent.prev"))
	if err != nil {
		return nil, false
	}
	return data, true
}

// requireOnlyNames asserts no temp litter survived: exactly these names.
func (f *fixture) requireOnlyNames(t *testing.T, names ...string) {
	t.Helper()
	entries, err := os.ReadDir(f.dir)
	if err != nil {
		t.Fatal(err)
	}
	var got []string
	for _, e := range entries {
		got = append(got, e.Name())
	}
	if len(got) != len(names) {
		t.Fatalf("dir holds %v, want %v", got, names)
	}
	for i := range got {
		if got[i] != names[i] {
			t.Fatalf("dir holds %v, want %v", got, names)
		}
	}
}

func requireErrorCategory(t *testing.T, err error, want string) {
	t.Helper()
	if err == nil {
		t.Fatalf("got nil error, want category %q", want)
	}
	var uerr *Error
	if !errors.As(err, &uerr) {
		t.Fatalf("error %v is not a categorized upgrade error", err)
	}
	if uerr.Category != want {
		t.Fatalf("category = %q, want %q (%v)", uerr.Category, want, err)
	}
}

func shaFile(t *testing.T, bin []byte, name string) []byte {
	t.Helper()
	sum := sha256.Sum256(bin)
	return []byte(fmt.Sprintf("%x  %s\n", sum, name))
}

func fileBase() string {
	return "cadence-agent-" + runtime.GOOS + "-" + runtime.GOARCH
}

func TestInstallHappyPath(t *testing.T) {
	target := Version{0, 16, 0}
	trace := filepath.Join(t.TempDir(), "trace")
	newBin, err := os.ReadFile(buildFake(t, fakeOpts{version: "0.16.0", trace: trace}))
	if err != nil {
		t.Fatal(err)
	}
	old := []byte("old-binary-bytes")
	key := testKey(t)
	files := map[string][]byte{
		"/agent/v0.16.0/" + fileBase():              newBin,
		"/agent/v0.16.0/" + fileBase() + ".sha256":  shaFile(t, newBin, fileBase()),
		"/agent/v0.16.0/" + fileBase() + ".minisig": key.sign(t, newBin, false),
	}
	f := newFixture(t, old, files, target)
	swapTrustedKey(t, key.pub) // newFixture made its own; trust the signer

	installed, err := f.in.Run(context.Background(), f.urls, target, runtime.GOOS, runtime.GOARCH)

	if err != nil || !installed {
		t.Fatalf("installed=%v err=%v", installed, err)
	}
	if got := f.canonical(t); !bytes.Equal(got, newBin) {
		t.Fatal("canonical binary is not the new artifact")
	}
	prev, ok := f.prev(t)
	if !ok || !bytes.Equal(prev, old) {
		t.Fatal(".prev is not the old binary")
	}
	fi, err := os.Stat(filepath.Join(f.dir, "cadence-agent"))
	if err != nil || fi.Mode().Perm() != 0o755 {
		t.Fatalf("canonical mode = %v, want 0755", fi.Mode())
	}
	// The -version probe DID execute the binary: legal, it ran only after
	// checksum, signature and ELF all passed.
	if _, err := os.Stat(trace); err != nil {
		t.Fatal("post-verification -version probe did not execute")
	}
	f.requireOnlyNames(t, "cadence-agent", "cadence-agent.prev")
}

func TestInstallHappyPathPrehashed(t *testing.T) {
	target := Version{0, 16, 0}
	newBin, err := os.ReadFile(buildFake(t, fakeOpts{version: "0.16.0"}))
	if err != nil {
		t.Fatal(err)
	}
	key := testKey(t)
	files := map[string][]byte{
		"/agent/v0.16.0/" + fileBase():              newBin,
		"/agent/v0.16.0/" + fileBase() + ".sha256":  shaFile(t, newBin, fileBase()),
		"/agent/v0.16.0/" + fileBase() + ".minisig": key.sign(t, newBin, true),
	}
	f := newFixture(t, []byte("old"), files, target)
	swapTrustedKey(t, key.pub)

	installed, err := f.in.Run(context.Background(), f.urls, target, runtime.GOOS, runtime.GOARCH)
	if err != nil || !installed {
		t.Fatalf("prehashed install: installed=%v err=%v", installed, err)
	}
}

func TestInstallChecksumMismatchNeverExecutes(t *testing.T) {
	target := Version{0, 16, 0}
	trace := filepath.Join(t.TempDir(), "trace")
	newBin, err := os.ReadFile(buildFake(t, fakeOpts{version: "0.16.0", trace: trace}))
	if err != nil {
		t.Fatal(err)
	}
	old := []byte("old-binary-bytes")
	key := testKey(t)
	sha := shaFile(t, newBin, fileBase())
	sha[0] ^= 0x01 // corrupt one hex digit, keep the shape valid
	files := map[string][]byte{
		"/agent/v0.16.0/" + fileBase():              newBin,
		"/agent/v0.16.0/" + fileBase() + ".sha256":  sha,
		"/agent/v0.16.0/" + fileBase() + ".minisig": key.sign(t, newBin, false),
	}
	f := newFixture(t, old, files, target)
	swapTrustedKey(t, key.pub)

	installed, err := f.in.Run(context.Background(), f.urls, target, runtime.GOOS, runtime.GOARCH)

	requireErrorCategory(t, err, CategoryVerificationFailed)
	if installed {
		t.Fatal("installed despite checksum mismatch")
	}
	if _, serr := os.Stat(trace); !os.IsNotExist(serr) {
		t.Fatal("unverified binary was executed")
	}
	if got := f.canonical(t); !bytes.Equal(got, old) {
		t.Fatal("canonical binary changed")
	}
	f.requireOnlyNames(t, "cadence-agent")
}

func TestInstallSignatureInvalidNeverExecutes(t *testing.T) {
	target := Version{0, 16, 0}
	trace := filepath.Join(t.TempDir(), "trace")
	newBin, err := os.ReadFile(buildFake(t, fakeOpts{version: "0.16.0", trace: trace}))
	if err != nil {
		t.Fatal(err)
	}
	old := []byte("old-binary-bytes")
	key := testKey(t)
	sig := key.sign(t, newBin, false)
	sig[len(sig)-5] ^= 0xFF // break the global signature
	files := map[string][]byte{
		"/agent/v0.16.0/" + fileBase():              newBin,
		"/agent/v0.16.0/" + fileBase() + ".sha256":  shaFile(t, newBin, fileBase()),
		"/agent/v0.16.0/" + fileBase() + ".minisig": sig,
	}
	f := newFixture(t, old, files, target)
	swapTrustedKey(t, key.pub)

	installed, err := f.in.Run(context.Background(), f.urls, target, runtime.GOOS, runtime.GOARCH)

	requireErrorCategory(t, err, CategoryVerificationFailed)
	if installed {
		t.Fatal("installed despite bad signature")
	}
	if _, serr := os.Stat(trace); !os.IsNotExist(serr) {
		t.Fatal("unsigned binary was executed")
	}
	if got := f.canonical(t); !bytes.Equal(got, old) {
		t.Fatal("canonical binary changed")
	}
}

func TestInstallSignatureWrongKeyNeverExecutes(t *testing.T) {
	target := Version{0, 16, 0}
	trace := filepath.Join(t.TempDir(), "trace")
	newBin, err := os.ReadFile(buildFake(t, fakeOpts{version: "0.16.0", trace: trace}))
	if err != nil {
		t.Fatal(err)
	}
	signer := testKey(t) // valid signature, untrusted key
	files := map[string][]byte{
		"/agent/v0.16.0/" + fileBase():              newBin,
		"/agent/v0.16.0/" + fileBase() + ".sha256":  shaFile(t, newBin, fileBase()),
		"/agent/v0.16.0/" + fileBase() + ".minisig": signer.sign(t, newBin, false),
	}
	f := newFixture(t, []byte("old"), files, target)
	// newFixture already trusts a different fresh key: do NOT swap.

	installed, err := f.in.Run(context.Background(), f.urls, target, runtime.GOOS, runtime.GOARCH)

	requireErrorCategory(t, err, CategoryVerificationFailed)
	if installed {
		t.Fatal("installed despite untrusted key")
	}
	if _, serr := os.Stat(trace); !os.IsNotExist(serr) {
		t.Fatal("binary signed by another key was executed")
	}
}

func TestInstallSignatureMissing(t *testing.T) {
	target := Version{0, 16, 0}
	newBin, err := os.ReadFile(buildFake(t, fakeOpts{version: "0.16.0"}))
	if err != nil {
		t.Fatal(err)
	}
	files := map[string][]byte{ // no .minisig: the server 404s it
		"/agent/v0.16.0/" + fileBase():             newBin,
		"/agent/v0.16.0/" + fileBase() + ".sha256": shaFile(t, newBin, fileBase()),
	}
	f := newFixture(t, []byte("old"), files, target)

	installed, err := f.in.Run(context.Background(), f.urls, target, runtime.GOOS, runtime.GOARCH)

	requireErrorCategory(t, err, CategoryDownloadFailed)
	if installed {
		t.Fatal("installed without a signature")
	}
}

func TestInstallNonELFNeverExecutes(t *testing.T) {
	target := Version{0, 16, 0}
	trace := filepath.Join(t.TempDir(), "trace")
	script := []byte("#!/bin/sh\necho pwned >" + trace + "\n")
	old := []byte("old")
	key := testKey(t)
	files := map[string][]byte{
		"/agent/v0.16.0/" + fileBase():              script,
		"/agent/v0.16.0/" + fileBase() + ".sha256":  shaFile(t, script, fileBase()),
		"/agent/v0.16.0/" + fileBase() + ".minisig": key.sign(t, script, false),
	}
	f := newFixture(t, old, files, target)
	swapTrustedKey(t, key.pub)

	installed, err := f.in.Run(context.Background(), f.urls, target, runtime.GOOS, runtime.GOARCH)

	requireErrorCategory(t, err, CategoryVerificationFailed)
	if installed {
		t.Fatal("installed a non-ELF")
	}
	if _, serr := os.Stat(trace); !os.IsNotExist(serr) {
		t.Fatal("non-ELF payload was executed")
	}
	if got := f.canonical(t); !bytes.Equal(got, old) {
		t.Fatal("canonical binary changed")
	}
}

func TestInstallWrongArchNeverExecutes(t *testing.T) {
	other := "arm64"
	if runtime.GOARCH == "arm64" {
		other = "amd64"
	}
	target := Version{0, 16, 0}
	trace := filepath.Join(t.TempDir(), "trace")
	newBin, err := os.ReadFile(buildFake(t, fakeOpts{
		version: "0.16.0", trace: trace, goarch: other,
	}))
	if err != nil {
		t.Fatal(err)
	}
	old := []byte("old")
	key := testKey(t)
	files := map[string][]byte{
		"/agent/v0.16.0/" + fileBase():              newBin,
		"/agent/v0.16.0/" + fileBase() + ".sha256":  shaFile(t, newBin, fileBase()),
		"/agent/v0.16.0/" + fileBase() + ".minisig": key.sign(t, newBin, false),
	}
	f := newFixture(t, old, files, target)
	swapTrustedKey(t, key.pub)

	installed, err := f.in.Run(context.Background(), f.urls, target, runtime.GOOS, runtime.GOARCH)

	requireErrorCategory(t, err, CategoryVerificationFailed)
	if installed {
		t.Fatal("installed a foreign-arch binary")
	}
	if _, serr := os.Stat(trace); !os.IsNotExist(serr) {
		t.Fatal("foreign-arch binary was executed")
	}
	if got := f.canonical(t); !bytes.Equal(got, old) {
		t.Fatal("canonical binary changed")
	}
}

func TestInstallTruncatedELFRefused(t *testing.T) {
	target := Version{0, 16, 0}
	full, err := os.ReadFile(buildFake(t, fakeOpts{version: "0.16.0"}))
	if err != nil {
		t.Fatal(err)
	}
	bin := full[:16] // "\x7fELF" plus a torn header
	key := testKey(t)
	files := map[string][]byte{
		"/agent/v0.16.0/" + fileBase():              bin,
		"/agent/v0.16.0/" + fileBase() + ".sha256":  shaFile(t, bin, fileBase()),
		"/agent/v0.16.0/" + fileBase() + ".minisig": key.sign(t, bin, false),
	}
	f := newFixture(t, []byte("old"), files, target)
	swapTrustedKey(t, key.pub)

	installed, err := f.in.Run(context.Background(), f.urls, target, runtime.GOOS, runtime.GOARCH)

	requireErrorCategory(t, err, CategoryVerificationFailed)
	if installed {
		t.Fatal("installed a malformed ELF")
	}
}

func TestInstallWrongVersionRefused(t *testing.T) {
	target := Version{0, 16, 0}
	trace := filepath.Join(t.TempDir(), "trace")
	newBin, err := os.ReadFile(buildFake(t, fakeOpts{version: "9.9.9", trace: trace}))
	if err != nil {
		t.Fatal(err)
	}
	old := []byte("old")
	key := testKey(t)
	files := map[string][]byte{
		"/agent/v0.16.0/" + fileBase():              newBin,
		"/agent/v0.16.0/" + fileBase() + ".sha256":  shaFile(t, newBin, fileBase()),
		"/agent/v0.16.0/" + fileBase() + ".minisig": key.sign(t, newBin, false),
	}
	f := newFixture(t, old, files, target)
	swapTrustedKey(t, key.pub)

	installed, err := f.in.Run(context.Background(), f.urls, target, runtime.GOOS, runtime.GOARCH)

	requireErrorCategory(t, err, CategoryVerificationFailed)
	if installed {
		t.Fatal("installed a version mismatch")
	}
	// The probe ran (signature was valid) but the install refused.
	if _, serr := os.Stat(trace); serr != nil {
		t.Fatal("validly signed binary was never probed")
	}
	if got := f.canonical(t); !bytes.Equal(got, old) {
		t.Fatal("canonical binary changed")
	}
}

func TestInstallVersionTimeoutKillsProbe(t *testing.T) {
	target := Version{0, 16, 0}
	newBin, err := os.ReadFile(buildFake(t, fakeOpts{version: "0.16.0", sleep: "30"}))
	if err != nil {
		t.Fatal(err)
	}
	old := []byte("old")
	key := testKey(t)
	files := map[string][]byte{
		"/agent/v0.16.0/" + fileBase():              newBin,
		"/agent/v0.16.0/" + fileBase() + ".sha256":  shaFile(t, newBin, fileBase()),
		"/agent/v0.16.0/" + fileBase() + ".minisig": key.sign(t, newBin, false),
	}
	f := newFixture(t, old, files, target)
	swapTrustedKey(t, key.pub)
	f.in.versionProbeTimeout = 300 * time.Millisecond

	start := time.Now()
	installed, err := f.in.Run(context.Background(), f.urls, target, runtime.GOOS, runtime.GOARCH)
	if time.Since(start) > 20*time.Second {
		t.Fatal("probe was not killed on timeout")
	}

	requireErrorCategory(t, err, CategoryVerificationFailed)
	if installed {
		t.Fatal("installed after probe timeout")
	}
	if got := f.canonical(t); !bytes.Equal(got, old) {
		t.Fatal("canonical binary changed")
	}
}

func TestInstallTruncatedDownloadRefused(t *testing.T) {
	target := Version{0, 16, 0}
	newBin, err := os.ReadFile(buildFake(t, fakeOpts{version: "0.16.0"}))
	if err != nil {
		t.Fatal(err)
	}
	old := []byte("old")
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "cadence-agent"), old, 0o755); err != nil {
		t.Fatal(err)
	}
	path := "/agent/v0.16.0/" + fileBase()
	mux := http.NewServeMux()
	mux.HandleFunc(path, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Length", fmt.Sprint(len(newBin)))
		_, _ = w.Write(newBin[:len(newBin)/2]) // lie, then stop: short body
	})
	srv := httptest.NewTLSServer(mux)
	t.Cleanup(srv.Close)
	f := &fixture{dir: dir, old: old, server: srv, target: target}
	f.urls = Artifacts{Binary: srv.URL + path}
	f.in = &Installer{Client: testClientFor(t, srv), Dir: dir, Name: "cadence-agent"}

	installed, err := f.in.Run(context.Background(), f.urls, target, runtime.GOOS, runtime.GOARCH)

	requireErrorCategory(t, err, CategoryDownloadFailed)
	if installed {
		t.Fatal("installed a truncated download")
	}
	if got := f.canonical(t); !bytes.Equal(got, old) {
		t.Fatal("canonical binary changed")
	}
}

func TestInstallOversizeDownloadRefused(t *testing.T) {
	target := Version{0, 16, 0}
	newBin, err := os.ReadFile(buildFake(t, fakeOpts{version: "0.16.0"}))
	if err != nil {
		t.Fatal(err)
	}
	files := map[string][]byte{
		"/agent/v0.16.0/" + fileBase(): newBin,
	}
	f := newFixture(t, []byte("old"), files, target)
	f.in.MaxBinaryBytes = 1024 // far below any real binary

	installed, err := f.in.Run(context.Background(), f.urls, target, runtime.GOOS, runtime.GOARCH)

	requireErrorCategory(t, err, CategoryDownloadFailed)
	if installed {
		t.Fatal("installed an oversize download")
	}
	f.requireOnlyNames(t, "cadence-agent")
}

func TestInstallRedirectRefused(t *testing.T) {
	target := Version{0, 16, 0}
	mux := http.NewServeMux()
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		// Even a same-server redirect refuses: the primitive takes none.
		http.Redirect(w, r, "/elsewhere", http.StatusFound)
	})
	srv := httptest.NewTLSServer(mux)
	t.Cleanup(srv.Close)
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "cadence-agent"), []byte("old"), 0o755); err != nil {
		t.Fatal(err)
	}
	base := srv.URL + "/agent/v0.16.0/" + fileBase()
	in := &Installer{
		Client: testClientFor(t, srv),
		Dir:    dir,
		Name:   "cadence-agent",
	}
	urls := Artifacts{Binary: base, SHA256: base + ".sha256", MiniSig: base + ".minisig"}

	installed, err := in.Run(context.Background(), urls, target, runtime.GOOS, runtime.GOARCH)

	requireErrorCategory(t, err, CategoryDownloadFailed)
	if installed {
		t.Fatal("installed after a redirect")
	}
	if !strings.Contains(err.Error(), "redirect") {
		t.Fatalf("error %v does not name the redirect refusal", err)
	}
}

type failRoundTripper struct{ t *testing.T }

func (f failRoundTripper) RoundTrip(*http.Request) (*http.Response, error) {
	f.t.Fatal("non-https artifact URL was fetched")
	return nil, fmt.Errorf("unreachable")
}

func TestInstallNonHTTPSNeverFetched(t *testing.T) {
	dir := t.TempDir()
	in := &Installer{
		Client: &http.Client{Transport: failRoundTripper{t}},
		Dir:    dir,
		Name:   "cadence-agent",
	}
	urls := Artifacts{
		Binary:  "http://127.0.0.1:9/agent/v0.16.0/cadence-agent-linux-amd64",
		SHA256:  "http://127.0.0.1:9/x.sha256",
		MiniSig: "http://127.0.0.1:9/x.minisig",
	}

	installed, err := in.Run(context.Background(), urls, Version{0, 16, 0}, "linux", "amd64")

	requireErrorCategory(t, err, CategoryDownloadFailed)
	if installed {
		t.Fatal("installed over plain http")
	}
}

func TestInstallServerErrorRefused(t *testing.T) {
	target := Version{0, 16, 0}
	mux := http.NewServeMux()
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	})
	srv := httptest.NewTLSServer(mux)
	t.Cleanup(srv.Close)
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "cadence-agent"), []byte("old"), 0o755); err != nil {
		t.Fatal(err)
	}
	base := srv.URL + "/agent/v0.16.0/" + fileBase()
	in := &Installer{
		Client: testClientFor(t, srv),
		Dir:    dir,
		Name:   "cadence-agent",
	}
	urls := Artifacts{Binary: base, SHA256: base + ".sha256", MiniSig: base + ".minisig"}

	installed, err := in.Run(context.Background(), urls, target, runtime.GOOS, runtime.GOARCH)

	requireErrorCategory(t, err, CategoryDownloadFailed)
	if installed {
		t.Fatal("installed after a server error")
	}
}

func TestInstallUnusableDirRefused(t *testing.T) {
	// Temp creation must fail safely when the install dir is unusable.
	// (A mode-based unwritable dir is skipped: root bypasses it, and the
	// agent upgrades as root. These shapes fail for every uid through
	// the same error path.)
	target := Version{0, 16, 0}
	newBin, err := os.ReadFile(buildFake(t, fakeOpts{version: "0.16.0"}))
	if err != nil {
		t.Fatal(err)
	}
	files := map[string][]byte{
		"/agent/v0.16.0/" + fileBase(): newBin,
	}
	regular := filepath.Join(t.TempDir(), "not-a-dir")
	if err := os.WriteFile(regular, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	for name, dir := range map[string]string{
		"dir is a file":  regular,
		"dir is missing": filepath.Join(t.TempDir(), "missing"),
	} {
		t.Run(name, func(t *testing.T) {
			f := newFixture(t, []byte("old"), files, target)
			f.in.Dir = dir
			installed, err := f.in.Run(context.Background(), f.urls, target,
				runtime.GOOS, runtime.GOARCH)
			requireErrorCategory(t, err, CategoryInstallFailed)
			if installed {
				t.Fatal("installed into an unusable dir")
			}
		})
	}
}

func TestInstallCanonicalMissingRefused(t *testing.T) {
	target := Version{0, 16, 0}
	newBin, err := os.ReadFile(buildFake(t, fakeOpts{version: "0.16.0"}))
	if err != nil {
		t.Fatal(err)
	}
	key := testKey(t)
	files := map[string][]byte{
		"/agent/v0.16.0/" + fileBase():              newBin,
		"/agent/v0.16.0/" + fileBase() + ".sha256":  shaFile(t, newBin, fileBase()),
		"/agent/v0.16.0/" + fileBase() + ".minisig": key.sign(t, newBin, false),
	}
	f := newFixture(t, nil, files, target) // no old binary at all
	swapTrustedKey(t, key.pub)

	installed, err := f.in.Run(context.Background(), f.urls, target, runtime.GOOS, runtime.GOARCH)

	requireErrorCategory(t, err, CategoryInstallFailed)
	if installed {
		t.Fatal("installed with no canonical binary")
	}
}

func TestInstallCanonicalSymlinkRefused(t *testing.T) {
	target := Version{0, 16, 0}
	newBin, err := os.ReadFile(buildFake(t, fakeOpts{version: "0.16.0"}))
	if err != nil {
		t.Fatal(err)
	}
	key := testKey(t)
	files := map[string][]byte{
		"/agent/v0.16.0/" + fileBase():              newBin,
		"/agent/v0.16.0/" + fileBase() + ".sha256":  shaFile(t, newBin, fileBase()),
		"/agent/v0.16.0/" + fileBase() + ".minisig": key.sign(t, newBin, false),
	}
	dir := t.TempDir()
	realPath := filepath.Join(dir, "real-target")
	if err := os.WriteFile(realPath, []byte("real"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(realPath, filepath.Join(dir, "cadence-agent")); err != nil {
		t.Fatal(err)
	}
	mux := http.NewServeMux()
	for path, data := range files {
		mux.HandleFunc(path, func(w http.ResponseWriter, r *http.Request) {
			_, _ = w.Write(data)
		})
	}
	srv := httptest.NewTLSServer(mux)
	t.Cleanup(srv.Close)
	swapTrustedKey(t, key.pub)
	base := srv.URL + "/agent/v0.16.0/" + fileBase()
	in := &Installer{
		Client: testClientFor(t, srv),
		Dir:    dir,
		Name:   "cadence-agent",
	}
	urls := Artifacts{Binary: base, SHA256: base + ".sha256", MiniSig: base + ".minisig"}

	installed, err := in.Run(context.Background(), urls, target, runtime.GOOS, runtime.GOARCH)

	requireErrorCategory(t, err, CategoryInstallFailed)
	if installed {
		t.Fatal("installed over a symlinked canonical path")
	}
	if got, _ := os.ReadFile(realPath); !bytes.Equal(got, []byte("real")) {
		t.Fatal("symlink target changed")
	}
}

func TestInstallPrevRotationKeepsOneGeneration(t *testing.T) {
	v2, err := os.ReadFile(buildFake(t, fakeOpts{version: "0.16.0"}))
	if err != nil {
		t.Fatal(err)
	}
	v3, err := os.ReadFile(buildFake(t, fakeOpts{version: "0.17.0"}))
	if err != nil {
		t.Fatal(err)
	}
	old := []byte("old")
	key := testKey(t)
	files := map[string][]byte{}
	for _, tc := range []struct {
		ver string
		bin []byte
	}{{"0.16.0", v2}, {"0.17.0", v3}} {
		base := "/agent/v" + tc.ver + "/" + fileBase()
		files[base] = tc.bin
		files[base+".sha256"] = shaFile(t, tc.bin, fileBase())
		files[base+".minisig"] = key.sign(t, tc.bin, false)
	}
	f := newFixture(t, old, files, Version{0, 16, 0})
	swapTrustedKey(t, key.pub)

	ok, err := f.in.Run(context.Background(), f.urls, Version{0, 16, 0}, runtime.GOOS, runtime.GOARCH)
	if err != nil || !ok {
		t.Fatalf("first install: %v", err)
	}
	base := f.server.URL + "/agent/v0.17.0/" + fileBase()
	urls := Artifacts{Binary: base, SHA256: base + ".sha256", MiniSig: base + ".minisig"}
	ok, err = f.in.Run(context.Background(), urls, Version{0, 17, 0}, runtime.GOOS, runtime.GOARCH)
	if err != nil || !ok {
		t.Fatalf("second install: %v", err)
	}

	if got := f.canonical(t); !bytes.Equal(got, v3) {
		t.Fatal("canonical is not v3")
	}
	prev, present := f.prev(t)
	if !present || !bytes.Equal(prev, v2) {
		t.Fatal(".prev is not v2")
	}
	f.requireOnlyNames(t, "cadence-agent", "cadence-agent.prev")
}

func TestInstallCrashBeforePrevKeepsOldCanonical(t *testing.T) {
	target := Version{0, 16, 0}
	newBin, err := os.ReadFile(buildFake(t, fakeOpts{version: "0.16.0"}))
	if err != nil {
		t.Fatal(err)
	}
	old := []byte("old")
	key := testKey(t)
	files := map[string][]byte{
		"/agent/v0.16.0/" + fileBase():              newBin,
		"/agent/v0.16.0/" + fileBase() + ".sha256":  shaFile(t, newBin, fileBase()),
		"/agent/v0.16.0/" + fileBase() + ".minisig": key.sign(t, newBin, false),
	}
	f := newFixture(t, old, files, target)
	swapTrustedKey(t, key.pub)
	f.in.faultBeforePrev = fmt.Errorf("injected crash before .prev")

	installed, err := f.in.Run(context.Background(), f.urls, target, runtime.GOOS, runtime.GOARCH)

	if err == nil || installed {
		t.Fatalf("installed=%v err=%v, want pre-commit failure", installed, err)
	}
	if got := f.canonical(t); !bytes.Equal(got, old) {
		t.Fatal("canonical changed before the commit")
	}
	if _, present := f.prev(t); present {
		t.Fatal(".prev must not exist after crash point A")
	}
}

func TestInstallCrashAfterPrevKeepsOldCanonical(t *testing.T) {
	target := Version{0, 16, 0}
	newBin, err := os.ReadFile(buildFake(t, fakeOpts{version: "0.16.0"}))
	if err != nil {
		t.Fatal(err)
	}
	old := []byte("old")
	key := testKey(t)
	files := map[string][]byte{
		"/agent/v0.16.0/" + fileBase():              newBin,
		"/agent/v0.16.0/" + fileBase() + ".sha256":  shaFile(t, newBin, fileBase()),
		"/agent/v0.16.0/" + fileBase() + ".minisig": key.sign(t, newBin, false),
	}
	f := newFixture(t, old, files, target)
	swapTrustedKey(t, key.pub)
	f.in.faultAfterPrev = fmt.Errorf("injected crash after .prev")

	installed, err := f.in.Run(context.Background(), f.urls, target, runtime.GOOS, runtime.GOARCH)

	if err == nil || installed {
		t.Fatalf("installed=%v err=%v, want pre-commit failure", installed, err)
	}
	// Point B state: canonical untouched, .prev refreshed. Recovery is an
	// operator running the .prev (or reinstalling); nothing is ambiguous.
	if got := f.canonical(t); !bytes.Equal(got, old) {
		t.Fatal("canonical changed before the commit")
	}
	prev, present := f.prev(t)
	if !present || !bytes.Equal(prev, old) {
		t.Fatal(".prev is not the old binary after crash point B")
	}
}

func TestInstallCrashAfterRenameStaysInstalled(t *testing.T) {
	target := Version{0, 16, 0}
	newBin, err := os.ReadFile(buildFake(t, fakeOpts{version: "0.16.0"}))
	if err != nil {
		t.Fatal(err)
	}
	old := []byte("old")
	key := testKey(t)
	files := map[string][]byte{
		"/agent/v0.16.0/" + fileBase():              newBin,
		"/agent/v0.16.0/" + fileBase() + ".sha256":  shaFile(t, newBin, fileBase()),
		"/agent/v0.16.0/" + fileBase() + ".minisig": key.sign(t, newBin, false),
	}
	f := newFixture(t, old, files, target)
	swapTrustedKey(t, key.pub)
	f.in.faultAfterRename = fmt.Errorf("injected post-commit fault")

	installed, err := f.in.Run(context.Background(), f.urls, target, runtime.GOOS, runtime.GOARCH)

	// Point C: post-commit faults are swallowed. The caller sees a clean
	// install and submits nothing; PR4 judges the job.
	if err != nil || !installed {
		t.Fatalf("installed=%v err=%v, want clean install", installed, err)
	}
	if got := f.canonical(t); !bytes.Equal(got, newBin) {
		t.Fatal("canonical is not the new binary")
	}
}

func TestInstallLeavesCredentialsAlone(t *testing.T) {
	// A stand-in /etc/cadence with sentinel credentials. The installer
	// never receives this path; hash everything before and after a happy
	// install plus a failing one.
	etc := t.TempDir()
	sentinels := map[string]string{
		"agent.env":           "CADENCE_TOKEN=s3cr3t\n",
		"client.crt":          "cert-bytes",
		"client.key":          "key-bytes",
		"server-ca.crt":       "ca-bytes",
		"systemd/custom.unit": "unit-bytes",
	}
	for name, content := range sentinels {
		p := filepath.Join(etc, name)
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(content), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	hash := func() map[string][32]byte {
		out := map[string][32]byte{}
		for name := range sentinels {
			data, err := os.ReadFile(filepath.Join(etc, name))
			if err != nil {
				t.Fatal(err)
			}
			out[name] = sha256.Sum256(data)
		}
		return out
	}
	before := hash()

	target := Version{0, 16, 0}
	newBin, err := os.ReadFile(buildFake(t, fakeOpts{version: "0.16.0"}))
	if err != nil {
		t.Fatal(err)
	}
	key := testKey(t)
	files := map[string][]byte{
		"/agent/v0.16.0/" + fileBase():              newBin,
		"/agent/v0.16.0/" + fileBase() + ".sha256":  shaFile(t, newBin, fileBase()),
		"/agent/v0.16.0/" + fileBase() + ".minisig": key.sign(t, newBin, false),
	}
	f := newFixture(t, []byte("old"), files, target)
	swapTrustedKey(t, key.pub)
	if _, err := f.in.Run(context.Background(), f.urls, target, runtime.GOOS, runtime.GOARCH); err != nil {
		t.Fatal(err)
	}
	// And a failing run (bad checksum) against a second install dir.
	sha := shaFile(t, newBin, fileBase())
	sha[0] ^= 0xFF
	files["/agent/v0.16.0/"+fileBase()+".sha256"] = sha
	f2 := newFixture(t, []byte("old"), files, target)
	swapTrustedKey(t, key.pub)
	if _, err := f2.in.Run(context.Background(), f2.urls, target, runtime.GOOS, runtime.GOARCH); err == nil {
		t.Fatal("tampered run unexpectedly installed")
	}

	after := hash()
	for name := range sentinels {
		if before[name] != after[name] {
			t.Fatalf("credential file %q changed", name)
		}
	}
}

// testClientFor builds the production DownloadClient trusting srv's CA.
func testClientFor(t *testing.T, srv *httptest.Server) *http.Client {
	t.Helper()
	cert := srv.Certificate()
	caFile := filepath.Join(t.TempDir(), "ca.crt")
	pemBytes := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: cert.Raw})
	if err := os.WriteFile(caFile, pemBytes, 0o644); err != nil {
		t.Fatal(err)
	}
	client, err := DownloadClient(caFile)
	if err != nil {
		t.Fatal(err)
	}
	return client
}

func TestParseSHA256File(t *testing.T) {
	sum := strings.Repeat("ab", 32)
	name := "cadence-agent-linux-amd64"
	good := []string{
		sum + "  " + name + "\n",
		sum + "  " + name, // missing final newline tolerated
	}
	for _, in := range good {
		got, err := parseSHA256File([]byte(in), name)
		if err != nil {
			t.Errorf("parseSHA256File(%q): %v", in, err)
			continue
		}
		if fmt.Sprintf("%x", got) != sum {
			t.Errorf("parseSHA256File(%q) = %x", in, got)
		}
	}
	bad := map[string]string{
		"empty":            "",
		"blank":            "\n",
		"uppercase":        strings.ToUpper(sum) + "  " + name + "\n",
		"short":            sum[:63] + "  " + name + "\n",
		"long":             sum + "ab  " + name + "\n",
		"nonhex":           strings.Repeat("zz", 32) + "  " + name + "\n",
		"wrong name":       sum + "  cadence-agent-linux-arm64\n",
		"single space":     sum + " " + name + "\n",
		"binary mode star": sum + " *" + name + "\n",
		"two lines":        sum + "  " + name + "\n" + sum + "  " + name + "\n",
		"cr":               sum + "  " + name + "\r\n",
		"trailing space":   sum + "  " + name + " \n",
		"leading space":    " " + sum + "  " + name + "\n",
		"no separator":     sum + name + "\n",
	}
	for why, in := range bad {
		if _, err := parseSHA256File([]byte(in), name); err == nil {
			t.Errorf("%s: %q accepted", why, in)
		}
	}
}

func TestCheckELFRejectsUnknownArch(t *testing.T) {
	if err := checkELF([]byte("data"), "mips"); err == nil {
		t.Error("mips accepted")
	}
}

func TestDownloadClient(t *testing.T) {
	if _, err := DownloadClient(filepath.Join(t.TempDir(), "missing.crt")); err == nil {
		t.Error("missing CA accepted")
	}
	badCA := filepath.Join(t.TempDir(), "bad.crt")
	if err := os.WriteFile(badCA, []byte("not a cert"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := DownloadClient(badCA); err == nil {
		t.Error("garbage CA accepted")
	}
	// No CA file: system roots, like the legacy agent API client.
	c, err := DownloadClient("")
	if err != nil {
		t.Fatalf("system roots client: %v", err)
	}
	if c.CheckRedirect == nil {
		t.Error("system roots client allows redirects")
	}
	// A pinned CA validates its own server.
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("ok"))
	}))
	t.Cleanup(srv.Close)
	pinned := testClientFor(t, srv)
	resp, err := pinned.Get(srv.URL)
	if err != nil {
		t.Fatalf("pinned CA fetch: %v", err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %v", resp.Status)
	}
	// ... and rejects a TLS server with a genuinely different cert.
	// (Plain NewTLSServer pairs share one static test cert, so the
	// foreign server needs its own freshly minted one.)
	other := newForeignTLSServer(t)
	t.Cleanup(other.Close)
	if _, err := pinned.Get(other.URL); err == nil {
		t.Error("pinned client trusted a foreign server")
	}
}

// newForeignTLSServer serves TLS with a fresh self-signed cert that no
// test CA pins.
func newForeignTLSServer(t *testing.T) *httptest.Server {
	t.Helper()
	priv, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	template := x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: "foreign.test"},
		NotBefore:             time.Now().Add(-time.Hour),
		NotAfter:              time.Now().Add(time.Hour),
		KeyUsage:              x509.KeyUsageDigitalSignature | x509.KeyUsageCertSign,
		ExtKeyUsage:           []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
		DNSNames:              []string{"foreign.test"},
		IPAddresses:           []net.IP{net.ParseIP("127.0.0.1")},
		BasicConstraintsValid: true,
		IsCA:                  true,
	}
	der, err := x509.CreateCertificate(rand.Reader, &template, &template, &priv.PublicKey, priv)
	if err != nil {
		t.Fatal(err)
	}
	keyDER, err := x509.MarshalECPrivateKey(priv)
	if err != nil {
		t.Fatal(err)
	}
	cert, err := tls.X509KeyPair(
		pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}),
		pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: keyDER}),
	)
	if err != nil {
		t.Fatal(err)
	}
	srv := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))
	srv.TLS = &tls.Config{Certificates: []tls.Certificate{cert}}
	srv.StartTLS()
	return srv
}

func TestFreeBytes(t *testing.T) {
	if avail, ok := freeBytes(t.TempDir()); !ok || avail == 0 {
		t.Logf("freeBytes(tempdir) = %d, %v (non-linux reports unknown)", avail, ok)
	}
	if _, ok := freeBytes(filepath.Join(t.TempDir(), "missing", "deeper")); ok {
		t.Error("freeBytes(missing path) reported ok")
	}
}

func TestInstallVersionOutputOverrunRefused(t *testing.T) {
	target := Version{0, 16, 0}
	// A -version flood (5 KB) trips the bounded capture, not the disk.
	newBin, err := os.ReadFile(buildFake(t, fakeOpts{version: strings.Repeat("v", 5000)}))
	if err != nil {
		t.Fatal(err)
	}
	key := testKey(t)
	files := map[string][]byte{
		"/agent/v0.16.0/" + fileBase():              newBin,
		"/agent/v0.16.0/" + fileBase() + ".sha256":  shaFile(t, newBin, fileBase()),
		"/agent/v0.16.0/" + fileBase() + ".minisig": key.sign(t, newBin, false),
	}
	f := newFixture(t, []byte("old"), files, target)
	swapTrustedKey(t, key.pub)

	installed, err := f.in.Run(context.Background(), f.urls, target, runtime.GOOS, runtime.GOARCH)

	requireErrorCategory(t, err, CategoryVerificationFailed)
	if installed {
		t.Fatal("installed after output overrun")
	}
	if !strings.Contains(err.Error(), "exceeds") {
		t.Fatalf("error %v does not name the overrun", err)
	}
}

func TestInstallEmptyMetaRefused(t *testing.T) {
	target := Version{0, 16, 0}
	newBin, err := os.ReadFile(buildFake(t, fakeOpts{version: "0.16.0"}))
	if err != nil {
		t.Fatal(err)
	}
	files := map[string][]byte{
		"/agent/v0.16.0/" + fileBase():             newBin,
		"/agent/v0.16.0/" + fileBase() + ".sha256": {},
	}
	f := newFixture(t, []byte("old"), files, target)

	installed, err := f.in.Run(context.Background(), f.urls, target, runtime.GOOS, runtime.GOARCH)

	requireErrorCategory(t, err, CategoryDownloadFailed)
	if installed {
		t.Fatal("installed with empty metadata")
	}
}

func TestInstallOversizeMetaRefused(t *testing.T) {
	target := Version{0, 16, 0}
	newBin, err := os.ReadFile(buildFake(t, fakeOpts{version: "0.16.0"}))
	if err != nil {
		t.Fatal(err)
	}
	files := map[string][]byte{
		"/agent/v0.16.0/" + fileBase():              newBin,
		"/agent/v0.16.0/" + fileBase() + ".sha256":  shaFile(t, newBin, fileBase()),
		"/agent/v0.16.0/" + fileBase() + ".minisig": bytes.Repeat([]byte("x"), maxMetaBytes+1),
	}
	f := newFixture(t, []byte("old"), files, target)

	installed, err := f.in.Run(context.Background(), f.urls, target, runtime.GOOS, runtime.GOARCH)

	requireErrorCategory(t, err, CategoryDownloadFailed)
	if installed {
		t.Fatal("installed with oversize metadata")
	}
}

func TestInstallPrevStageBlockedRefused(t *testing.T) {
	// A non-empty directory squatting the .prev staging name breaks the
	// hardlink step: pre-commit failure, canonical untouched.
	target := Version{0, 16, 0}
	newBin, err := os.ReadFile(buildFake(t, fakeOpts{version: "0.16.0"}))
	if err != nil {
		t.Fatal(err)
	}
	old := []byte("old")
	key := testKey(t)
	files := map[string][]byte{
		"/agent/v0.16.0/" + fileBase():              newBin,
		"/agent/v0.16.0/" + fileBase() + ".sha256":  shaFile(t, newBin, fileBase()),
		"/agent/v0.16.0/" + fileBase() + ".minisig": key.sign(t, newBin, false),
	}
	f := newFixture(t, old, files, target)
	swapTrustedKey(t, key.pub)
	stage := filepath.Join(f.dir, ".cadence-agent.prev.tmp")
	if err := os.Mkdir(stage, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(stage, "squat"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}

	installed, err := f.in.Run(context.Background(), f.urls, target, runtime.GOOS, runtime.GOARCH)

	requireErrorCategory(t, err, CategoryInstallFailed)
	if installed {
		t.Fatal("installed with blocked .prev staging")
	}
	if got := f.canonical(t); !bytes.Equal(got, old) {
		t.Fatal("canonical binary changed")
	}
}

func TestInstallPrevTargetIsDirRefused(t *testing.T) {
	// A directory at the .prev path breaks the atomic publish step.
	target := Version{0, 16, 0}
	newBin, err := os.ReadFile(buildFake(t, fakeOpts{version: "0.16.0"}))
	if err != nil {
		t.Fatal(err)
	}
	old := []byte("old")
	key := testKey(t)
	files := map[string][]byte{
		"/agent/v0.16.0/" + fileBase():              newBin,
		"/agent/v0.16.0/" + fileBase() + ".sha256":  shaFile(t, newBin, fileBase()),
		"/agent/v0.16.0/" + fileBase() + ".minisig": key.sign(t, newBin, false),
	}
	f := newFixture(t, old, files, target)
	swapTrustedKey(t, key.pub)
	if err := os.Mkdir(filepath.Join(f.dir, "cadence-agent.prev"), 0o755); err != nil {
		t.Fatal(err)
	}

	installed, err := f.in.Run(context.Background(), f.urls, target, runtime.GOOS, runtime.GOARCH)

	requireErrorCategory(t, err, CategoryInstallFailed)
	if installed {
		t.Fatal("installed with .prev blocked by a directory")
	}
	if got := f.canonical(t); !bytes.Equal(got, old) {
		t.Fatal("canonical binary changed")
	}
}

func TestInstallCanonicalIsDirRefused(t *testing.T) {
	target := Version{0, 16, 0}
	newBin, err := os.ReadFile(buildFake(t, fakeOpts{version: "0.16.0"}))
	if err != nil {
		t.Fatal(err)
	}
	key := testKey(t)
	files := map[string][]byte{
		"/agent/v0.16.0/" + fileBase():              newBin,
		"/agent/v0.16.0/" + fileBase() + ".sha256":  shaFile(t, newBin, fileBase()),
		"/agent/v0.16.0/" + fileBase() + ".minisig": key.sign(t, newBin, false),
	}
	f := newFixture(t, nil, files, target)
	swapTrustedKey(t, key.pub)
	if err := os.Mkdir(filepath.Join(f.dir, "cadence-agent"), 0o755); err != nil {
		t.Fatal(err)
	}

	installed, err := f.in.Run(context.Background(), f.urls, target, runtime.GOOS, runtime.GOARCH)

	requireErrorCategory(t, err, CategoryInstallFailed)
	if installed {
		t.Fatal("installed over a directory canonical path")
	}
}

func TestRedactURL(t *testing.T) {
	if got := redactURL("https://h.test/agent/v1?a=b#f"); got != "h.test/agent/v1" {
		t.Errorf("redactURL = %q", got)
	}
	if got := redactURL("://bad"); got != "artifact" {
		t.Errorf("redactURL(garbage) = %q", got)
	}
}

func TestDeadlineSlowHeadersAborts(t *testing.T) {
	target := Version{0, 16, 0}
	mux := http.NewServeMux()
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		// Headers arrive after the deadline; yield early when the
		// cancelled client goes away so teardown stays fast.
		select {
		case <-time.After(5 * time.Second):
			_, _ = w.Write([]byte("too late"))
		case <-r.Context().Done():
		}
	})
	srv := httptest.NewTLSServer(mux)
	t.Cleanup(srv.Close)
	dir := t.TempDir()
	old := []byte("old")
	if err := os.WriteFile(filepath.Join(dir, "cadence-agent"), old, 0o755); err != nil {
		t.Fatal(err)
	}
	base := srv.URL + "/agent/v0.16.0/" + fileBase()
	in := &Installer{
		Client:        testClientFor(t, srv),
		Dir:           dir,
		Name:          "cadence-agent",
		localDeadline: 300 * time.Millisecond,
	}
	urls := Artifacts{Binary: base, SHA256: base + ".sha256", MiniSig: base + ".minisig"}

	start := time.Now()
	installed, err := in.Run(context.Background(), urls, target, runtime.GOOS, runtime.GOARCH)
	if time.Since(start) > 4*time.Second {
		t.Fatal("slow headers outlived the deadline")
	}

	requireErrorCategory(t, err, CategoryDownloadFailed)
	if installed {
		t.Fatal("installed after slow headers")
	}
	if got, _ := os.ReadFile(filepath.Join(dir, "cadence-agent")); !bytes.Equal(got, old) {
		t.Fatal("canonical binary changed")
	}
}

func TestDeadlineSlowBodyAborts(t *testing.T) {
	target := Version{0, 16, 0}
	mux := http.NewServeMux()
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Length", "1000000")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("x"))
		if f, ok := w.(http.Flusher); ok {
			f.Flush()
		}
		// Then stall mid-body; yield early when the client goes away.
		select {
		case <-time.After(5 * time.Second):
		case <-r.Context().Done():
		}
	})
	srv := httptest.NewTLSServer(mux)
	t.Cleanup(srv.Close)
	dir := t.TempDir()
	old := []byte("old")
	if err := os.WriteFile(filepath.Join(dir, "cadence-agent"), old, 0o755); err != nil {
		t.Fatal(err)
	}
	base := srv.URL + "/agent/v0.16.0/" + fileBase()
	in := &Installer{
		Client:        testClientFor(t, srv),
		Dir:           dir,
		Name:          "cadence-agent",
		localDeadline: 300 * time.Millisecond,
	}
	urls := Artifacts{Binary: base, SHA256: base + ".sha256", MiniSig: base + ".minisig"}

	start := time.Now()
	installed, err := in.Run(context.Background(), urls, target, runtime.GOOS, runtime.GOARCH)
	if time.Since(start) > 4*time.Second {
		t.Fatal("slow body outlived the deadline")
	}

	requireErrorCategory(t, err, CategoryDownloadFailed)
	if installed {
		t.Fatal("installed after slow body")
	}
	if got, _ := os.ReadFile(filepath.Join(dir, "cadence-agent")); !bytes.Equal(got, old) {
		t.Fatal("canonical binary changed")
	}
}

func TestDeadlineStalledSecondArtifactAborts(t *testing.T) {
	target := Version{0, 16, 0}
	trace := filepath.Join(t.TempDir(), "trace")
	newBin, err := os.ReadFile(buildFake(t, fakeOpts{version: "0.16.0", trace: trace}))
	if err != nil {
		t.Fatal(err)
	}
	old := []byte("old")
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "cadence-agent"), old, 0o755); err != nil {
		t.Fatal(err)
	}
	base := "/agent/v0.16.0/" + fileBase()
	mux := http.NewServeMux()
	mux.HandleFunc(base, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write(newBin) // binary fast...
	})
	mux.HandleFunc(base+".sha256", func(w http.ResponseWriter, r *http.Request) {
		// ...but the checksum stalls; yield early on client cancel.
		select {
		case <-time.After(5 * time.Second):
		case <-r.Context().Done():
		}
	})
	srv := httptest.NewTLSServer(mux)
	t.Cleanup(srv.Close)
	in := &Installer{
		Client:        testClientFor(t, srv),
		Dir:           dir,
		Name:          "cadence-agent",
		localDeadline: 500 * time.Millisecond,
	}
	abs := srv.URL + base
	urls := Artifacts{Binary: abs, SHA256: abs + ".sha256", MiniSig: abs + ".minisig"}

	installed, err := in.Run(context.Background(), urls, target, runtime.GOOS, runtime.GOARCH)

	requireErrorCategory(t, err, CategoryDownloadFailed)
	if installed {
		t.Fatal("installed after stalled sidecar")
	}
	if _, serr := os.Stat(trace); !os.IsNotExist(serr) {
		t.Fatal("unverified binary was executed")
	}
	if got, _ := os.ReadFile(filepath.Join(dir, "cadence-agent")); !bytes.Equal(got, old) {
		t.Fatal("canonical binary changed")
	}
}

func TestDeadlineBeatsVersionProbeTimeout(t *testing.T) {
	target := Version{0, 16, 0}
	newBin, err := os.ReadFile(buildFake(t, fakeOpts{version: "0.16.0", sleep: "30"}))
	if err != nil {
		t.Fatal(err)
	}
	old := []byte("old")
	key := testKey(t)
	files := map[string][]byte{
		"/agent/v0.16.0/" + fileBase():              newBin,
		"/agent/v0.16.0/" + fileBase() + ".sha256":  shaFile(t, newBin, fileBase()),
		"/agent/v0.16.0/" + fileBase() + ".minisig": key.sign(t, newBin, false),
	}
	f := newFixture(t, old, files, target)
	swapTrustedKey(t, key.pub)
	f.in.localDeadline = 300 * time.Millisecond
	f.in.versionProbeTimeout = 30 * time.Second // longer: the global deadline must win

	start := time.Now()
	installed, err := f.in.Run(context.Background(), f.urls, target, runtime.GOOS, runtime.GOARCH)
	if time.Since(start) > 10*time.Second {
		t.Fatal("global deadline did not beat the probe timeout")
	}

	requireErrorCategory(t, err, CategoryVerificationFailed)
	if installed {
		t.Fatal("installed after probe overran the deadline")
	}
	if got := f.canonical(t); !bytes.Equal(got, old) {
		t.Fatal("canonical binary changed")
	}
}

func TestDeadlineBeforePrevAbandons(t *testing.T) {
	// A 1ns deadline is already spent before the first fetch can finish:
	// the attempt dies before .prev is ever touched.
	target := Version{0, 16, 0}
	newBin, err := os.ReadFile(buildFake(t, fakeOpts{version: "0.16.0"}))
	if err != nil {
		t.Fatal(err)
	}
	old := []byte("old")
	key := testKey(t)
	files := map[string][]byte{
		"/agent/v0.16.0/" + fileBase():              newBin,
		"/agent/v0.16.0/" + fileBase() + ".sha256":  shaFile(t, newBin, fileBase()),
		"/agent/v0.16.0/" + fileBase() + ".minisig": key.sign(t, newBin, false),
	}
	f := newFixture(t, old, files, target)
	swapTrustedKey(t, key.pub)
	f.in.localDeadline = time.Nanosecond

	installed, err := f.in.Run(context.Background(), f.urls, target, runtime.GOOS, runtime.GOARCH)

	if err == nil || installed {
		t.Fatalf("installed=%v err=%v, want pre-commit abort", installed, err)
	}
	if got := f.canonical(t); !bytes.Equal(got, old) {
		t.Fatal("canonical binary changed")
	}
	if _, present := f.prev(t); present {
		t.Fatal(".prev must not exist when the deadline fires first")
	}
}

func TestDeadlineAfterPrevBeforeRenameAbandons(t *testing.T) {
	// The hook sleeps past a short deadline inside the pre-rename window:
	// the commit gate must refuse with the canonical binary intact and a
	// coherent .prev (it holds the still-current binary).
	target := Version{0, 16, 0}
	newBin, err := os.ReadFile(buildFake(t, fakeOpts{version: "0.16.0"}))
	if err != nil {
		t.Fatal(err)
	}
	old := []byte("old")
	key := testKey(t)
	files := map[string][]byte{
		"/agent/v0.16.0/" + fileBase():              newBin,
		"/agent/v0.16.0/" + fileBase() + ".sha256":  shaFile(t, newBin, fileBase()),
		"/agent/v0.16.0/" + fileBase() + ".minisig": key.sign(t, newBin, false),
	}
	f := newFixture(t, old, files, target)
	swapTrustedKey(t, key.pub)
	// Generous deadline for the fast pre-hook phases, then a hook sleep
	// that deterministically outlives it inside the pre-rename window.
	f.in.localDeadline = time.Second
	f.in.beforeRename = func() { time.Sleep(2500 * time.Millisecond) }

	installed, err := f.in.Run(context.Background(), f.urls, target, runtime.GOOS, runtime.GOARCH)

	requireErrorCategory(t, err, CategoryInstallFailed)
	if installed {
		t.Fatal("committed after the deadline fired pre-rename")
	}
	if !strings.Contains(err.Error(), "deadline exceeded") {
		t.Fatalf("error %v does not name the deadline", err)
	}
	if got := f.canonical(t); !bytes.Equal(got, old) {
		t.Fatal("canonical binary changed")
	}
	prev, present := f.prev(t)
	if !present || !bytes.Equal(prev, old) {
		t.Fatal(".prev is not the still-current binary")
	}
}

// fakeClock is a wall clock the test moves by hand. Built from time.Unix,
// so it carries no monotonic reading, exactly like the production gate's
// UnixNano comparisons.
type fakeClock struct {
	wall time.Time
}

func (f *fakeClock) Now() time.Time { return f.wall }

func (f *fakeClock) advance(d time.Duration) { f.wall = f.wall.Add(d) }

func TestWallExceeded(t *testing.T) {
	start := time.Unix(1_700_000_000, 0).UnixNano()
	maxLocal := 300 * time.Second
	cases := []struct {
		name    string
		advance time.Duration
		want    bool
	}{
		{"start", 0, false},
		{"normal progress", 30 * time.Second, false},
		{"just inside", 300 * time.Second, false},
		{"just outside", 300*time.Second + time.Nanosecond, true},
		{"suspend past window", 600 * time.Second, true},
		{"forward jump", 3600 * time.Second, true},
		{"small backward drift tolerated", -30 * time.Second, false},
		{"backward at tolerance edge", -60 * time.Second, false},
		{"gross backward incoherence refuses", -61 * time.Second, true},
		{"clock reset to epoch refuses", -100000 * time.Hour, true},
	}
	for _, c := range cases {
		now := start + int64(c.advance)
		if got := wallExceeded(now, start, maxLocal); got != c.want {
			t.Errorf("%s: wallExceeded = %v, want %v", c.name, got, c.want)
		}
	}
}

// wallFixture wires a happy-path install whose wall clock the test drives.
// The monotonic context keeps a generous real deadline so only the wall
// gate can fire.
func wallFixture(t *testing.T, version string) (*fixture, *fakeClock) {
	t.Helper()
	target := Version{0, 16, 0}
	newBin, err := os.ReadFile(buildFake(t, fakeOpts{version: version}))
	if err != nil {
		t.Fatal(err)
	}
	key := testKey(t)
	files := map[string][]byte{
		"/agent/v0.16.0/" + fileBase():              newBin,
		"/agent/v0.16.0/" + fileBase() + ".sha256":  shaFile(t, newBin, fileBase()),
		"/agent/v0.16.0/" + fileBase() + ".minisig": key.sign(t, newBin, false),
	}
	f := newFixture(t, []byte("old"), files, target)
	swapTrustedKey(t, key.pub)
	fc := &fakeClock{wall: time.Unix(1_700_000_000, 0)}
	f.in.clock = fc
	return f, fc
}

func TestSuspendSimulatedRefusesRename(t *testing.T) {
	// 30s of normal progress, then a 600s wall jump with the monotonic
	// context untouched (what a host suspend looks like to the two
	// clocks): the commit gate must refuse with the old binary intact.
	f, fc := wallFixture(t, "0.16.0")
	fc.advance(30 * time.Second)
	f.in.beforeRename = func() { fc.advance(600 * time.Second) }

	installed, err := f.in.Run(context.Background(), f.urls, f.target, runtime.GOOS, runtime.GOARCH)

	requireErrorCategory(t, err, CategoryInstallFailed)
	if installed {
		t.Fatal("committed after a simulated suspend")
	}
	if !strings.Contains(err.Error(), "wall-clock") {
		t.Fatalf("error %v does not name the wall-clock gate", err)
	}
	if got := f.canonical(t); !bytes.Equal(got, []byte("old")) {
		t.Fatal("canonical binary changed")
	}
	prev, present := f.prev(t)
	if !present || !bytes.Equal(prev, []byte("old")) {
		t.Fatal(".prev is not the still-current binary")
	}
}

func TestWallNormalProgressCommits(t *testing.T) {
	f, fc := wallFixture(t, "0.16.0")
	fc.advance(30 * time.Second)
	f.in.beforeRename = func() { fc.advance(10 * time.Second) }

	installed, err := f.in.Run(context.Background(), f.urls, f.target, runtime.GOOS, runtime.GOARCH)

	if err != nil || !installed {
		t.Fatalf("installed=%v err=%v, want clean install", installed, err)
	}
}

func TestWallForwardJumpRefuses(t *testing.T) {
	f, fc := wallFixture(t, "0.16.0")
	f.in.beforeRename = func() { fc.advance(301 * time.Second) }

	installed, err := f.in.Run(context.Background(), f.urls, f.target, runtime.GOOS, runtime.GOARCH)

	requireErrorCategory(t, err, CategoryInstallFailed)
	if installed {
		t.Fatal("committed after a forward clock jump")
	}
	if got := f.canonical(t); !bytes.Equal(got, []byte("old")) {
		t.Fatal("canonical binary changed")
	}
}

func TestWallSmallBackwardDriftTolerated(t *testing.T) {
	// A stepped NTP correction (seconds back) must not break a healthy
	// upgrade: worst case it stretches the wall window by under 60s,
	// still inside the 120s server-side margin.
	f, fc := wallFixture(t, "0.16.0")
	fc.advance(30 * time.Second)
	f.in.beforeRename = func() { fc.advance(-50 * time.Second) } // net -20s

	installed, err := f.in.Run(context.Background(), f.urls, f.target, runtime.GOOS, runtime.GOARCH)

	if err != nil || !installed {
		t.Fatalf("installed=%v err=%v, want clean install", installed, err)
	}
}

func TestWallGrossBackwardJumpRefuses(t *testing.T) {
	// Beyond maxBackwardSkew the clocks are incoherent (a CMOS reset, a
	// deliberate setback): fail closed rather than extend the window.
	f, fc := wallFixture(t, "0.16.0")
	fc.advance(30 * time.Second)
	f.in.beforeRename = func() { fc.advance(-100 * time.Second) } // net -70s

	installed, err := f.in.Run(context.Background(), f.urls, f.target, runtime.GOOS, runtime.GOARCH)

	requireErrorCategory(t, err, CategoryInstallFailed)
	if installed {
		t.Fatal("committed after a gross backward jump")
	}
	if got := f.canonical(t); !bytes.Equal(got, []byte("old")) {
		t.Fatal("canonical binary changed")
	}
}

func TestInstallVersionExitNonzeroRefused(t *testing.T) {
	target := Version{0, 16, 0}
	newBin, err := os.ReadFile(buildFake(t, fakeOpts{exit: "3"}))
	if err != nil {
		t.Fatal(err)
	}
	key := testKey(t)
	files := map[string][]byte{
		"/agent/v0.16.0/" + fileBase():              newBin,
		"/agent/v0.16.0/" + fileBase() + ".sha256":  shaFile(t, newBin, fileBase()),
		"/agent/v0.16.0/" + fileBase() + ".minisig": key.sign(t, newBin, false),
	}
	f := newFixture(t, []byte("old"), files, target)
	swapTrustedKey(t, key.pub)

	installed, err := f.in.Run(context.Background(), f.urls, target, runtime.GOOS, runtime.GOARCH)

	requireErrorCategory(t, err, CategoryVerificationFailed)
	if installed {
		t.Fatal("installed after failing probe")
	}
}
