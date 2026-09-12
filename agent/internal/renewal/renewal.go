// Package renewal rotates an enrolled agent certificate before it expires.
package renewal

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/pem"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"cadence/agent/internal/client"
	"cadence/agent/internal/config"
	"cadence/agent/internal/enrollment"
)

const RenewBefore = 14 * 24 * time.Hour

// NeedsRenewal checks the leaf certificate selected by agent.env.
func NeedsRenewal(certFile string, now time.Time) (bool, error) {
	data, err := os.ReadFile(certFile)
	if err != nil {
		return false, fmt.Errorf("reading client certificate: %w", err)
	}
	block, _ := pem.Decode(data)
	if block == nil || block.Type != "CERTIFICATE" {
		return false, fmt.Errorf("client certificate is not valid PEM")
	}
	certificate, err := x509.ParseCertificate(block.Bytes)
	if err != nil {
		return false, fmt.Errorf("parsing client certificate: %w", err)
	}
	return !certificate.NotAfter.After(now.Add(RenewBefore)), nil
}

func selectedCertificate(envFile, fallback string) string {
	data, err := os.ReadFile(envFile)
	if err != nil {
		return fallback
	}
	for _, line := range strings.Split(string(data), "\n") {
		if value, found := strings.CutPrefix(line, "CADENCE_CLIENT_CERT_FILE="); found {
			return value
		}
	}
	return fallback
}

// RunIfNeeded serializes renewal across concurrent systemd one-shot services.
// The previous certificate remains active server-side and the current process
// may finish on it; the next process reads the newly switched agent.env.
func RunIfNeeded(ctx context.Context, cfg config.Config) (bool, error) {
	if cfg.ClientCertFile == "" {
		return false, nil
	}
	directory := filepath.Dir(cfg.ClientCertFile)
	lock, err := os.OpenFile(filepath.Join(directory, ".renew.lock"), os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return false, fmt.Errorf("opening certificate renewal lock: %w", err)
	}
	defer lock.Close()
	if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX); err != nil {
		return false, fmt.Errorf("locking certificate renewal: %w", err)
	}
	defer func() { _ = syscall.Flock(int(lock.Fd()), syscall.LOCK_UN) }()

	// Another agent process may have renewed while this one waited for the
	// lock. Its inherited environment is stale, so inspect the switched file.
	if selectedCertificate(filepath.Join(directory, "agent.env"), cfg.ClientCertFile) != cfg.ClientCertFile {
		return false, nil
	}
	needed, err := NeedsRenewal(cfg.ClientCertFile, time.Now())
	if err != nil || !needed {
		return false, err
	}

	hostname, err := os.Hostname()
	if err != nil {
		return false, fmt.Errorf("reading hostname for certificate renewal: %w", err)
	}
	csrPEM, keyPEM, err := enrollment.NewCSR(hostname)
	if err != nil {
		return false, err
	}
	httpClient, err := client.NewMTLS(
		cfg.ServerURL,
		cfg.Token,
		cfg.ClientCertFile,
		cfg.ClientKeyFile,
		cfg.ServerCAFile,
		cfg.HTTPTimeout,
	)
	if err != nil {
		return false, err
	}
	renewed, err := httpClient.RenewCertificate(ctx, csrPEM)
	if err != nil {
		return false, err
	}
	if _, err := tls.X509KeyPair([]byte(renewed.ClientCertificatePEM), keyPEM); err != nil {
		return false, fmt.Errorf("renewed certificate does not match generated private key: %w", err)
	}
	result := enrollment.Result{
		ServerURL:                  cfg.ServerURL,
		Token:                      cfg.Token,
		ClientCertificatePEM:       renewed.ClientCertificatePEM,
		ClientCertificateExpiresAt: renewed.ClientCertificateExpiresAt,
		PrivateKeyPEM:              keyPEM,
	}
	if err := enrollment.WriteCredentials(directory, cfg.ServerCAFile, result); err != nil {
		return false, err
	}
	return true, nil
}
