// Package enrollment performs the one-time, server-CA-pinned credential
// exchange. The agent private key is generated and retained on the host.
package enrollment

import (
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"
)

const maxResponseBytes = 256 * 1024

type request struct {
	Code     string `json:"code"`
	Hostname string `json:"hostname"`
	CSRPEM   string `json:"csr_pem"`
}

// Result contains the credential bundle returned exactly once by the server.
type Result struct {
	HostID                     string    `json:"host_id"`
	ServerURL                  string    `json:"server_url"`
	Token                      string    `json:"token"`
	ClientCertificatePEM       string    `json:"client_certificate_pem"`
	ClientCertificateExpiresAt time.Time `json:"client_certificate_expires_at"`
	PrivateKeyPEM              []byte    `json:"-"`
}

func rootPool(caFile string) (*x509.CertPool, error) {
	data, err := os.ReadFile(caFile)
	if err != nil {
		return nil, fmt.Errorf("reading pinned server CA: %w", err)
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(data) {
		return nil, fmt.Errorf("pinned server CA contains no certificate")
	}
	return pool, nil
}

// NewCSR creates a P-256 key and CSR. The caller must keep the returned key
// private and write it only after the server returns a matching certificate.
func NewCSR(hostname string) (string, []byte, error) {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return "", nil, fmt.Errorf("generating client private key: %w", err)
	}
	der, err := x509.CreateCertificateRequest(rand.Reader, &x509.CertificateRequest{
		Subject: pkix.Name{CommonName: hostname},
	}, key)
	if err != nil {
		return "", nil, fmt.Errorf("creating certificate signing request: %w", err)
	}
	keyDER, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		return "", nil, fmt.Errorf("encoding client private key: %w", err)
	}
	csrPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE REQUEST", Bytes: der})
	keyPEM := pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: keyDER})
	return string(csrPEM), keyPEM, nil
}

