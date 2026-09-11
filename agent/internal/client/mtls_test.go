package client

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"io"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"

	"cadence/agent/internal/report"
)

type mtlsFixture struct {
	ca        *x509.Certificate
	caKey     *ecdsa.PrivateKey
	caPEM     []byte
	clientPEM []byte
	clientKey []byte
}

func newMTLSFixture(t *testing.T) mtlsFixture {
	t.Helper()
	caKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	caTemplate := &x509.Certificate{
		SerialNumber: big.NewInt(10),
		Subject:      pkix.Name{CommonName: "mTLS test CA"},
		NotBefore:    now.Add(-time.Minute), NotAfter: now.Add(time.Hour),
		IsCA: true, BasicConstraintsValid: true,
		KeyUsage: x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature,
	}
	caDER, err := x509.CreateCertificate(rand.Reader, caTemplate, caTemplate, &caKey.PublicKey, caKey)
	if err != nil {
		t.Fatal(err)
	}
	ca, err := x509.ParseCertificate(caDER)
	if err != nil {
		t.Fatal(err)
	}
	clientKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	clientTemplate := &x509.Certificate{
		SerialNumber: big.NewInt(11),
		Subject:      pkix.Name{CommonName: "agent"},
		NotBefore:    now.Add(-time.Minute), NotAfter: now.Add(time.Hour),
		KeyUsage:    x509.KeyUsageDigitalSignature,
		ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth},
	}
	clientDER, err := x509.CreateCertificate(
		rand.Reader, clientTemplate, ca, &clientKey.PublicKey, caKey,
	)
	if err != nil {
		t.Fatal(err)
	}
	clientKeyDER, err := x509.MarshalPKCS8PrivateKey(clientKey)
	if err != nil {
		t.Fatal(err)
	}
	return mtlsFixture{
		ca: ca, caKey: caKey,
		caPEM:     pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: caDER}),
		clientPEM: pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: clientDER}),
		clientKey: pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: clientKeyDER}),
	}
}

func (fixture mtlsFixture) serverCertificate(t *testing.T) tls.Certificate {
	t.Helper()
	serverKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	template := &x509.Certificate{
		SerialNumber: big.NewInt(12),
		Subject:      pkix.Name{CommonName: "server"},
		NotBefore:    now.Add(-time.Minute), NotAfter: now.Add(time.Hour),
		KeyUsage:    x509.KeyUsageDigitalSignature,
		ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
		IPAddresses: []net.IP{net.ParseIP("127.0.0.1")},
	}
	der, err := x509.CreateCertificate(
		rand.Reader, template, fixture.ca, &serverKey.PublicKey, fixture.caKey,
	)
	if err != nil {
		t.Fatal(err)
	}
	keyDER, err := x509.MarshalPKCS8PrivateKey(serverKey)
	if err != nil {
		t.Fatal(err)
	}
	certificate, err := tls.X509KeyPair(
		pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}),
		pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: keyDER}),
	)
	if err != nil {
		t.Fatal(err)
	}
	return certificate
}

func TestNewMTLSAddsClientCertificateAndKeepsHMAC(t *testing.T) {
	fixture := newMTLSFixture(t)
	roots := x509.NewCertPool()
	roots.AddCert(fixture.ca)
	server := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.TLS == nil || len(r.TLS.PeerCertificates) == 0 {
			t.Fatal("request has no verified client certificate")
		}
		body, _ := io.ReadAll(r.Body)
		verifySignedHeaders(t, r, "tok", body)
		_, _ = w.Write([]byte(`{"job":null}`))
	}))
	server.TLS = &tls.Config{
		Certificates: []tls.Certificate{fixture.serverCertificate(t)},
		ClientAuth:   tls.RequireAndVerifyClientCert,
		ClientCAs:    roots,
		MinVersion:   tls.VersionTLS12,
	}
	server.StartTLS()
	defer server.Close()

	directory := t.TempDir()
	certFile := filepath.Join(directory, "client.crt")
	keyFile := filepath.Join(directory, "client.key")
	caFile := filepath.Join(directory, "ca.crt")
	for path, data := range map[string][]byte{
		certFile: fixture.clientPEM,
		keyFile:  fixture.clientKey,
		caFile:   fixture.caPEM,
	} {
		if err := os.WriteFile(path, data, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	c, err := NewMTLS(server.URL, "tok", certFile, keyFile, caFile, 5*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := c.SendReport(context.Background(), report.Report{}); err != nil {
		t.Fatal(err)
	}
}

func TestNewMTLSRejectsMissingCredentialFiles(t *testing.T) {
	if _, err := NewMTLS("https://cadence.lan:8443", "tok", "/missing", "/missing", "/missing", time.Second); err == nil {
		t.Fatal("expected an error for missing credentials")
	}
}
