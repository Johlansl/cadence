package client

// TEST KEY ONLY — never use in production.
//
// Writes backend/tests/testdata/signed_request.json: one request signed by
// the real Client.sign prod path, consumed by the backend contract test
// (backend/tests/test_agent_contract.py) through the real _auth_signed.
// Static file, committed once; the Python side pins these exact bytes, so a
// legitimate signature-chain change must regenerate it (see _regen in the
// JSON) and commit the result.

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// TEST KEY ONLY — never use in production.
const goldenTestKey = "cadence-test-golden-key-01-do-not-use-in-prod"

const goldenBody = `{"golden":true}`

const goldenMethod = "POST"

const goldenPath = "/api/v1/reports"

type goldenVector struct {
	Comment    string `json:"_comment"`
	Regen      string `json:"_regen"`
	Key        string `json:"key"`
	Method     string `json:"method"`
	Path       string `json:"path"`
	Timestamp  string `json:"timestamp"`
	TokenHash  string `json:"token_hash"`
	Signature  string `json:"signature"`
	BodySHA256 string `json:"body_sha256"`
	Body       string `json:"body"`
}

func TestWriteSignedGolden(t *testing.T) {
	if os.Getenv("CADENCE_WRITE_GOLDEN") != "1" {
		t.Skip("set CADENCE_WRITE_GOLDEN=1 to (re)write the signed-request golden vector")
	}
	body := []byte(goldenBody)
	tokenHash, timestamp, signature := New("http://127.0.0.1:8000", goldenTestKey, time.Second).sign(goldenMethod, goldenPath, body)
	bodySum := sha256.Sum256(body)
	vec := goldenVector{
		Comment:    "TEST KEY ONLY — never use in production. Signed by the real agent Client.sign; verified by backend _auth_signed.",
		Regen:      "cd agent && CADENCE_WRITE_GOLDEN=1 go test ./internal/client/ -run TestWriteSignedGolden -v",
		Key:        goldenTestKey,
		Method:     goldenMethod,
		Path:       goldenPath,
		Timestamp:  timestamp,
		TokenHash:  tokenHash,
		Signature:  signature,
		BodySHA256: hex.EncodeToString(bodySum[:]),
		Body:       goldenBody,
	}
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	enc.SetIndent("", "  ")
	if err := enc.Encode(vec); err != nil {
		t.Fatal(err)
	}
	out := buf.Bytes()
	dest := filepath.Join("..", "..", "..", "backend", "tests", "testdata", "signed_request.json")
	if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(dest, out, 0o644); err != nil {
		t.Fatal(err)
	}
	t.Logf("wrote %s", dest)
}
