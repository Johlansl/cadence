package upgrade

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestDecodeParamsHappy(t *testing.T) {
	p, err := DecodeParams(json.RawMessage(`{"target_version":"0.15.0"}`))
	if err != nil {
		t.Fatal(err)
	}
	if p.TargetVersion != "0.15.0" {
		t.Fatalf("TargetVersion = %q", p.TargetVersion)
	}
	// Whitespace shape does not matter.
	p, err = DecodeParams(json.RawMessage("{\n  \"target_version\" : \"1.2.3\"\n}\n"))
	if err != nil {
		t.Fatal(err)
	}
	if p.TargetVersion != "1.2.3" {
		t.Fatalf("TargetVersion = %q", p.TargetVersion)
	}
}

func TestDecodeParamsRejectsTrapKeys(t *testing.T) {
	traps := []string{
		"url", "sha256", "checksum", "pubkey", "path",
		"command", "cmd", "args", "base_url", "allow_downgrade",
	}
	for _, trap := range traps {
		raw := json.RawMessage(`{"target_version":"0.15.0","` + trap + `":"x"}`)
		if _, err := DecodeParams(raw); err == nil {
			t.Errorf("trap key %q accepted", trap)
		}
	}
}

func TestDecodeParamsRejectsMalformed(t *testing.T) {
	cases := map[string]string{
		"empty":            ``,
		"blank":            `   `,
		"empty object":     `{}`,
		"array":            `[]`,
		"string":           `"0.15.0"`,
		"null":             `null`,
		"number":           `15`,
		"truncated":        `{"target_version":"0.15.0"`,
		"not json":         `{oops`,
		"missing target":   `{"foo":"0.15.0"}`,
		"null target":      `{"target_version":null}`,
		"numeric target":   `{"target_version":15}`,
		"bool target":      `{"target_version":true}`,
		"array target":     `{"target_version":["0.15.0"]}`,
		"object target":    `{"target_version":{"v":"0.15.0"}}`,
		"empty target":     `{"target_version":""}`,
		"bad target":       `{"target_version":"1.2"}`,
		"duplicate keys":   `{"target_version":"0.15.0","target_version":"0.16.0"}`,
		"duplicate traps":  `{"target_version":"0.15.0","url":"a","url":"b"}`,
		"trailing object":  `{"target_version":"0.15.0"} {}`,
		"trailing string":  `{"target_version":"0.15.0"} "x"`,
		"case differs":     `{"Target_Version":"0.15.0"}`,
		"nested duplicate": `{"target_version":"0.15.0","x":{"target_version":"0.15.0"}}`,
		"overlong":         `{"target_version":"` + strings.Repeat("1", 5000) + `"}`,
	}
	for name, raw := range cases {
		if _, err := DecodeParams(json.RawMessage(raw)); err == nil {
			t.Errorf("%s: %q accepted", name, raw)
		}
	}
}

func TestParseTarget(t *testing.T) {
	good := map[string]Version{
		"0.0.0":                         {0, 0, 0},
		"0.15.0":                        {0, 15, 0},
		"1.2.3":                         {1, 2, 3},
		"10.20.30":                      {10, 20, 30},
		"999999999.999999999.999999999": {999999999, 999999999, 999999999},
		"9223372036854775807.0.0":       {9223372036854775807, 0, 0}, // max int64
	}
	for in, want := range good {
		got, err := ParseTarget(in)
		if err != nil {
			t.Errorf("ParseTarget(%q) error: %v", in, err)
			continue
		}
		if got != want {
			t.Errorf("ParseTarget(%q) = %v, want %v", in, got, want)
		}
		if got.String() != want.String() {
			t.Errorf("ParseTarget(%q).String() = %q, want %q", in, got, got)
		}
	}

	bad := []string{
		"", " ", "v1.2.3", "1.2", "1.2.3.4", "1.2.3-rc1", "1.2.3+foo",
		" 1.2.3", "1.2.3 ", "1.2.3\n", "1..3", ".2.3", "1.2.", "...",
		"a.b.c", "1.2.x", "0x1.2.3", "1,2,3", "1-2-3",
		// Not canonical: one release, one spelling.
		"01.2.3", "1.02.3", "1.2.03", "01.02.03", "00.0.0",
		"99999999999999999999999.0.0", // 23 digits: over the component cap
		"9223372036854775808.0.0",     // max int64 + 1: out of int range
		"1.99999999999999999999999.0", // overflow in minor
		strings.Repeat("1", 5000),     // absurd length
		"1.2.3-" + strings.Repeat("x", 500),
	}
	for _, in := range bad {
		if v, err := ParseTarget(in); err == nil {
			t.Errorf("ParseTarget(%q) = %v, want error", in, v)
		}
	}
	// Canonical input renders byte-identical: no second spelling exists.
	if v, _ := ParseTarget("1.2.3"); v.String() != "1.2.3" {
		t.Errorf("canonical form = %q, want 1.2.3", v)
	}
}

