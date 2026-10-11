# shellcheck shell=sh
# agent-sign-lib.sh -- helpers shared by the agent release signing tools.
#
# Sourced, never executed: it only defines functions. Users:
#   ci-sign-agent.sh          trusted comment format
#   build-agent-release.sh    break-glass signing with the trust set
#   backup-signing-key.sh     is the key one of the trust set?
#   restore-signing-key.sh    same, plus the encryption check
# (agent-install.sh and publish-agent.sh stay standalone on purpose: the
# first is copied alone to hosts, the second must not depend on signing code,
# so each keeps its own key ID code. publish-agent.sh sources only
# agent-version-lib.sh, the version grammar. scripts/test-ci-sign-agent.sh feeds
# the same keys to the three key ID copies and the same versions to the copies of
# the grammar, so none can drift unnoticed.)
#
# The trust set is the two committed public keys, in this order:
#   agent/minisign.pub          active key (signs releases in CI)
#   agent/minisign-backup.pub   offline backup key (break-glass releases)
# CADENCE_PUBLISH_PUBKEY and CADENCE_PUBLISH_PUBKEY_BACKUP override the two
# paths, the same variables publish-agent.sh reads. They exist for tests; when
# one is set a warning goes to stderr so it cannot go unnoticed.

# agent_trusted_comment <version> <arch> <sha256>
# The ONE definition of the trusted comment of an agent release signature.
# For human audit only: the agent never parses it.
agent_trusted_comment() {
	printf 'cadence-agent %s linux/%s sha256:%s' "$1" "$2" "$3"
}

# is_pub_key_line <string>: exactly ^RW[A-Za-z0-9+/]{54}$ (explicit character
# list: ranges are locale dependent in some shells).
is_pub_key_line() {
	case $1 in
	RW*) ;;
	*) return 1 ;;
	esac
	case $1 in
	*[!ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/]*) return 1 ;;
	esac
	[ "${#1}" -eq 56 ]
}

# key_id <base64 line>: the 16 hex digit key ID carried by a public key line
# or a signature line (bytes 3 to 10 once decoded, printed in reverse order).
# Derived from the bytes, never from an "untrusted comment". Informational:
# any error prints "?" and the function still succeeds.
key_id() {
	_kid=$(printf '%s' "$1" | base64 -d 2>/dev/null | od -An -tx1 -j2 -N8 2>/dev/null |
		tr -d ' \n' |
		sed 's/^\(..\)\(..\)\(..\)\(..\)\(..\)\(..\)\(..\)\(..\)$/\8\7\6\5\4\3\2\1/' |
		tr 'a-f' 'A-F') || _kid=""
	case $_kid in
	"" | *[!0-9A-F]*) _kid='?' ;;
	esac
	[ "${#_kid}" -eq 16 ] || _kid='?'
	printf '%s\n' "$_kid"
}

# pub_file_ok <file>: a minisign public key file, exactly two lines (the
# "untrusted comment:" line, then a key line).
pub_file_ok() {
	[ -f "$1" ] || return 1
	_n=$(sed -n '$=' "$1" 2>/dev/null) || return 1
	[ "$_n" = "2" ] || return 1
	_c=$(sed -n 1p "$1" 2>/dev/null) || return 1
	case $_c in
	"untrusted comment:"*) ;;
	*) return 1 ;;
	esac
	_k=$(sed -n 2p "$1" 2>/dev/null) || return 1
	is_pub_key_line "$_k"
}

# pub_file_id <file>: key ID of a public key file (see key_id).
pub_file_id() {
	key_id "$(sed -n 2p "$1" 2>/dev/null || true)"
}

# trust_set_load <repo root>: sets TRUST_ACTIVE and TRUST_BACKUP.
trust_set_load() {
	TRUST_ACTIVE=${CADENCE_PUBLISH_PUBKEY:-$1/agent/minisign.pub}
	TRUST_BACKUP=${CADENCE_PUBLISH_PUBKEY_BACKUP:-$1/agent/minisign-backup.pub}
	if [ -n "${CADENCE_PUBLISH_PUBKEY:-}" ] || [ -n "${CADENCE_PUBLISH_PUBKEY_BACKUP:-}" ]; then
		echo "WARNING: trust set override in effect (CADENCE_PUBLISH_PUBKEY / CADENCE_PUBLISH_PUBKEY_BACKUP): for tests only" >&2
	fi
}

