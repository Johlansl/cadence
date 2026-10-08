// Package upgrade holds the pure local decision logic for an agent_upgrade
// job (6B): is the payload strictly valid, how does the target compare to
// the running version, where would the artifacts come from, and is this
// process eligible to self-upgrade.
//
// This package is dormant decision support for PR6. It performs no I/O
// beyond resolving the current executable path, and it mutates nothing:
// no download, no signature check, no file replacement, no subprocess.
// The real dispatcher (cmd/agent runJob) still refuses agent_upgrade as an
// unsupported job type; nothing here is wired into it yet.
package upgrade

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"strconv"
	"strings"
)

// maxParamsBytes bounds the params blob DecodeParams will inspect. A closed
// {"target_version": "N.N.N"} contract never legitimately approaches this.
const maxParamsBytes = 4096

// Params is the closed agent_upgrade contract: exactly one key, a strict
// N.N.N target. Anything else (url, checksum, pubkey, path, command,
// downgrade flag, ...) is rejected, never silently ignored.
type Params struct {
	TargetVersion string `json:"target_version"`
}

// DecodeParams decodes a job's params into the closed Params contract.
// Fail-closed: unknown keys, duplicate keys, a missing or non-string
// target, trailing garbage, an overlong blob, or malformed JSON all error.
//
// encoding/json silently accepts duplicate keys (last wins), so the key set
// is first walked with a decoder token scan: exactly one occurrence of
// exactly "target_version", then end of object, then EOF.
func DecodeParams(raw json.RawMessage) (Params, error) {
	var zero Params
	if len(raw) == 0 || len(raw) > maxParamsBytes {
		return zero, fmt.Errorf("agent_upgrade params must be 1..%d bytes", maxParamsBytes)
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	if !consumeDelim(dec, '{') {
		return zero, fmt.Errorf("agent_upgrade params must be a JSON object")
	}
	seen := false
	for dec.More() {
		key, err := dec.Token()
		if err != nil {
			return zero, fmt.Errorf("agent_upgrade params are malformed: %v", err)
		}
		name, ok := key.(string)
		if !ok || name != "target_version" || seen {
			return zero, fmt.Errorf("agent_upgrade params must be exactly {'target_version': ...}")
		}
		seen = true
		var skip json.RawMessage
		if err := dec.Decode(&skip); err != nil {
			return zero, fmt.Errorf("agent_upgrade params are malformed: %v", err)
		}
	}
	if !consumeDelim(dec, '}') {
		return zero, fmt.Errorf("agent_upgrade params are malformed")
	}
	if _, err := dec.Token(); err != io.EOF {
		return zero, fmt.Errorf("agent_upgrade params carry trailing data")
	}
	var p Params
	strict := json.NewDecoder(bytes.NewReader(raw))
	strict.DisallowUnknownFields()
	if err := strict.Decode(&p); err != nil {
		return zero, fmt.Errorf("agent_upgrade params are malformed: %v", err)
	}
	if _, err := ParseTarget(p.TargetVersion); err != nil {
		return zero, err
	}
	return p, nil
}

// consumeDelim reads one token and reports whether it is the expected JSON
// delimiter.
func consumeDelim(dec *json.Decoder, want json.Delim) bool {
	tok, err := dec.Token()
	if err != nil {
		return false
	}
	d, ok := tok.(json.Delim)
	return ok && d == want
}

// Version is three dot-separated integer components.
type Version struct {
	Major, Minor, Patch int
}

// String renders the canonical form: no leading zeros, no suffix.
func (v Version) String() string {
	return strconv.Itoa(v.Major) + "." + strconv.Itoa(v.Minor) + "." + strconv.Itoa(v.Patch)
}

// compare orders versions by (major, minor, patch): -1, 0 or +1.
func (v Version) compare(o Version) int {
	if v.Major != o.Major {
		if v.Major < o.Major {
			return -1
		}
		return 1
	}
	if v.Minor != o.Minor {
		if v.Minor < o.Minor {
			return -1
		}
		return 1
	}
	if v.Patch != o.Patch {
		if v.Patch < o.Patch {
			return -1
		}
		return 1
	}
	return 0
}

// maxComponentDigits bounds one numeric component. Three dot-separated
// digit runs; strconv then rejects anything past int range.
const maxComponentDigits = 19

// parseTriple parses canonical N.N.N: three non-empty all-digit components
// in canonical decimal form (no leading zeros, except a component that is
// exactly "0"), nothing else. One release has exactly one spelling: a job
// naming "01.02.003" is refused rather than silently read as 1.2.3, so the
// derived artifact path and the -version proof can never disagree with the
// params text.
func parseTriple(s string) (Version, error) {
	var zero Version
	parts := strings.Split(s, ".")
	if len(parts) != 3 {
		return zero, fmt.Errorf("version %q is not strict N.N.N", s)
	}
	nums := make([]int, 3)
	for i, part := range parts {
		if part == "" || len(part) > maxComponentDigits {
			return zero, fmt.Errorf("version %q is not strict N.N.N", s)
		}
		if len(part) > 1 && part[0] == '0' {
			return zero, fmt.Errorf("version %q is not canonical N.N.N", s)
		}
		for _, r := range part {
			if r < '0' || r > '9' {
				return zero, fmt.Errorf("version %q is not strict N.N.N", s)
			}
		}
		n, err := strconv.Atoi(part)
		if err != nil {
			return zero, fmt.Errorf("version %q is not strict N.N.N", s)
		}
		nums[i] = n
	}
	return Version{nums[0], nums[1], nums[2]}, nil
}

// ParseTarget parses the job's target version: canonical N.N.N only, no
// suffix, no whitespace, no 'v' prefix, no leading zeros.
func ParseTarget(s string) (Version, error) {
	if s == "" || len(s) > 3*(maxComponentDigits+1) {
		return Version{}, fmt.Errorf("target_version %q is not strict N.N.N", s)
	}
	v, err := parseTriple(s)
	if err != nil {
		return Version{}, fmt.Errorf("target_version %q is not strict N.N.N", s)
	}
	return v, nil
}

// ParseCurrent parses the running agent's own version: canonical N.N.N,
// optionally followed by "-" and a non-empty suffix (release builds are
// plain N.N.N; git-describe builds look like 0.14.0-51-ge946197). Only the
// three leading integers order; the suffix is dropped.
func ParseCurrent(s string) (Version, error) {
	if s == "" || len(s) > 3*(maxComponentDigits+1)+256 {
		return Version{}, fmt.Errorf("current version %q is invalid", s)
	}
	head := s
	if i := strings.IndexByte(s, '-'); i >= 0 {
		if suffix := s[i+1:]; suffix == "" {
			return Version{}, fmt.Errorf("current version %q is invalid", s)
		} else {
			head = s[:i]
		}
	}
	v, err := parseTriple(head)
	if err != nil {
		return Version{}, fmt.Errorf("current version %q is invalid", s)
	}
	return v, nil
}

// Outcome is the closed comparison of a target against the running agent.
// The zero value is Invalid: fail-closed by construction.
type Outcome int

const (
	// Invalid means the target or the current version failed to parse.
	Invalid Outcome = iota
	// TargetNewer means the target is strictly above the running version.
	TargetNewer
	// TargetEqual means target and running version share the same triple.
	TargetEqual
	// TargetOlder means the target is strictly below the running version.
	TargetOlder
)

// String renders the outcome for logs and failure summaries.
func (o Outcome) String() string {
	switch o {
	case TargetNewer:
		return "newer"
	case TargetEqual:
		return "equal"
	case TargetOlder:
		return "older"
	default:
		return "invalid"
	}
}

// Compare parses current (N.N.N plus optional describe suffix) and target
// (strict N.N.N) and orders them on the three integers alone. Any parse
// failure yields Invalid.
func Compare(current, target string) Outcome {
	cur, err := ParseCurrent(current)
	if err != nil {
		return Invalid
	}
	tgt, err := ParseTarget(target)
	if err != nil {
		return Invalid
	}
	switch cur.compare(tgt) {
	case -1:
		return TargetNewer
	case 1:
		return TargetOlder
	default:
		return TargetEqual
	}
}

// Decision is the local policy verdict for an upgrade job. The zero value
// refuses: fail-closed by construction.
type Decision int

const (
	// RefusedInvalid rejects an unparsable target or current version.
	RefusedInvalid Decision = iota
	// Eligible means PR6 may proceed (subject to the upgrades kill-switch).
	Eligible
	// AlreadyAtTarget is the logical no-op: nothing to install.
	AlreadyAtTarget
	// RefusedDowngrade rejects a target below the running version.
	// Remote downgrade has no flag, no exception, no override.
	RefusedDowngrade
)

// String renders the decision for logs and failure summaries.
func (d Decision) String() string {
	switch d {
	case Eligible:
		return "eligible"
	case AlreadyAtTarget:
		return "already_at_target"
	case RefusedDowngrade:
		return "refused_downgrade"
	default:
		return "refused_invalid"
	}
}

// Decide maps a comparison outcome to the local policy verdict.
func Decide(o Outcome) Decision {
	switch o {
	case TargetNewer:
		return Eligible
	case TargetEqual:
		return AlreadyAtTarget
	case TargetOlder:
		return RefusedDowngrade
	default:
		return RefusedInvalid
	}
}

// EligibleForUpgrade reports whether PR6 may act on the decision: the local
// policy must say Eligible and the operator kill-switch
// (CADENCE_ENABLE_UPGRADES, passed in by the caller so this package holds no
// config of its own) must be on.
func EligibleForUpgrade(d Decision, upgradesEnabled bool) bool {
	return d == Eligible && upgradesEnabled
}