func TestParseCurrent(t *testing.T) {
	good := map[string]Version{
		"0.14.0":             {0, 14, 0},
		"0.0.0":              {0, 0, 0},
		"0.14.0-51-ge946197": {0, 14, 0}, // git describe build
		"0.15.0-3-gabc":      {0, 15, 0},
		"1.2.3-rc1":          {1, 2, 3},
		"1.2.3-a-b-c":        {1, 2, 3}, // suffix keeps its dashes
	}
	for in, want := range good {
		got, err := ParseCurrent(in)
		if err != nil {
			t.Errorf("ParseCurrent(%q) error: %v", in, err)
			continue
		}
		if got != want {
			t.Errorf("ParseCurrent(%q) = %v, want %v", in, got, want)
		}
	}
	bad := []string{
		"", "-", "0.15.0-", "-suffix", "v1.2.3", "1.2", "1.2-suffix",
		"1.2.3.4-x", "a.b.c-x", " 1.2.3", "1.2.3 ",
		"01.02.03", "01.02.03-suffix",
		"99999999999999999999999.0.0-x",
		"0.0.0-" + strings.Repeat("x", 2000), // absurd length
	}
	for _, in := range bad {
		if v, err := ParseCurrent(in); err == nil {
			t.Errorf("ParseCurrent(%q) = %v, want error", in, v)
		}
	}
}

func TestCompare(t *testing.T) {
	cases := []struct {
		current, target string
		want            Outcome
	}{
		{"0.14.0", "0.15.0", TargetNewer},
		{"0.14.0-51-ge946197", "0.15.0", TargetNewer},
		{"0.15.0-3-gabc", "0.15.0", TargetEqual},
		{"0.16.0", "0.15.0", TargetOlder},
		{"1.0.0", "2.0.0", TargetNewer},
		{"2.0.0", "1.9.9", TargetOlder},
		{"0.9.9", "0.10.0", TargetNewer}, // numeric, not lexicographic
		{"0.10.0", "0.9.9", TargetOlder},
		{"1.2.3", "1.2.4", TargetNewer}, // patch decides
		{"1.2.4", "1.2.3", TargetOlder},
		{"0.0.0", "0.0.0", TargetEqual},
		{"0.0.0", "0.0.1", TargetNewer},
		{"01.02.03", "1.2.3", Invalid}, // non-canonical current refuses
		{"1.2.3", "01.02.04", Invalid}, // non-canonical target refuses
		// Either side unparsable: invalid, fail-closed.
		{"bogus", "0.15.0", Invalid},
		{"", "0.15.0", Invalid},
		{"0.14.0", "bogus", Invalid},
		{"0.14.0", "", Invalid},
		{"0.14.0", "0.15.0-rc1", Invalid}, // target takes no suffix
		{"bogus", "bogus", Invalid},
	}
	for _, c := range cases {
		if got := Compare(c.current, c.target); got != c.want {
			t.Errorf("Compare(%q, %q) = %v, want %v", c.current, c.target, got, c.want)
		}
	}
}

