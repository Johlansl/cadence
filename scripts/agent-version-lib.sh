# shellcheck shell=sh
# agent-version-lib.sh -- the one definition of an agent release version.
#
# Sourced, never executed: it only defines a function. Users:
#   publish-agent.sh          version of a bundle or a release to stage
#   ci-sign-agent.sh          version a CI run signs
#   check-release-source.sh   version of a break-glass build
# agent-install.sh keeps its own copy on purpose: it is downloaded and run on
# its own, on hosts that have no checkout. scripts/test-ci-sign-agent.sh feeds
# the same list of versions to this function and to that copy, so the two cannot
# drift apart unnoticed.

# check_version <string>: succeed only for a strict canonical N.N.N, the same
# grammar as the agent's ParseTarget: digits and dots only, exactly three
# components, each "0" or starting with 1-9. No suffix, no sign, no space, no
# leading zero. One release has exactly one spelling.
check_version() {
	case $1 in
	*[!0-9.]* | "" | .* | *. | *..*) return 1 ;;
	esac
	_v1=${1%%.*}
	_rest=${1#*.}
	case $_rest in *.*) ;; *) return 1 ;; esac
	_v2=${_rest%%.*}
	_v3=${_rest#*.}
	case $_v3 in *.*) return 1 ;; esac
	[ -n "$_v1" ] && [ -n "$_v2" ] && [ -n "$_v3" ] || return 1
	for _v in "$_v1" "$_v2" "$_v3"; do
		case $_v in 0*) [ "$_v" = "0" ] || return 1 ;; esac
	done
	return 0
}
