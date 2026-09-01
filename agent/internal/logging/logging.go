// Package logging is a tiny logfmt-style wrapper over the stdlib log package,
// so the agent's output lines up with the backend's (level=... msg=... k=v).
// Timestamps come from journald. CADENCE_LOG_LEVEL=debug enables Debug lines.
package logging

import (
	"fmt"
	"log"
	"os"
	"strings"
)

var debugEnabled = strings.EqualFold(os.Getenv("CADENCE_LOG_LEVEL"), "debug")

func quote(s string) string {
	if s == "" || strings.ContainsAny(s, " =\"\n") {
		return `"` + strings.NewReplacer(`"`, `\"`, "\n", " ").Replace(s) + `"`
	}
	return s
}

func line(level, msg string, fields []any) string {
	var b strings.Builder
	fmt.Fprintf(&b, "level=%s msg=%s", level, quote(msg))
	for i := 0; i+1 < len(fields); i += 2 {
		fmt.Fprintf(&b, " %v=%s", fields[i], quote(fmt.Sprint(fields[i+1])))
	}
	return b.String()
}

// Info/Warn/Error/Debug take a message then alternating key, value pairs.
func Info(msg string, fields ...any)  { log.Print(line("info", msg, fields)) }
func Warn(msg string, fields ...any)  { log.Print(line("warn", msg, fields)) }
func Error(msg string, fields ...any) { log.Print(line("error", msg, fields)) }

func Debug(msg string, fields ...any) {
	if debugEnabled {
		log.Print(line("debug", msg, fields))
	}
}