func TestDecide(t *testing.T) {
	cases := map[Outcome]Decision{
		TargetNewer: Eligible,
		TargetEqual: AlreadyAtTarget,
		TargetOlder: RefusedDowngrade,
		Invalid:     RefusedInvalid,
		Outcome(99): RefusedInvalid, // unknown outcome refuses
	}
	for in, want := range cases {
		if got := Decide(in); got != want {
			t.Errorf("Decide(%v) = %v, want %v", in, got, want)
		}
	}
	// Zero values fail closed.
	var o Outcome
	if o != Invalid {
		t.Errorf("zero Outcome = %v, want invalid", o)
	}
	var d Decision
	if d != RefusedInvalid {
		t.Errorf("zero Decision = %v, want refused_invalid", d)
	}
}

func TestOutcomeAndDecisionString(t *testing.T) {
	for o, want := range map[Outcome]string{
		Invalid: "invalid", TargetNewer: "newer",
		TargetEqual: "equal", TargetOlder: "older", Outcome(99): "invalid",
	} {
		if o.String() != want {
			t.Errorf("Outcome(%d).String() = %q, want %q", int(o), o, want)
		}
	}
	for d, want := range map[Decision]string{
		RefusedInvalid: "refused_invalid", Eligible: "eligible",
		AlreadyAtTarget:  "already_at_target",
		RefusedDowngrade: "refused_downgrade", Decision(99): "refused_invalid",
	} {
		if d.String() != want {
			t.Errorf("Decision(%d).String() = %q, want %q", int(d), d, want)
		}
	}
}

func TestEligibleForUpgrade(t *testing.T) {
	cases := []struct {
		d       Decision
		enabled bool
		want    bool
	}{
		{Eligible, true, true},
		{Eligible, false, false}, // kill-switch off blocks PR6
		{AlreadyAtTarget, true, false},
		{RefusedDowngrade, true, false},
		{RefusedInvalid, true, false},
	}
	for _, c := range cases {
		if got := EligibleForUpgrade(c.d, c.enabled); got != c.want {
			t.Errorf("EligibleForUpgrade(%v, %v) = %v, want %v",
				c.d, c.enabled, got, c.want)
		}
	}
}

func FuzzParseTarget(f *testing.F) {
	for _, s := range []string{"0.15.0", "1.2.3", "01.02.03", "", "1.2", "v1.2.3",
		"1.2.3-rc1", "99999999999999999999999.0.0", "a.b.c"} {
		f.Add(s)
	}
	f.Fuzz(func(t *testing.T, s string) {
		v, err := ParseTarget(s)
		if err != nil {
			return
		}
		// An accepted target round-trips through its canonical form.
		again, err := ParseTarget(v.String())
		if err != nil || again != v {
			t.Fatalf("ParseTarget(%q) = %v, reparse = %v, %v", s, v, again, err)
		}
	})
}

func FuzzParseCurrent(f *testing.F) {
	for _, s := range []string{"0.14.0", "0.14.0-51-ge946197", "1.2.3-rc1",
		"", "-", "0.15.0-", "1.2", "bogus"} {
		f.Add(s)
	}
	f.Fuzz(func(t *testing.T, s string) {
		v, err := ParseCurrent(s)
		if err != nil {
			return
		}
		again, err := ParseTarget(v.String())
		if err != nil || again != v {
			t.Fatalf("ParseCurrent(%q) = %v, reparse = %v, %v", s, v, again, err)
		}
	})
}

func FuzzDecodeParams(f *testing.F) {
	for _, s := range []string{`{"target_version":"0.15.0"}`, `{}`, ``, `[]`,
		`{"target_version":"0.15.0","url":"x"}`,
		`{"target_version":"0.15.0","target_version":"0.16.0"}`} {
		f.Add(s)
	}
	f.Fuzz(func(t *testing.T, s string) {
		p, err := DecodeParams(json.RawMessage(s))
		if err != nil {
			return
		}
		// A decoded target is always a strict parseable version.
		if _, err := ParseTarget(p.TargetVersion); err != nil {
			t.Fatalf("DecodeParams(%q) target %q rejected: %v", s, p.TargetVersion, err)
		}
	})
}
