package apterr

import "testing"

func TestIsLockHeld(t *testing.T) {
	held := []string{
		"E: Could not get lock /var/lib/dpkg/lock-frontend",
		"E: Unable to acquire the dpkg frontend lock (/var/lib/dpkg/lock-frontend), is another process using it?",
		"Waiting for cache lock: Resource temporarily unavailable",
		"could not get lock /var/lib/apt/lists/lock",
	}
	for _, s := range held {
		if !IsLockHeld(s) {
			t.Errorf("IsLockHeld(%q) = false, want true", s)
		}
	}
	notHeld := []string{
		"E: Sub-process /usr/bin/dpkg returned an error code (1)",
		"E: Unable to locate package foo",
		"",
	}
	for _, s := range notHeld {
		if IsLockHeld(s) {
			t.Errorf("IsLockHeld(%q) = true, want false", s)
		}
	}
}

func TestIsInterrupted(t *testing.T) {
	yes := []string{
		"E: dpkg was interrupted, you must manually run 'dpkg --configure -a' to correct the problem.",
		"run 'dpkg --configure -a'",
	}
	for _, s := range yes {
		if !IsInterrupted(s) {
			t.Errorf("IsInterrupted(%q) = false, want true", s)
		}
	}
	no := []string{"E: Could not get lock", "some unrelated error", ""}
	for _, s := range no {
		if IsInterrupted(s) {
			t.Errorf("IsInterrupted(%q) = true, want false", s)
		}
	}
}
