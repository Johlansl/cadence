export type HostStatus =
  | 'up_to_date'
  | 'updates_available'
  | 'security_updates_available'

export type RebootPolicy = 'auto' | 'never'

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
  last_seen_at: string | null
  created_at: string
  updated_at: string
  status: HostStatus
  updates_available_count: number
  security_updates_count: number
}

export interface HostPackage {
  name: string
  architecture: string
  installed_version: string
  candidate_version: string | null
  is_security_update: boolean
  update_origin: string | null
  updated_at: string
}

export interface HostDetail extends HostSummary {
  packages: HostPackage[]
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

export type JobStatus = 'pending' | 'running' | 'succeeded' | 'failed'

export interface Job {
  id: string
  host_id: string
  job_type: string
  status: JobStatus
  params: Record<string, unknown>
  requested_by: string | null
  result: { exit_code?: number | null; reboot_required?: boolean | null } | null
  log: string | null
  created_at: string
  started_at: string | null
  completed_at: string | null
}
