package enrollment

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"io"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func testCA(t *testing.T) (*x509.Certificate, *ecdsa.PrivateKey, []byte) {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	template := &x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: "test CA"},
		NotBefore:             now.Add(-time.Minute),
		NotAfter:              now.Add(time.Hour),
		IsCA:                  true,
		BasicConstraintsValid: true,
		KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature,
	}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	certificate, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatal(err)
	}
	return certificate, key, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
}

func signedCertificate(t *testing.T, ca *x509.Certificate, caKey *ecdsa.PrivateKey, publicKey any, server bool) []byte {
	t.Helper()
	now := time.Now()
	template := &x509.Certificate{
		SerialNumber: big.NewInt(2),
		Subject:      pkix.Name{CommonName: "test leaf"},
		NotBefore:    now.Add(-time.Minute),
		NotAfter:     now.Add(time.Hour),
		KeyUsage:     x509.KeyUsageDigitalSignature,
	}
	if server {
		template.ExtKeyUsage = []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}
		template.IPAddresses = []net.IP{net.ParseIP("127.0.0.1")}
	} else {
		template.ExtKeyUsage = []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth}
	}
	der, err := x509.CreateCertificate(rand.Reader, template, ca, publicKey, caKey)
	if err != nil {
		t.Fatal(err)
	}
	return der
}

func TestEnrollPinsCAAndWritesVersionedCredentials(t *testing.T) {
	ca, caKey, caPEM := testCA(t)
	serverKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	serverDER := signedCertificate(t, ca, caKey, &serverKey.PublicKey, true)
	serverKeyDER, _ := x509.MarshalPKCS8PrivateKey(serverKey)
	serverTLS, err := tls.X509KeyPair(
		pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: serverDER}),
		pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: serverKeyDER}),
	)
	if err != nil {
		t.Fatal(err)
	}

	server := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/api/v1/agent/enroll" {
			t.Errorf("unexpected request %s %s", r.Method, r.URL.Path)
		}
		body, _ := io.ReadAll(r.Body)
		var got request
		if err := json.Unmarshal(body, &got); err != nil {
			t.Fatal(err)
		}
		if got.Code != "cad1.test.fingerprint" || got.Hostname != "vm-test" {
			t.Errorf("unexpected enrollment identity: %+v", got)
		}
		csrBlock, _ := pem.Decode([]byte(got.CSRPEM))
		if csrBlock == nil {
			t.Fatal("CSR is not PEM")
		}
		csr, err := x509.ParseCertificateRequest(csrBlock.Bytes)
		if err != nil || csr.CheckSignature() != nil {
			t.Fatalf("invalid CSR: %v", err)
		}
		clientDER := signedCertificate(t, ca, caKey, csr.PublicKey, false)
		response := map[string]any{
			"host_id":                       "00000000-0000-0000-0000-000000000123",
			"server_url":                    "https://cadence.lan:8443",
			"token":                         "new-hmac-token",
			"client_certificate_pem":        string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: clientDER})),
			"client_certificate_expires_at": time.Now().Add(time.Hour).UTC(),
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(response)
	}))
	server.TLS = &tls.Config{Certificates: []tls.Certificate{serverTLS}, MinVersion: tls.VersionTLS12}
	server.StartTLS()
	defer server.Close()

	temporary := t.TempDir()
	caFile := filepath.Join(temporary, "server-ca.crt")
	if err := os.WriteFile(caFile, caPEM, 0o600); err != nil {
		t.Fatal(err)
	}
	result, err := Enroll(
		context.Background(), server.URL, caFile, "cad1.test.fingerprint\n", "vm-test", 5*time.Second,
	)
	if err != nil {
		t.Fatal(err)
	}
	credentialDir := filepath.Join(temporary, "credentials")
	if err := os.MkdirAll(credentialDir, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(
		filepath.Join(credentialDir, "agent.env"),
		[]byte("CADENCE_TOKEN=old\nCADENCE_ENABLE_UPGRADES=false\n"),
		0o600,
	); err != nil {
		t.Fatal(err)
	}
	if err := WriteCredentials(credentialDir, caFile, result); err != nil {
		t.Fatal(err)
	}
	environment, err := os.ReadFile(filepath.Join(credentialDir, "agent.env"))
	if err != nil {
		t.Fatal(err)
	}
	envText := string(environment)
	for _, expected := range []string{
		"CADENCE_SERVER_URL=https://cadence.lan:8443",
		"CADENCE_TOKEN=new-hmac-token",
		"CADENCE_CLIENT_CERT_FILE=",
		"CADENCE_CLIENT_KEY_FILE=",
		"CADENCE_SERVER_CA_FILE=" + caFile,
		"CADENCE_ENABLE_UPGRADES=false",
	} {
		if !strings.Contains(envText, expected) {
			t.Errorf("agent.env missing %q:\n%s", expected, envText)
		}
	}
	if strings.Contains(envText, "CADENCE_TOKEN=old") {
		t.Error("old token was retained")
	}
	for _, pattern := range []string{"client-*.crt", "client-*.key"} {
		matches, _ := filepath.Glob(filepath.Join(credentialDir, pattern))
		if len(matches) != 1 {
			t.Fatalf("%s matches = %v", pattern, matches)
		}
		info, _ := os.Stat(matches[0])
		if info.Mode().Perm() != 0o600 {
			t.Errorf("%s mode = %o", matches[0], info.Mode().Perm())
		}
	}
}
