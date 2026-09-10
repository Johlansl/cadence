"""Campaign configuration and lifecycle control (roadmap item 5).

A campaign is a staged rollout of apt_upgrade jobs across a fixed host set;
once activated, the scheduler's advance_campaigns() (app.campaigns.engine)
drives it stage by stage. The GET views are unauthenticated like the other
dashboard reads; create and the lifecycle actions (activate / pause / resume /
cancel) need the X-Admin-Key and are audited.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi import status as http_status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.audit import record_audit
from app.api.deps import get_db, require_admin_key
from app.campaigns.lifecycle import finalize_campaign_hosts
from app.campaigns.targeting import resolve_targets, slice_into_stages
from app.core.config import settings
from app.models.models import Campaign, CampaignHost, Host
from app.schemas.schemas import (
    CampaignCreate,
    CampaignDetailOut,
    CampaignHostOut,
    CampaignOut,
    CampaignStageDetail,
)

router = APIRouter(prefix="/api/v1", tags=["campaigns"])
admin_router = APIRouter(
    prefix="/api/v1/admin", tags=["campaigns"], dependencies=[Depends(require_admin_key)]
)


# --- derived views -------------------------------------------------------------


def _host_rows(db: Session, campaign_id: uuid.UUID) -> list[tuple[CampaignHost, str]]:
    return list(
        db.execute(
            select(CampaignHost, Host.hostname)
            .join(Host, Host.id == CampaignHost.host_id)
            .where(CampaignHost.campaign_id == campaign_id)
            .order_by(Host.hostname, Host.id)
        ).all()
    )


def _current_stage_index(rows: list[tuple[CampaignHost, str]]) -> int | None:
    active = [ch.stage_index for ch, _ in rows if ch.state in ("pending", "running")]
    return min(active) if active else None


def _to_out(campaign: Campaign, rows: list[tuple[CampaignHost, str]]) -> CampaignOut:
    states = [ch.state for ch, _ in rows]
    return CampaignOut(
        id=campaign.id,
        name=campaign.name,
        job_type=campaign.job_type,
        stages=campaign.stages,
        max_concurrency=campaign.max_concurrency,
        max_failures=campaign.max_failures,
        observation_window_seconds=campaign.observation_window_seconds,
        status=campaign.status,
        halt_reason=campaign.halt_reason,
        requested_by=campaign.requested_by,
        hosts_total=len(rows),
        hosts_done=states.count("done"),
        hosts_skipped=states.count("skipped"),
        hosts_orphaned=states.count("orphaned"),
        current_stage_index=_current_stage_index(rows),
        created_at=campaign.created_at,
        started_at=campaign.started_at,
        completed_at=campaign.completed_at,
        updated_at=campaign.updated_at,
    )


def _to_detail(
    campaign: Campaign, rows: list[tuple[CampaignHost, str]]
) -> CampaignDetailOut:
    stages_detail = []
    for idx, spec in enumerate(campaign.stages):
        in_stage = [ch.state for ch, _ in rows if ch.stage_index == idx]
        stages_detail.append(
            CampaignStageDetail(
                index=idx,
                size_spec=spec,
                hosts_total=len(in_stage),
                pending=in_stage.count("pending"),
                running=in_stage.count("running"),
                done=in_stage.count("done"),
                skipped=in_stage.count("skipped"),
                orphaned=in_stage.count("orphaned"),
            )
        )
    return CampaignDetailOut(
        **_to_out(campaign, rows).model_dump(),
        stages_detail=stages_detail,
        hosts=[
            CampaignHostOut(
                host_id=ch.host_id,
                hostname=hostname,
                stage_index=ch.stage_index,
                state=ch.state,
                skip_reason=ch.skip_reason,
                job_id=ch.job_id,
            )
            for ch, hostname in rows
        ],
    )


def _actor(request: Request) -> str:
    return (request.headers.get("x-actor") or "").strip() or "admin"


# --- reads -------------------------------------------------------------------


@router.get("/campaigns", response_model=list[CampaignOut])
def list_campaigns(db: Session = Depends(get_db)) -> list[CampaignOut]:
    campaigns = (
        db.execute(select(Campaign).order_by(Campaign.created_at.desc()))
        .scalars()
        .all()
    )
    return [_to_out(c, _host_rows(db, c.id)) for c in campaigns]


@router.get("/campaigns/{campaign_id}", response_model=CampaignDetailOut)
def get_campaign(
    campaign_id: uuid.UUID, db: Session = Depends(get_db)
) -> CampaignDetailOut:
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, "campaign not found")
    return _to_detail(campaign, _host_rows(db, campaign_id))


# --- create ----------------------------------------------------------------


@admin_router.post(
    "/campaigns",
    response_model=CampaignDetailOut,
    status_code=http_status.HTTP_201_CREATED,
)
def create_campaign(
    request: Request, payload: CampaignCreate, db: Session = Depends(get_db)
) -> CampaignDetailOut:
    try:
        hosts = resolve_targets(db, host_ids=payload.host_ids, tag=payload.tag)
    except ValueError as exc:
        raise HTTPException(
            http_status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)
        ) from exc
    if not hosts:
        raise HTTPException(
            http_status.HTTP_422_UNPROCESSABLE_CONTENT, "targeting matched no hosts"
        )
    try:
        assignment = slice_into_stages(len(hosts), payload.stages)
    except ValueError as exc:
        raise HTTPException(
            http_status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)
        ) from exc

    window = (
        payload.observation_window_seconds
        if payload.observation_window_seconds is not None
        else settings.campaign_observation_window_seconds
    )
    campaign = Campaign(
        name=payload.name,
        stages=payload.stages,
        max_concurrency=payload.max_concurrency,
        max_failures=payload.max_failures,
        observation_window_seconds=window,
        status="draft",
        requested_by=_actor(request),
    )
    db.add(campaign)
    db.flush()  # populate campaign.id
    db.add_all(
        CampaignHost(
            campaign_id=campaign.id, host_id=host.id, stage_index=stage_index
        )
        for host, stage_index in zip(hosts, assignment, strict=True)
    )
    record_audit(
        db,
        request,
        "campaign.create",
        target_type="campaign",
        target_id=campaign.id,
        detail={
            "name": campaign.name,
            "hosts": len(hosts),
            "stages": campaign.stages,
        },
    )
    db.commit()
    db.refresh(campaign)
    return _to_detail(campaign, _host_rows(db, campaign.id))


# --- lifecycle actions ---------------------------------------------------------


def _load_for_transition(
    db: Session, campaign_id: uuid.UUID, allowed: tuple[str, ...], verb: str
) -> Campaign:
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, "campaign not found")
    if campaign.status not in allowed:
        raise HTTPException(
            http_status.HTTP_409_CONFLICT,
            f"campaign is {campaign.status}, cannot {verb}",
        )
    return campaign


@admin_router.post("/campaigns/{campaign_id}/activate", response_model=CampaignDetailOut)
def activate_campaign(
    request: Request, campaign_id: uuid.UUID, db: Session = Depends(get_db)
) -> CampaignDetailOut:
    now = datetime.now(timezone.utc)
    campaign = _load_for_transition(db, campaign_id, ("draft",), "activate")
    campaign.status = "running"
    campaign.started_at = now
    campaign.updated_at = now
    record_audit(
        db, request, "campaign.activate", target_type="campaign", target_id=campaign.id
    )
    db.commit()
    return _to_detail(campaign, _host_rows(db, campaign.id))


@admin_router.post("/campaigns/{campaign_id}/pause", response_model=CampaignDetailOut)
def pause_campaign(
    request: Request, campaign_id: uuid.UUID, db: Session = Depends(get_db)
) -> CampaignDetailOut:
    now = datetime.now(timezone.utc)
    campaign = _load_for_transition(db, campaign_id, ("running",), "pause")
    campaign.status = "paused"
    campaign.updated_at = now
    record_audit(
        db, request, "campaign.pause", target_type="campaign", target_id=campaign.id
    )
    db.commit()
    return _to_detail(campaign, _host_rows(db, campaign.id))


@admin_router.post("/campaigns/{campaign_id}/resume", response_model=CampaignDetailOut)
def resume_campaign(
    request: Request, campaign_id: uuid.UUID, db: Session = Depends(get_db)
) -> CampaignDetailOut:
    now = datetime.now(timezone.utc)
    campaign = _load_for_transition(db, campaign_id, ("paused",), "resume")
    campaign.status = "running"
    campaign.updated_at = now
    record_audit(
        db, request, "campaign.resume", target_type="campaign", target_id=campaign.id
    )
    db.commit()
    return _to_detail(campaign, _host_rows(db, campaign.id))


@admin_router.post("/campaigns/{campaign_id}/cancel", response_model=CampaignDetailOut)
def cancel_campaign(
    request: Request, campaign_id: uuid.UUID, db: Session = Depends(get_db)
) -> CampaignDetailOut:
    now = datetime.now(timezone.utc)
    campaign = _load_for_transition(
        db, campaign_id, ("draft", "running", "paused"), "cancel"
    )
    campaign.status = "cancelled"
    campaign.completed_at = now
    campaign.updated_at = now
    # In-flight jobs keep running on their agents; their campaign_hosts rows are
    # terminal-ized now so the engine (which ignores non-running campaigns)
    # never leaves them stuck.
    finalize_campaign_hosts(db, campaign.id, now=now)
    record_audit(
        db, request, "campaign.cancel", target_type="campaign", target_id=campaign.id
    )
    db.commit()
    return _to_detail(campaign, _host_rows(db, campaign.id))
