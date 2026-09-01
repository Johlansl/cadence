"""Maintenance-window schedules.

One per host (DB UNIQUE). The read view is unauthenticated like the other
dashboard GETs; create / update / delete need the X-Admin-Key. The `scheduler`
service (app/scheduler.py) is what actually turns a due schedule into a job.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi import status as http_status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.audit import record_audit
from app.api.deps import get_db, require_admin_key
from app.core.schedule_timing import next_run_at
from app.models.models import Host, Schedule
from app.schemas.schemas import ScheduleIn, ScheduleOut, ScheduleUpdate

router = APIRouter(prefix="/api/v1", tags=["schedules"])
admin_router = APIRouter(
    prefix="/api/v1/admin", tags=["schedules"], dependencies=[Depends(require_admin_key)]
)


def _recompute(schedule: Schedule, *, now: datetime) -> None:
    """Set next_run_at from the window fields, or None while disabled."""
    if not schedule.enabled:
        schedule.next_run_at = None
        return
    schedule.next_run_at = next_run_at(
        kind=schedule.kind,
        day_of_month=schedule.day_of_month,
        weekday=schedule.weekday,
        hour=schedule.hour,
        minute=schedule.minute,
        timezone=schedule.timezone,
        after=now,
    )


@router.get("/hosts/{host_id}/schedules", response_model=list[ScheduleOut])
def list_host_schedules(host_id: uuid.UUID, db: Session = Depends(get_db)) -> list[Schedule]:
    if db.get(Host, host_id) is None:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, "host not found")
    return list(
        db.execute(select(Schedule).where(Schedule.host_id == host_id)).scalars().all()
    )


@admin_router.post(
    "/hosts/{host_id}/schedules",
    response_model=ScheduleOut,
    status_code=http_status.HTTP_201_CREATED,
)
def create_schedule(
    request: Request,
    host_id: uuid.UUID,
    payload: ScheduleIn,
    db: Session = Depends(get_db),
) -> Schedule:
    if db.get(Host, host_id) is None:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, "host not found")
    exists = db.execute(
        select(Schedule.id).where(Schedule.host_id == host_id)
    ).first()
    if exists is not None:
        raise HTTPException(
            http_status.HTTP_409_CONFLICT, "this host already has a schedule"
        )

    now = datetime.now(timezone.utc)
    schedule = Schedule(host_id=host_id, **payload.model_dump())
    _recompute(schedule, now=now)
    db.add(schedule)
    db.flush()  # populate schedule.id for the audit row
    record_audit(
        db, request, "schedule.create", target_type="schedule",
        target_id=schedule.id, detail={"host_id": str(host_id)},
    )
    db.commit()
    db.refresh(schedule)
    return schedule


@admin_router.patch("/schedules/{schedule_id}", response_model=ScheduleOut)
def update_schedule(
    request: Request,
    schedule_id: uuid.UUID,
    payload: ScheduleUpdate,
    db: Session = Depends(get_db),
) -> Schedule:
    schedule = db.get(Schedule, schedule_id)
    if schedule is None:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, "schedule not found")

    patch = payload.model_dump(exclude_unset=True)
    merged = {
        "enabled": schedule.enabled,
        "kind": schedule.kind,
        "day_of_month": schedule.day_of_month,
        "weekday": schedule.weekday,
        "hour": schedule.hour,
        "minute": schedule.minute,
        "timezone": schedule.timezone,
        "params": schedule.params,
        **patch,
    }
    # Re-validate the whole thing (coherence, ranges, tz, params.reboot).
    validated = ScheduleIn(**merged)

    now = datetime.now(timezone.utc)
    for field, value in validated.model_dump().items():
        setattr(schedule, field, value)
    schedule.updated_at = now
    _recompute(schedule, now=now)
    record_audit(
        db, request, "schedule.update", target_type="schedule",
        target_id=schedule_id, detail={"fields": sorted(patch)},
    )
    db.commit()
    db.refresh(schedule)
    return schedule


@admin_router.delete("/schedules/{schedule_id}", status_code=http_status.HTTP_204_NO_CONTENT)
def delete_schedule(
    request: Request, schedule_id: uuid.UUID, db: Session = Depends(get_db)
) -> Response:
    schedule = db.get(Schedule, schedule_id)
    if schedule is None:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, "schedule not found")
    host_id = schedule.host_id
    db.delete(schedule)
    record_audit(
        db, request, "schedule.delete", target_type="schedule",
        target_id=schedule_id, detail={"host_id": str(host_id)},
    )
    db.commit()
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)
