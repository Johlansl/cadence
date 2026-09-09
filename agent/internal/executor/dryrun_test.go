package executor

import (
	"context"
	"reflect"
	"testing"

	"cadence/agent/internal/apterr"
)

// simOutput is a canned `apt-get -s dist-upgrade` output: one upgrade, one
// newly pulled dependency, one removal, and a kept-back block.
const simOutput = `Reading package lists...
Building dependency tree...
Calculating upgrade...
The following packages will be REMOVED:
  obsolete-lib
The following NEW packages will be installed:
  libfoo1
The following packages will be upgraded:
  base-files openssl
Inst base-files [12.4+deb12u5] (12.4+deb12u7 Debian:12/stable [amd64])
Inst libfoo1 (1.2.3-1 Debian:12/stable [amd64])
Inst openssl [3.0.11-1] (3.0.14-1 Debian-Security:12/stable-security [amd64])
Remv obsolete-lib [4.5-6]
The following packages have been kept back:
  docker-ce
Conf base-files (12.4+deb12u7 Debian:12/stable [amd64])
`

func fakeAptForDryRun(t *testing.T, dir, distUpgradeBody string) {
	t.Helper()
	fakeBin(t, dir, "apt-get", `case "$*" in
*"-s dist-upgrade"*) `+distUpgradeBody+` ;;
*update*) exit 0 ;;
*) exit 0 ;;
esac`)
}

func TestRunAptDryRunSuccess(t *testing.T) {
	dir := fakePATH(t)
	fakeAptForDryRun(t, dir, `cat <<'EOF'
`+simOutput+`EOF
exit 0`)
	fakeBin(t, dir, "apt-mark", `case "$1" in showhold) echo docker-ce ;; esac; exit 0`)

	out := RunAptDryRun(context.Background(), nil)

	if out.Status != "succeeded" || out.ExitCode != 0 {
		t.Fatalf("want clean success, got status=%q exit=%d cat=%q log=%q",
			out.Status, out.ExitCode, out.FailureCategory, out.Log)
	}
	if out.DryRun == nil {
		t.Fatal("DryRun is nil on success")
	}
	dr := out.DryRun

	if len(dr.Updated) != 2 || dr.Updated[0].Name != "base-files" || dr.Updated[1].Name != "openssl" {
		t.Fatalf("Updated = %+v", dr.Updated)
	}
	if dr.Updated[1].InstalledVersion != "3.0.11-1" || dr.Updated[1].CandidateVersion != "3.0.14-1" || !dr.Updated[1].IsSecurityUpdate {
		t.Errorf("openssl entry = %+v", dr.Updated[1])
	}
	if len(dr.NewlyInstalled) != 1 || dr.NewlyInstalled[0].Name != "libfoo1" || dr.NewlyInstalled[0].InstalledVersion != "" {
		t.Fatalf("NewlyInstalled = %+v", dr.NewlyInstalled)
	}
	if len(dr.Removed) != 1 || dr.Removed[0].Name != "obsolete-lib" || dr.Removed[0].InstalledVersion != "4.5-6" {
		t.Fatalf("Removed = %+v", dr.Removed)
	}
	if !reflect.DeepEqual(dr.KeptBack, []string{"docker-ce"}) {
		t.Errorf("KeptBack = %v", dr.KeptBack)
	}
	if !reflect.DeepEqual(dr.HeldInPlace, []string{"docker-ce"}) {
		t.Errorf("HeldInPlace = %v (kept back and on hold, should be correlated)", dr.HeldInPlace)
	}
	if len(dr.Excluded) != 0 {
		t.Errorf("Excluded = %v, want empty", dr.Excluded)
	}
}

func TestRunAptDryRunFiltersExclusions(t *testing.T) {
	dir := fakePATH(t)
	fakeAptForDryRun(t, dir, `cat <<'EOF'
`+simOutput+`EOF
exit 0`)
	fakeBin(t, dir, "apt-mark", `exit 0`)

	out := RunAptDryRun(context.Background(), []string{"openssl", "libfoo1"})

	if out.Status != "succeeded" || out.DryRun == nil {
		t.Fatalf("want success, got %q / %v", out.Status, out.DryRun)
	}
	dr := out.DryRun
	if len(dr.Updated) != 1 || dr.Updated[0].Name != "base-files" {
		t.Errorf("Updated = %+v, want only base-files (openssl excluded)", dr.Updated)
	}
	if len(dr.NewlyInstalled) != 0 {
		t.Errorf("NewlyInstalled = %+v, want empty (libfoo1 excluded)", dr.NewlyInstalled)
	}
	if !reflect.DeepEqual(dr.Excluded, []string{"libfoo1", "openssl"}) {
		t.Errorf("Excluded = %v, want [libfoo1 openssl]", dr.Excluded)
	}
}

func TestRunAptDryRunClassifiesFailure(t *testing.T) {
	dir := fakePATH(t)
	fakeAptForDryRun(t, dir, `echo "E: Failed to fetch http://deb.debian.org/... Could not resolve 'deb.debian.org'" >&2
exit 100`)
	fakeBin(t, dir, "apt-mark", `exit 0`)

	out := RunAptDryRun(context.Background(), nil)

	if out.Status != "failed" {
		t.Fatalf("want failed, got %q", out.Status)
	}
	if out.ExitCode != 100 {
		t.Errorf("ExitCode = %d, want 100", out.ExitCode)
	}
	if out.FailureCategory != apterr.CategoryNetworkOrRepo {
		t.Errorf("FailureCategory = %q, want %q", out.FailureCategory, apterr.CategoryNetworkOrRepo)
	}
	if out.DryRun != nil {
		t.Errorf("DryRun should be nil on failure, got %+v", out.DryRun)
	}
}

func TestRunAptDryRunUpdateFailureIsNonFatal(t *testing.T) {
	dir := fakePATH(t)
	fakeBin(t, dir, "apt-get", `case "$*" in
*"-s dist-upgrade"*) cat <<'EOF'
`+simOutput+`EOF
exit 0 ;;
*update*) echo "W: Failed to fetch ... temporary failure" >&2; exit 100 ;;
esac`)
	fakeBin(t, dir, "apt-mark", `exit 0`)

	out := RunAptDryRun(context.Background(), nil)

	if out.Status != "succeeded" || out.DryRun == nil {
		t.Fatalf("apt-get update failure should not fail the dry-run: status=%q log=%q", out.Status, out.Log)
	}
	if len(out.DryRun.Updated) != 2 {
		t.Errorf("Updated = %+v", out.DryRun.Updated)
	}
}
