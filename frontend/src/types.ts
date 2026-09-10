export type HostStatus = 'up_to_date' | 'updates_available' | 'security_updates_available'

export type RebootPolicy = 'auto' | 'never' | 'prompt'

export interface HostSummary {
  id: string
  hostname: string
  fqdn: string | null
  description: string | null
  os_family: string
  os_name: string | null
  os_version: string | null
  package_manager: string
  agent_version: string | null
  reboot_required: boolean
  reboot_policy: RebootPolicy
  is_active: boolean
  tags: Record<string, string>
  last_seen_at: string | null
  created_at: string
  updated_at: string
  status: HostStatus
  updates_available_count: number
  security_updates_count: number
  excluded_count: number
}

export interface FleetSummary {
  total_hosts: number
  active_hosts: number
  inactive_hosts: number
  up_to_date: number
  updates_available: number
  security_updates_available: number
  reboot_required: number
  late: number
  silent: number
  pending_updates: number
  security_updates: number
  oldest_report_age_seconds: number | null
  jobs_running: number
  jobs_succeeded_24h: number
  jobs_failed_24h: number
}

export interface ReportSummary {
  id: number
  received_at: string
  agent_version: string | null
  installed_package_count: number
  updates_available_count: number
  security_updates_count: number
  reboot_required: boolean
}

export interface AdvisoryRef {
  id: string
  url: string
  cves: string[]
}

export interface HostPackage {
  name: string
  architecture: string
  installed_version: string
  candidate_version: string | null
  is_security_update: boolean
  update_origin: string | null
  updated_at: string
  source_package?: string | null
  advisories: AdvisoryRef[]
  excluded: boolean
}

export interface HostDetail extends HostSummary {
  packages: HostPackage[]
}

export type PackageStatusFilter = 'pending' | 'security' | 'all'

export interface PackageHost {
  host_id: string
  hostname: string
  installed_version: string
  candidate_version: string | null
  is_security_update: boolean
  update_origin: string | null
  updated_at: string
  source_package?: string | null
  advisories: AdvisoryRef[]
}

export interface PackageSummaryRow {
  name: string
  architecture: string
  hosts: PackageHost[]
}

export type ScheduleKind = 'monthly' | 'weekly'

export interface Schedule {
  id: string
  host_id: string
  enabled: boolean
  kind: ScheduleKind
  day_of_month: number | null
  weekday: number | null // Monday = 0
  hour: number
  minute: number
  timezone: string
  params: Record<string, unknown>
  last_run_at: string | null
  next_run_at: string | null
  created_at: string
  updated_at: string
}

export interface ScheduleInput {
  enabled: boolean
  kind: ScheduleKind
  day_of_month: number | null
  weekday: number | null
  hour: number
  minute: number
  timezone: string
  params: Record<string, unknown>
}

export type WebhookEvent =
  | 'job.succeeded'
  | 'job.failed'
  | 'host.offline'
  | 'host.reboot_required'
  | 'host.security_updates_available'
  | 'campaign.stage_completed'
  | 'campaign.completed'
  | 'campaign.stopped'

export interface Webhook {
  id: string
  url_preview: string
  enabled: boolean
  event_types: WebhookEvent[]
  description: string | null
  created_at: string
  updated_at: string
  last_success_at: string | null
  last_error: string | null
  pending_count: number
  failed_count: number
}

export interface WebhookInput {
  url: string
  event_types: WebhookEvent[]
  enabled: boolean
  description: string | null
}

// The one response that carries the full URL and the plaintext secret, shown
// once at creation.
export interface WebhookCreated {
  id: string
  url: string
  secret: string
  enabled: boolean
  event_types: WebhookEvent[]
  description: string | null
  created_at: string
}

export type PolicyScope = 'global' | 'host' | 'tag'

export interface PackageExclusion {
  id: string
  scope: PolicyScope
  host_id: string | null
  tag: string | null
  pattern: string
  description: string | null
  created_at: string
}

export interface PackageExclusionInput {
  scope: PolicyScope
  host_id: string | null
  tag: string | null
  pattern: string
  description: string | null
}

export type JobStatus = 'pending' | 'running' | 'succeeded' | 'failed'

// One package line in an apt_dry_run preview (roadmap item 4).
export interface DryRunPkg {
  name: string
  architecture?: string
  installed_version?: string
  candidate_version?: string
  is_security_update?: boolean
}

// The structured result an apt_dry_run job stores under result.dry_run.
export interface DryRunResult {
  updated: DryRunPkg[]
  newly_installed: DryRunPkg[]
  removed: DryRunPkg[]
  kept_back: string[]
  excluded: string[]
  held_in_place: string[]
}

export interface Job {
  id: string
  host_id: string
  job_type: string
  status: JobStatus
  params: Record<string, unknown>
  requested_by: string | null
  result: {
    exit_code?: number | null
    reboot_required?: boolean | null
    held_conflicts?: string[] | null
    dry_run?: DryRunResult | null
  } | null
  log: string | null
  failure_category: string | null
  failure_summary: string | null
  created_at: string
  started_at: string | null
  completed_at: string | null
}

// --- campaigns (roadmap item 5) ---

export type CampaignStatus = 'draft' | 'running' | 'paused' | 'completed' | 'stopped' | 'cancelled'

export type CampaignHostState = 'pending' | 'running' | 'done' | 'skipped' | 'orphaned'

// A wave size: a positive integer (absolute), "N%" (percent of the target
// count), or "rest" (all remaining, last only).
export type StageSize = number | string

export interface Campaign {
  id: string
  name: string
  job_type: string
  stages: StageSize[]
  max_concurrency: number
  max_failures: number
  observation_window_seconds: number
  status: CampaignStatus
  halt_reason: string | null
  requested_by: string | null
  hosts_total: number
  hosts_done: number
  hosts_skipped: number
  hosts_orphaned: number
  current_stage_index: number | null
  created_at: string
  started_at: string | null
  completed_at: string | null
  updated_at: string
}

export interface CampaignStageDetail {
  index: number
  size_spec: StageSize
  hosts_total: number
  pending: number
  running: number
  done: number
  skipped: number
  orphaned: number
}

export interface CampaignHost {
  host_id: string
  hostname: string
  stage_index: number
  state: CampaignHostState
  skip_reason: string | null
  job_id: string | null
}

export interface CampaignDetail extends Campaign {
  stages_detail: CampaignStageDetail[]
  hosts: CampaignHost[]
}

// Exactly one of host_ids / tag is sent; observation_window_seconds omitted
// means "use the server default".
export interface CampaignInput {
  name: string
  stages: StageSize[]
  max_concurrency: number
  max_failures: number
  observation_window_seconds?: number | null
  host_ids?: string[]
  tag?: string | null
}
