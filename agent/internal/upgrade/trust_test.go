package upgrade

import (
	"encoding/hex"
	"os"
	"path/filepath"
	"strings"
	"testing"

	minisign "github.com/jedisct1/go-minisign"
)

func TestTrustedKeyMatchesRepoKey(t *testing.T) {
	for _, tc := range []struct {
		file string
		text string
	}{
		{"minisign.pub", activePubKeyText},
		{"minisign-backup.pub", backupPubKeyText},
	} {
		t.Run(tc.file, func(t *testing.T) {
			raw, err := os.ReadFile(filepath.Join("..", "..", tc.file))
			if err != nil {
				t.Fatal(err)
			}
			if strings.TrimSpace(string(raw)) != strings.TrimSpace(tc.text) {
				t.Fatalf("embedded trusted key drifted from agent/%s: "+
					"rotate both together", tc.file)
			}
			key, err := parsePubKey(tc.text)
			if err != nil {
				t.Fatal(err)
			}
			if key.KeyId != keyIDFromComment(t, tc.text) {
				t.Errorf("parsed key ID = %X, want the ID in the comment line", key.KeyId)
			}
		})
	}
}

func TestTrustedKeysAreDistinct(t *testing.T) {
	keys := mustBuildTrustedKeys(activePubKeyText, backupPubKeyText)
	if len(keys) != 2 {
		t.Fatalf("got %d keys, want 2", len(keys))
	}
	if keys[0].KeyId == keys[1].KeyId {
		t.Fatal("active and backup keys must not share a key ID")
	}
}

func TestMustBuildTrustedKeysPanicsOnDuplicateKeyID(t *testing.T) {
	defer func() {
		if recover() == nil {
			t.Fatal("mustBuildTrustedKeys accepted two keys sharing a key ID")
		}
	}()
	mustBuildTrustedKeys(activePubKeyText, activePubKeyText)
}

// keyIDFromComment extracts the key ID from a .pub file's own
// "untrusted comment: minisign public key <HEX>" line and returns it as the
// raw little-endian bytes minisign.PublicKey.KeyId carries (the CLI prints
// the ID as the big-endian hex string of those same bytes reversed).
// Deriving the expectation from the text itself, rather than a hand-typed
// byte literal, is what actually catches a comment/key mismatch instead of
// just restating one.
func keyIDFromComment(t *testing.T, text string) [8]byte {
	t.Helper()
	const prefix = "untrusted comment: minisign public key "
	line := strings.SplitN(text, "\n", 2)[0]
	if !strings.HasPrefix(line, prefix) {
		t.Fatalf("key text has no recognizable comment line: %q", line)
	}
	raw, err := hex.DecodeString(strings.TrimSpace(strings.TrimPrefix(line, prefix)))
	if err != nil || len(raw) != 8 {
		t.Fatalf("cannot parse key ID in %q: %v", line, err)
	}
	var id [8]byte
	for i, b := range raw {
		id[len(raw)-1-i] = b
	}
	return id
}

func TestInteropVerifiesCLIProducedSignature(t *testing.T) {
	dir := filepath.Join("testdata", "minisign-interop")
	msg, err := os.ReadFile(filepath.Join(dir, "message.bin"))
	if err != nil {
		t.Fatal(err)
	}
	sigText, err := os.ReadFile(filepath.Join(dir, "message.bin.minisig"))
	if err != nil {
		t.Fatal(err)
	}
	pubText, err := os.ReadFile(filepath.Join(dir, "interop-test.pub"))
	if err != nil {
		t.Fatal(err)
	}
	key, err := parsePubKey(string(pubText))
	if err != nil {
		t.Fatal(err)
	}
	swapTrustedKey(t, key)

	comment, err := verifySignature(msg, sigText)
	if err != nil {
		t.Fatalf("CLI-produced signature rejected: %v", err)
	}
	if comment != "trusted comment: cadence interop fixture" {
		t.Errorf("trusted comment = %q", comment)
	}
}

// swapTrustedKey replaces the entire trust set with a single test key for
// one test. Every call site elsewhere in this package (install_test.go)
// still expects exactly this: trust only the one key under test.
func swapTrustedKey(t *testing.T, key minisign.PublicKey) {
	t.Helper()
	prev := trustedKeys
	trustedKeys = []minisign.PublicKey{key}
	t.Cleanup(func() { trustedKeys = prev })
}

func TestVerifySignatureRejects(t *testing.T) {
	dir := filepath.Join("testdata", "minisign-interop")
	msg, _ := os.ReadFile(filepath.Join(dir, "message.bin"))
	sigText, _ := os.ReadFile(filepath.Join(dir, "message.bin.minisig"))
	pubText, _ := os.ReadFile(filepath.Join(dir, "interop-test.pub"))
	key, err := parsePubKey(string(pubText))
	if err != nil {
		t.Fatal(err)
	}
	swapTrustedKey(t, key)

	other := testKey(t) // a different valid key: must not verify
	swapToOther := func() { swapTrustedKey(t, other.pub) }

	cases := map[string]func(){
		"tampered message": func() {
			bad := append([]byte(nil), msg...)
			bad[0] ^= 0xFF
			if _, err := verifySignature(bad, sigText); err == nil {
				t.Error("tampered message accepted")
			}
		},
		"tampered signature": func() {
			bad := append([]byte(nil), sigText...)
			bad[len(bad)-5] ^= 0xFF
			if _, err := verifySignature(msg, bad); err == nil {
				t.Error("tampered signature accepted")
			}
		},
		"truncated signature": func() {
			if _, err := verifySignature(msg, sigText[:100]); err == nil {
				t.Error("truncated signature accepted")
			}
		},
		"empty signature": func() {
			if _, err := verifySignature(msg, nil); err == nil {
				t.Error("empty signature accepted")
			}
		},
		"oversized signature": func() {
			big := make([]byte, maxMetaBytes+1)
			if _, err := verifySignature(msg, big); err == nil {
				t.Error("oversized signature accepted")
			}
		},
		"garbage signature": func() {
			if _, err := verifySignature(msg, []byte("not a signature")); err == nil {
				t.Error("garbage signature accepted")
			}
		},
		"wrong key": func() {
			swapToOther()
			if _, err := verifySignature(msg, sigText); err == nil {
				t.Error("signature verified under another key")
			}
		},
	}
	for name, fn := range cases {
		t.Run(name, func(t *testing.T) { fn() })
	}
}

func TestParsePubKeyRejects(t *testing.T) {
	for _, in := range []string{
		"",
		"garbage",
		"untrusted comment: only one line",
		"wrong prefix\nQUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo=",
		"untrusted comment: bad base64\n!!!not-base64!!!",
		"untrusted comment: short\nQUJD",
		"untrusted comment: a\nQUJD\ntrailing line",
	} {
		if _, err := parsePubKey(in); err == nil {
			t.Errorf("parsePubKey(%q) accepted", in)
		}
	}
}
