package renewal

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"math/big"
	"os"
	"path/filepath"
	"testing"
	"time"

	"cadence/agent/internal/config"
)

func writeCertificate(t *testing.T, path string, expiresAt time.Time) {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	template := &x509.Certificate{
		SerialNumber: big.NewInt(1),
		Subject:      pkix.Name{CommonName: "agent"},
		NotBefore:    time.Now().Add(-time.Hour),
		NotAfter:     expiresAt,
	}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(
		path, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}), 0o600,
	); err != nil {
		t.Fatal(err)
	}
}

func TestNeedsRenewalAtFourteenDays(t *testing.T) {
	now := time.Now()
	for name, testCase := range map[string]struct {
		expiresAt time.Time
		want      bool
	}{
		"outside window": {now.Add(15 * 24 * time.Hour), false},
		"inside window":  {now.Add(13 * 24 * time.Hour), true},
		"expired":        {now.Add(-time.Minute), true},
	} {
		t.Run(name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "client.crt")
			writeCertificate(t, path, testCase.expiresAt)
			got, err := NeedsRenewal(path, now)
			if err != nil {
				t.Fatal(err)
			}
			if got != testCase.want {
				t.Fatalf("NeedsRenewal = %v, want %v", got, testCase.want)
			}
		})
	}
}

func TestRunIfNeededNoticesAnotherProcessSwitchedCredentials(t *testing.T) {
	directory := t.TempDir()
	oldCert := filepath.Join(directory, "client-old.crt")
	writeCertificate(t, oldCert, time.Now().Add(time.Hour))
	if err := os.WriteFile(
		filepath.Join(directory, "agent.env"),
		[]byte("CADENCE_CLIENT_CERT_FILE="+filepath.Join(directory, "client-new.crt")+"\n"),
		0o600,
	); err != nil {
		t.Fatal(err)
	}
	renewed, err := RunIfNeeded(context.Background(), config.Config{ClientCertFile: oldCert})
	if err != nil {
		t.Fatal(err)
	}
	if renewed {
		t.Fatal("stale process should not renew after agent.env switched")
	}
}
