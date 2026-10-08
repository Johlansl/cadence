package upgrade

import (
	"fmt"
	"strings"

	minisign "github.com/jedisct1/go-minisign"
)

// trustedPubKeyText is the Minisign root of trust for agent upgrades: the
// committed fleet public key (agent/minisign.pub), compiled into the
// binary as a literal. The server, the job, and every runtime config are
// unable to replace it: verification below parses this text and nothing
// else. A rotation edits the file and this literal together; TestTrustedKey
// MatchesRepoKey fails CI on any drift between the two. (A go:embed was
// considered and rejected: embed patterns cannot name parent directories,
// so the literal plus the sync test is the smaller mechanism.) In-package
// tests reassign trustedPubKey (a test-only seam, never a production knob)
// to exercise verification against test keys.
const trustedPubKeyText = `untrusted comment: minisign public key 229C17EE3D3EDC7B
RWR73D497hecIlc6UpAyi6dawVeZ4k5+XJQpz7s74EA9coOyDo0PazMr`

// trustedPubKey is the parsed root of trust. Reassigned by in-package
// tests only.
var trustedPubKey = mustParsePubKey(trustedPubKeyText)

func mustParsePubKey(text string) minisign.PublicKey {
	key, err := parsePubKey(text)
	if err != nil {
		panic("upgrade: embedded minisign public key is invalid: " + err.Error())
	}
	return key
}

// parsePubKey decodes full .pub file content ("untrusted comment: ...\n" +
// base64 key line). Strict: exactly the two lines, nothing else.
func parsePubKey(text string) (minisign.PublicKey, error) {
	var zero minisign.PublicKey
	lines := strings.Split(strings.TrimRight(text, "\n"), "\n")
	if len(lines) != 2 || !strings.HasPrefix(lines[0], "untrusted comment: ") {
		return zero, fmt.Errorf("minisign public key is malformed")
	}
	key, err := minisign.DecodePublicKey(lines[0] + "\n" + lines[1])
	if err != nil {
		return zero, fmt.Errorf("minisign public key is malformed: %v", err)
	}
	return key, nil
}

// verifySignature checks the downloaded bytes against a .minisig file with
// the embedded root of trust. The library call verifies the message
// signature, the key identifier binding, and the trusted comment's global
// signature; any failure (or a malformed signature file) refuses. The
// trusted comment is returned for logging only: it is never version truth.
func verifySignature(data, sigText []byte) (trustedComment string, err error) {
	if len(sigText) == 0 || len(sigText) > maxMetaBytes {
		return "", fmt.Errorf("signature file has an impossible size")
	}
	sig, err := minisign.DecodeSignature(string(sigText))
	if err != nil {
		return "", fmt.Errorf("signature file is malformed: %v", err)
	}
	ok, err := trustedPubKey.Verify(data, sig)
	if err != nil || !ok {
		return "", fmt.Errorf("signature verification failed: %v", err)
	}
	return sig.TrustedComment, nil
}
