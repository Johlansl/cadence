package upgrade

import (
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"runtime"
	"strings"
	"testing"
)

// TestParamsCarriesVersionOnly pins the closed contract at the type level:
// target_version in, nothing else expressible. No URL, path, checksum,
// pubkey, command, or bypass flag can ever ride the params struct.
func TestParamsCarriesVersionOnly(t *testing.T) {
	typ := reflect.TypeOf(Params{})
	if typ.NumField() != 1 {
		t.Fatalf("Params has %d fields, want exactly 1", typ.NumField())
	}
	f := typ.Field(0)
	if f.Name != "TargetVersion" || f.Tag.Get("json") != "target_version" {
		t.Fatalf("Params field = %s %q", f.Name, f.Tag.Get("json"))
	}
}

// TestInstallerHoldsNoCredentialPaths pins the narrow write allowlist at
// the type level: the installer names an install dir and nothing else. No
// credential, config, unit, or enrollment path is expressible.
func TestInstallerHoldsNoCredentialPaths(t *testing.T) {
	allowed := map[string]bool{
		"Client": true, "Dir": true, "Name": true, "MaxBinaryBytes": true,
		"versionProbeTimeout": true, "localDeadline": true, "beforeRename": true,
		"clock":           true,
		"faultBeforePrev": true, "faultAfterPrev": true, "faultAfterRename": true,
	}
	typ := reflect.TypeOf(Installer{})
	for i := 0; i < typ.NumField(); i++ {
		name := typ.Field(i).Name
		if !allowed[name] {
			t.Errorf("Installer gained field %q: widen the allowlist deliberately or not at all", name)
		}
		lower := strings.ToLower(name)
		for _, banned := range []string{
			"token", "secret", "cert", "key", "password", "credential",
			"config", "env", "unit", "service", "etc",
		} {
			if strings.Contains(lower, banned) {
				t.Errorf("Installer field %q smells like a credential path", name)
			}
		}
	}
}

// TestNoForbiddenPrimitives scans the package's own non-test sources for
// constructs the upgrade primitive must never grow: bulk deletion, shells,
// service control, credential paths, unsigned fallbacks, and unguarded
// subprocess or HTTP entry points. Cheap tripwires, not a review
// replacement: each exists because its misuse here is catastrophic.
func TestNoForbiddenPrimitives(t *testing.T) {
	_, self, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("cannot locate package dir")
	}
	dir := filepath.Dir(self)
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	forbidden := []string{
		"os.RemoveAll",       // never bulk-delete near /usr/bin
		"/bin/sh",            // never a shell
		"sh -c",              // never a shell command line
		"systemctl",          // never service control; timers relaunch us
		"reboot",             // never reboot from the upgrader
		"/etc/cadence",       // never touch credentials or config
		"agent.env",          // never touch credentials or config
		"InsecureSkipVerify", // never skip TLS verification
		"http.Get(",          // fetches go through the guarded client only
		"http.Post(",         // fetches go through the guarded client only
		"PrivateKey",         // the agent verifies; it never signs
		"allow_downgrade",    // no such knob exists, anywhere
		"fallback",           // no unsigned or degraded-mode fallback
	}
	bareExec := regexp.MustCompile(`exec\.Command\(`)
	for _, e := range entries {
		name := e.Name()
		if !strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
			continue
		}
		raw, err := os.ReadFile(filepath.Join(dir, name))
		if err != nil {
			t.Fatal(err)
		}
		src := string(raw)
		for _, s := range forbidden {
			if strings.Contains(src, s) {
				t.Errorf("%s contains forbidden %q", name, s)
			}
		}
		// exec.CommandContext only: every subprocess dies on timeout.
		// ("exec.CommandContext(" does not match the bare-call regex.)
		if bareExec.MatchString(src) {
			t.Errorf("%s uses bare exec.Command (use exec.CommandContext)", name)
		}
	}
}