# trust_set_check: both keys well formed, with different key IDs. Prints the
# reason on stderr and fails otherwise.
trust_set_check() {
	for _p in "$TRUST_ACTIVE" "$TRUST_BACKUP"; do
		if ! pub_file_ok "$_p"; then
			echo "trust set: $_p is not a well-formed minisign public key file" >&2
			return 1
		fi
	done
	if [ "$(pub_file_id "$TRUST_ACTIVE")" = "$(pub_file_id "$TRUST_BACKUP")" ]; then
		echo "trust set: the active and backup public keys are the same key" >&2
		return 1
	fi
}

# trust_set_verify <file> <sig>: succeed when the active key, then the backup
# key, verifies the signature. Prints "<key id> <active|backup>" on stdout.
trust_set_verify() {
	if minisign -V -p "$TRUST_ACTIVE" -x "$2" -m "$1" -q </dev/null >/dev/null 2>&1; then
		printf '%s active\n' "$(pub_file_id "$TRUST_ACTIVE")"
		return 0
	fi
	if minisign -V -p "$TRUST_BACKUP" -x "$2" -m "$1" -q </dev/null >/dev/null 2>&1; then
		printf '%s backup\n' "$(pub_file_id "$TRUST_BACKUP")"
		return 0
	fi
	return 1
}

# key_file_encrypted <secret key file>: succeed when the key is password
# protected (scrypt), judged from the key file header, not from a password
# attempt. An unprotected key has a zero KDF algorithm field.
key_file_encrypted() {
	_h=$(sed -n 2p "$1" 2>/dev/null | base64 -d 2>/dev/null | od -An -tx1 -N4 2>/dev/null | tr -d ' \n') || return 1
	[ "$_h" = "45645363" ]
}

# sign_bundle <secret key> <version> <dir>
# Signs <dir>/cadence-agent-linux-amd64 and -arm64 with the trusted comment
# above, then verifies every signature against the trust set (the trust set
# must be loaded and checked first) and checks its trusted comment byte for
# byte. Any failure removes the signatures this call wrote and fails.
# minisign asks for the key password itself when the key has one (stdin and
# the terminal are left to it); nothing here ever sees or prints a password.
sign_bundle() {
	_sk=$1
	_ver=$2
	_dir=$3
	for _a in amd64 arm64; do
		_bin=$_dir/cadence-agent-linux-$_a
		_sum=$(sha256sum "$_bin" | cut -d' ' -f1)
		_tc=$(agent_trusted_comment "$_ver" "$_a" "$_sum")
		rm -f -- "$_bin.minisig"
		if ! minisign -S -s "$_sk" -x "$_bin.minisig" -t "$_tc" -m "$_bin" >/dev/null; then
			echo "sign_bundle: signing failed for $_bin" >&2
			rm -f -- "$_dir/cadence-agent-linux-amd64.minisig" "$_dir/cadence-agent-linux-arm64.minisig"
			return 1
		fi
		if ! _who=$(trust_set_verify "$_bin" "$_bin.minisig"); then
			echo "sign_bundle: the signature for $_bin does not verify against the trust set: wrong secret key?" >&2
			rm -f -- "$_dir/cadence-agent-linux-amd64.minisig" "$_dir/cadence-agent-linux-arm64.minisig"
			return 1
		fi
		_line=$(sed -n 3p "$_bin.minisig")
		if [ "$_line" != "trusted comment: $_tc" ]; then
			echo "sign_bundle: unexpected trusted comment in $_bin.minisig" >&2
			rm -f -- "$_dir/cadence-agent-linux-amd64.minisig" "$_dir/cadence-agent-linux-arm64.minisig"
			return 1
		fi
		echo "sign_bundle: linux/$_a signature verified by key ${_who% *} (${_who#* })"
	done
}
