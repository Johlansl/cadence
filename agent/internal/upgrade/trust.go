package upgrade

import (
	"fmt"
	"strings"

	minisign "github.com/jedisct1/go-minisign"
)

// activePubKeyText is the Minisign root of trust used to sign every release
// in CI (agent/minisign.pub), compiled into the binary as a literal. A
// rotation edits the file and this literal together; TestTrustedKeyMatchesRepoKey
// fails CI on any drift between the two. (A go:embed was considered and
// rejected: embed patterns cannot name parent directories, so the literal
// plus the sync test is the smaller mechanism.)
const activePubKeyText = `untrusted comment: minisign public key C9EDE89E1DEEED37
RWQ37e4dnujtyfC7Twy6ItYBgiBIG/Fl/btaG6f3Pg8tUd2tDzQtEB+Q`

// backupPubKeyText is the offline backup root of trust (agent/minisign-backup.pub),
// held only by the maintainer, outside CI. It exists to sign an emergency
// release if the active key is ever lost or compromised: every agent trusts
// it already, so such a release verifies on hosts that never saw the
// incident. Kept in sync with agent/minisign-backup.pub the same way as
// activePubKeyText.
const backupPubKeyText = `untrusted comment: minisign public key 2167C9939EC0F4FC
RWT89MCek8lnIQSGl1pzce0uAEqbX+AdYBzpzhRebFg7LszYq0eOy+Ks`

// trustedKeys is the root of trust for agent upgrades: every key in this set
// is accepted, tried in order. The server, the job, and every runtime
// config are unable to replace it: verification below parses these
// constants and nothing else. In-package tests reassign trustedKeys (a
// test-only seam, never a production knob) to exercise verification against
// test keys.
var trustedKeys = mustBuildTrustedKeys(activePubKeyText, backupPubKeyText)

func mustParsePubKey(text string) minisign.PublicKey {
	key, err := parsePubKey(text)
	if err != nil {
		panic("upgrade: embedded minisign public key is invalid: " + err.Error())
	}
	return key
}

// mustBuildTrustedKeys parses every embedded key text and panics if any
// two parsed keys share a key ID. A trust set is only as strong as its
// members being independent; a duplicate (for instance a rotation that
// accidentally commits the same key twice) must fail loud at startup, not
// silently collapse to a single effective key.
func mustBuildTrustedKeys(texts ...string) []minisign.PublicKey {
	keys := make([]minisign.PublicKey, len(texts))
	for i, text := range texts {
		keys[i] = mustParsePubKey(text)
	}
	for i := range keys {
		for j := i + 1; j < len(keys); j++ {
			if keys[i].KeyId == keys[j].KeyId {
				panic("upgrade: trusted keys are not distinct, found a duplicate key ID")
			}
		}
	}
	return keys
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

// verifySignature checks the downloaded bytes against a .minisig file,
// trying every key in the embedded trust set in order and accepting on the
// first match. The library call verifies the message signature, the key
// identifier binding, and the trusted comment's global signature; a key
// whose id does not match the signature simply fails that one attempt
// (minisign.PublicKey.Verify returns false, error, never a panic or shared
// state) and the next key is tried. Refuses only once every key has been
// tried and none verified, or the signature file itself is malformed. The
// trusted comment is returned for logging only: it is never version truth.
func verifySignature(data, sigText []byte) (trustedComment string, err error) {
	if len(sigText) == 0 || len(sigText) > maxMetaBytes {
		return "", fmt.Errorf("signature file has an impossible size")
	}
	sig, err := minisign.DecodeSignature(string(sigText))
	if err != nil {
		return "", fmt.Errorf("signature file is malformed: %v", err)
	}
	for _, key := range trustedKeys {
		if ok, _ := key.Verify(data, sig); ok {
			return sig.TrustedComment, nil
		}
	}
	return "", fmt.Errorf("signature verification failed: no trusted key matched")
}
