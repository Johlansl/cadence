package upgrade

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	minisign "github.com/jedisct1/go-minisign"
)

func TestTrustedKeyMatchesRepoKey(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("..", "..", "minisign.pub"))
	if err != nil {
		t.Fatal(err)
	}
	if strings.TrimSpace(string(raw)) != strings.TrimSpace(trustedPubKeyText) {
		t.Fatal("embedded trusted key drifted from agent/minisign.pub: " +
			"rotate both together")
	}
	key, err := parsePubKey(trustedPubKeyText)
	if err != nil {
		t.Fatal(err)
	}
	// The CLI prints the key ID as a little-endian uint64
	// (229C17EE3D3EDC7B); on the wire it is these raw bytes.
	var wantID [8]byte
	copy(wantID[:], []byte{0x7B, 0xDC, 0x3E, 0x3D, 0xEE, 0x17, 0x9C, 0x22})
	if key.KeyId != wantID {
		t.Errorf("trusted key ID = %X, want 7BDC3E3DEE179C22", key.KeyId)
	}
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

// swapTrustedKey installs a test root of trust for one test.
func swapTrustedKey(t *testing.T, key minisign.PublicKey) {
	t.Helper()
	prev := trustedPubKey
	trustedPubKey = key
	t.Cleanup(func() { trustedPubKey = prev })
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
	swapToOther := func() {
		prev := trustedPubKey
		trustedPubKey = other.pub
		t.Cleanup(func() { trustedPubKey = prev })
	}

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
		"wrong prefix\nRWR73D497hecIlc6UpAyi6dawVeZ4k5+XJQpz7s74EA9coOyDo0PazMr",
		"untrusted comment: bad base64\n!!!not-base64!!!",
		"untrusted comment: short\nQUJD",
		"untrusted comment: a\nQUJD\ntrailing line",
	} {
		if _, err := parsePubKey(in); err == nil {
			t.Errorf("parsePubKey(%q) accepted", in)
		}
	}
}