// Enroll sends the one-time code only after TLS has validated the already
// fingerprint-checked server CA supplied by the trusted bootstrap prelude.
func Enroll(ctx context.Context, serverURL, caFile, code, hostname string, timeout time.Duration) (Result, error) {
	base := strings.TrimRight(strings.TrimSpace(serverURL), "/")
	parsed, err := url.Parse(base)
	if err != nil || parsed.Scheme != "https" || parsed.Host == "" {
		return Result{}, fmt.Errorf("enrollment server URL must be a valid https URL")
	}
	if strings.TrimSpace(code) == "" {
		return Result{}, fmt.Errorf("enrollment code is empty")
	}
	if strings.TrimSpace(hostname) == "" {
		return Result{}, fmt.Errorf("hostname is empty")
	}

	csrPEM, keyPEM, err := NewCSR(hostname)
	if err != nil {
		return Result{}, err
	}
	body, err := json.Marshal(request{Code: strings.TrimSpace(code), Hostname: hostname, CSRPEM: csrPEM})
	if err != nil {
		return Result{}, fmt.Errorf("encoding enrollment request: %w", err)
	}
	roots, err := rootPool(caFile)
	if err != nil {
		return Result{}, err
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.TLSClientConfig = &tls.Config{RootCAs: roots, MinVersion: tls.VersionTLS12}
	defer transport.CloseIdleConnections()
	httpClient := &http.Client{Timeout: timeout, Transport: transport}
	req, err := http.NewRequestWithContext(
		ctx, http.MethodPost, base+"/api/v1/agent/enroll", bytes.NewReader(body),
	)
	if err != nil {
		return Result{}, fmt.Errorf("creating enrollment request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := httpClient.Do(req)
	if err != nil {
		return Result{}, fmt.Errorf("enrollment request: %w", err)
	}
	defer resp.Body.Close()
	payload, err := io.ReadAll(io.LimitReader(resp.Body, maxResponseBytes+1))
	if err != nil {
		return Result{}, fmt.Errorf("reading enrollment response: %w", err)
	}
	if len(payload) > maxResponseBytes {
		return Result{}, fmt.Errorf("enrollment response exceeds %d bytes", maxResponseBytes)
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return Result{}, fmt.Errorf("enrollment server returned %s: %s", resp.Status, bytes.TrimSpace(payload))
	}
	var result Result
	if err := json.Unmarshal(payload, &result); err != nil {
		return Result{}, fmt.Errorf("decoding enrollment response: %w", err)
	}
	if result.HostID == "" || result.ServerURL == "" || result.Token == "" || result.ClientCertificatePEM == "" {
		return Result{}, fmt.Errorf("enrollment response is missing credentials")
	}
	resultServer, err := url.Parse(result.ServerURL)
	if err != nil || resultServer.Scheme != "https" || resultServer.Host == "" {
		return Result{}, fmt.Errorf("enrollment response contains an invalid agent server URL")
	}
	if result.ClientCertificateExpiresAt.IsZero() || !result.ClientCertificateExpiresAt.After(time.Now()) {
		return Result{}, fmt.Errorf("enrollment response contains an expired client certificate")
	}
	if _, err := tls.X509KeyPair([]byte(result.ClientCertificatePEM), keyPEM); err != nil {
		return Result{}, fmt.Errorf("issued certificate does not match generated private key: %w", err)
	}
	result.PrivateKeyPEM = keyPEM
	return result, nil
}

func atomicWrite(path string, data []byte, mode os.FileMode) error {
	temporary, err := os.CreateTemp(filepath.Dir(path), ".cadence-credential-*")
	if err != nil {
		return err
	}
	temporaryPath := temporary.Name()
	defer func() { _ = os.Remove(temporaryPath) }()
	if err := temporary.Chmod(mode); err != nil {
		temporary.Close()
		return err
	}
	if _, err := temporary.Write(data); err != nil {
		temporary.Close()
		return err
	}
	if err := temporary.Sync(); err != nil {
		temporary.Close()
		return err
	}
	if err := temporary.Close(); err != nil {
		return err
	}
	return os.Rename(temporaryPath, path)
}

func operationalSettings(path string) []string {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	allowed := map[string]bool{
		"CADENCE_RUN_APT_UPDATE":       true,
		"CADENCE_ENABLE_UPGRADES":      true,
		"CADENCE_ENABLE_REBOOT":        true,
		"CADENCE_HTTP_TIMEOUT_SECONDS": true,
	}
	var kept []string
	for _, line := range strings.Split(string(data), "\n") {
		key, _, found := strings.Cut(line, "=")
		if found && allowed[key] {
			kept = append(kept, line)
		}
	}
	return kept
}

// WriteCredentials writes versioned key/certificate files first and switches
// agent.env last. An interrupted write therefore leaves the previous bundle
// selected rather than pairing a new key with an old certificate.
func WriteCredentials(directory, serverCAFile string, result Result) error {
	if result.PrivateKeyPEM == nil {
		return fmt.Errorf("enrollment result has no private key")
	}
	block, _ := pem.Decode([]byte(result.ClientCertificatePEM))
	if block == nil || block.Type != "CERTIFICATE" {
		return fmt.Errorf("issued client certificate is invalid")
	}
	if strings.ContainsAny(result.ServerURL+result.Token+serverCAFile, "\r\n") {
		return fmt.Errorf("credential values must not contain newlines")
	}
	fingerprint := sha256.Sum256(block.Bytes)
	suffix := hex.EncodeToString(fingerprint[:8])
	if err := os.MkdirAll(directory, 0o700); err != nil {
		return fmt.Errorf("creating credential directory: %w", err)
	}
	if err := os.Chmod(directory, 0o700); err != nil {
		return fmt.Errorf("securing credential directory: %w", err)
	}
	certPath := filepath.Join(directory, "client-"+suffix+".crt")
	keyPath := filepath.Join(directory, "client-"+suffix+".key")
	if err := atomicWrite(keyPath, result.PrivateKeyPEM, 0o600); err != nil {
		return fmt.Errorf("writing client private key: %w", err)
	}
	if err := atomicWrite(certPath, []byte(result.ClientCertificatePEM), 0o600); err != nil {
		return fmt.Errorf("writing client certificate: %w", err)
	}
	envPath := filepath.Join(directory, "agent.env")
	lines := []string{
		"CADENCE_SERVER_URL=" + result.ServerURL,
		"CADENCE_TOKEN=" + result.Token,
		"CADENCE_CLIENT_CERT_FILE=" + certPath,
		"CADENCE_CLIENT_KEY_FILE=" + keyPath,
		"CADENCE_SERVER_CA_FILE=" + serverCAFile,
	}
	operational := operationalSettings(envPath)
	if len(operational) == 0 {
		operational = []string{"CADENCE_RUN_APT_UPDATE=true"}
	}
	lines = append(lines, operational...)
	if err := atomicWrite(envPath, []byte(strings.Join(lines, "\n")+"\n"), 0o600); err != nil {
		return fmt.Errorf("writing agent environment: %w", err)
	}
	return nil
}
