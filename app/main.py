"""FastAPI 入口：功能开关判定服务。

路由：
- POST /api/v1/flags              创建/更新开关配置（已存在则 version+1，同事务保存不可变版本）
- GET  /api/v1/flags              列出全部开关
- GET  /api/v1/flags/{flag_key}   查看单个开关
- DELETE /api/v1/flags/{flag_key} 删除开关（版本历史一并删除）
- PUT    /api/v1/flags/{flag_key}/overrides/{user_id}  写入按用户临时强制变体
- DELETE /api/v1/flags/{flag_key}/overrides/{user_id}  撤销按用户临时强制变体
- GET  /api/v1/flags/{flag_key}/versions            列出版本历史
- GET  /api/v1/flags/{flag_key}/versions/{version}  查看指定版本配置
- POST /api/v1/flags/{flag_key}/rollback  回滚：将历史版本复制为新当前版本（乐观版本检查）
- POST /api/v1/flags/{flag_key}/evaluate  判定（按路径中的 flag_key，可指定 config_version）
- POST /api/v1/flags/{flag_key}/preview-impact  发布前候选配置影响预览（只读，不落版本与快照）
- POST /api/v1/releases           创建多开关发布批次（多候选配置 + 共用预览用户，只生成待提交批次）
- POST /api/v1/releases/{release_id}/commit  提交批次（单事务再核版本，原子发布；重复提交幂等）
- GET  /api/v1/releases/{release_id}         查询批次状态、逐开关预览摘要与提交后版本号
- POST /api/v1/evaluate           判定（flag_key 在请求体中）
- GET  /api/v1/decisions          判定快照审计列表（不透明游标分页 + 组合过滤）
- GET  /api/v1/decisions/{request_id}     按 request_id 查询单条判定快照
- GET  /healthz                   健康检查
"""

import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import crud
from app import cursor as cursor_mod
from app import engine as eng
from app.database import Base, engine, get_db
from app.engine import RuleEvaluationError, decide
from app.models import FlagConfig
from app.schemas import (
    DecisionListResponse,
    EvaluateRequest,
    EvaluateResponse,
    FlagConfigBody,
    FlagResponse,
    FlagUpsertRequest,
    FlagVersionResponse,
    OverrideResponse,
    OverrideUpsertRequest,
    PreviewChange,
    PreviewImpactRequest,
    PreviewImpactResponse,
    PreviewOutcome,
    PreviewUser,
    ReleaseCreateRequest,
    ReleaseResponse,
    RollbackRequest,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    db = next(get_db())
    try:
        # 老库 create_all 不会追加新列 / 新索引，显式幂等补齐；
        # 列补齐必须先于任何 ORM 查询（否则老库缺列时 SELECT 直接报错）
        crud.ensure_activation_window_columns(db)
        crud.ensure_snapshot_decision_path(db)
        crud.ensure_decision_audit_indexes(db)
        crud.backfill_missing_versions(db)
    finally:
        db.close()
    _seed_demo_flags()
    yield


app = FastAPI(
    title="Feature Flag Decision API",
    version="1.0.0",
    description="有序属性规则 + 加权变体 + 稳定哈希分桶的功能开关判定服务",
    lifespan=lifespan,
)


def _seed_demo_flags() -> None:
    """首次启动时写入一个演示开关，已存在则不覆盖。"""
    db = next(get_db())
    try:
        if crud.get_flag_or_none(db, "new_checkout") is not None:
            return
        demo = FlagUpsertRequest(
            flag_key="new_checkout",
            description="新版结账页灰度：内部员工与白名单地区走规则，其余用户按 90/10 分桶",
            variants=[
                {"key": "on", "weight": 10, "description": "新版结账页"},
                {"key": "off", "weight": 90, "description": "旧版结账页"},
            ],
            rules=[
                {
                    "priority": 1,
                    "attribute": "employee",
                    "operator": "eq",
                    "values": [True],
                    "variant_key": "on",
                },
                {
                    "priority": 2,
                    "attribute": "region",
                    "operator": "in",
                    "values": ["cn-beta", "sg-beta"],
                    "variant_key": "on",
                },
            ],
        )
        crud.upsert_flag(db, demo)
    finally:
        db.close()


def _snapshot_to_response(snap, *, request_context: dict | None = None) -> dict:
    context = request_context if request_context is not None else json.loads(
        snap.request_context_json
    )
    return {
        "request_id": snap.request_id,
        "flag_key": snap.flag_key,
        "user_id": snap.user_id,
        "config_version": snap.config_version,
        "matched_rule": None,
        "bucket_value": snap.bucket_value,
        "variant_key": snap.variant_key,
        # 新快照固化判定路径（含 override）；老数据无该列时按是否命中规则推导
        "decision_path": snap.decision_path
        or ("rule" if snap.matched_rule_index is not None else "bucket"),
        "explanation": json.loads(snap.explanation_json),
        # SnapshotResponse 扩展字段
        "request_context": context,
        "matched_rule_index": snap.matched_rule_index,
        "decided_at": snap.decided_at,
    }


def _replay_response(
    db: Session, snap, flag: FlagConfig, req: EvaluateRequest
) -> EvaluateResponse:
    """从快照重建判定响应（幂等重试场景）。

    matched_rule 详情优先取快照记录的 config_version 对应的历史版本配置，
    版本记录缺失（如开关被删除重建）时回退到当前配置。
    """
    body = _snapshot_to_response(snap, request_context=req.context)
    matched_rule = None
    if snap.matched_rule_index is not None:
        ver = crud.get_flag_version(db, snap.flag_key, snap.config_version)
        rules = crud.parse_rules(ver.rules_json if ver is not None else flag.rules_json)
        if 0 <= snap.matched_rule_index < len(rules):
            r = rules[snap.matched_rule_index]
            matched_rule = {
                "rule_index": snap.matched_rule_index,
                "priority": r.priority,
                "attribute": r.attribute,
                "operator": r.operator,
                "values": r.values,
                "variant_key": r.variant_key,
            }
    body["matched_rule"] = matched_rule
    allowed = EvaluateResponse.model_fields
    return EvaluateResponse(**{k: v for k, v in body.items() if k in allowed})


def _decision_response(
    request_id: str, flag: FlagConfig, req: EvaluateRequest, decision, config_version: int
) -> EvaluateResponse:
    matched_rule = None
    if decision.matched_rule is not None:
        r = decision.matched_rule
        matched_rule = {
            "rule_index": decision.matched_rule_index,
            "priority": r.priority,
            "attribute": r.attribute,
            "operator": r.operator,
            "values": r.values,
            "variant_key": r.variant_key,
        }
    return EvaluateResponse(
        request_id=request_id,
        flag_key=flag.flag_key,
        user_id=req.user_id,
        config_version=config_version,
        matched_rule=matched_rule,
        bucket_value=decision.bucket_value,
        variant_key=decision.variant_key,
        decision_path=decision.decision_path,
        explanation=decision.explanation,
    )


@app.exception_handler(RuleEvaluationError)
async def rule_error_handler(request: Request, exc: RuleEvaluationError) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": str(exc)})


@app.get("/healthz")
def healthz(db: Session = Depends(get_db)) -> dict:
    return {"status": "ok", "snapshots": crud.count_snapshots(db)}


@app.post("/api/v1/flags", response_model=FlagResponse, status_code=status.HTTP_201_CREATED)
def create_or_update_flag(payload: FlagUpsertRequest, db: Session = Depends(get_db)):
    try:
        flag = crud.upsert_flag(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return crud.flag_to_response(flag)


@app.get("/api/v1/flags", response_model=list[FlagResponse])
def list_flags(db: Session = Depends(get_db)):
    return [crud.flag_to_response(f) for f in crud.list_flags(db)]


@app.get("/api/v1/flags/{flag_key}", response_model=FlagResponse)
def get_flag(flag_key: str, db: Session = Depends(get_db)):
    flag = crud.get_flag_or_none(db, flag_key)
    if flag is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"开关 {flag_key!r} 不存在")
    return crud.flag_to_response(flag)


@app.delete("/api/v1/flags/{flag_key}", status_code=status.HTTP_204_NO_CONTENT)
def delete_flag(flag_key: str, db: Session = Depends(get_db)) -> None:
    if not crud.delete_flag(db, flag_key):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"开关 {flag_key!r} 不存在")


@app.put(
    "/api/v1/flags/{flag_key}/overrides/{user_id}",
    response_model=OverrideResponse,
)
def put_override(
    flag_key: str, user_id: str, req: OverrideUpsertRequest, db: Session = Depends(get_db)
):
    """写入（或覆盖）按用户的临时强制变体。

    - 开关不存在返回 ``404``；variant_key 不在开关当前配置的变体中同样返回 ``404``；
    - expires_at 缺省表示不过期；提供时必须是带时区的未来 UTC 时间，否则 ``422``
      （由 Pydantic 校验）；
    - 同一 flag_key + user_id 重复 PUT 为整体覆盖（variant_key / reason / expires_at）。
    """
    flag = crud.get_flag_or_none(db, flag_key)
    if flag is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"开关 {flag_key!r} 不存在")
    current_variant_keys = {v.key for v in crud.parse_variants(flag.variants_json)}
    if req.variant_key not in current_variant_keys:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"开关 {flag_key!r} 的当前配置中不存在变体 {req.variant_key!r}",
        )
    ov = crud.upsert_override(db, flag_key, user_id, req)
    return crud.override_to_response(ov)


@app.delete(
    "/api/v1/flags/{flag_key}/overrides/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_override(flag_key: str, user_id: str, db: Session = Depends(get_db)) -> None:
    """撤销按用户的临时强制变体。开关或 override 不存在均返回 ``404``。"""
    if crud.get_flag_or_none(db, flag_key) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"开关 {flag_key!r} 不存在")
    if not crud.delete_override(db, flag_key, user_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"开关 {flag_key!r} 不存在用户 {user_id!r} 的临时强制变体",
        )


@app.get("/api/v1/flags/{flag_key}/versions", response_model=list[FlagVersionResponse])
def list_flag_versions(flag_key: str, db: Session = Depends(get_db)):
    if crud.get_flag_or_none(db, flag_key) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"开关 {flag_key!r} 不存在")
    return [crud.version_to_response(v) for v in crud.list_flag_versions(db, flag_key)]


@app.get("/api/v1/flags/{flag_key}/versions/{version}", response_model=FlagVersionResponse)
def get_flag_version(flag_key: str, version: int, db: Session = Depends(get_db)):
    if crud.get_flag_or_none(db, flag_key) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"开关 {flag_key!r} 不存在")
    ver = crud.get_flag_version(db, flag_key, version)
    if ver is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"开关 {flag_key!r} 没有版本 {version}",
        )
    return crud.version_to_response(ver)


@app.post(
    "/api/v1/flags/{flag_key}/rollback",
    response_model=FlagResponse,
    status_code=status.HTTP_201_CREATED,
)
def rollback_flag(
    flag_key: str, req: RollbackRequest, db: Session = Depends(get_db)
):
    """将指定历史版本复制为新的当前版本（回滚发布）。

    - 同一事务中追加新版本（version+1，内容复制自 target_version）并更新当前配置；
    - 历史版本保持不可变，回滚不删除、不覆盖任何历史行；
    - ``expected_current_version`` 与实际当前版本不一致时返回 ``409``，不改动数据；
    - 开关不存在或目标历史版本不存在返回 ``404``。
    """
    try:
        flag = crud.rollback_flag(
            db,
            flag_key,
            target_version=req.target_version,
            expected_current_version=req.expected_current_version,
        )
    except crud.NotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"开关 {flag_key!r} 不存在"
        )
    except crud.VersionConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except crud.VersionNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return crud.flag_to_response(flag)


def _evaluate(req: EvaluateRequest, db: Session, flag_key: str) -> EvaluateResponse:
    flag = crud.get_flag_or_none(db, flag_key)
    if flag is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"开关 {flag_key!r} 不存在"
        )

    request_id = req.request_id or uuid.uuid4().hex

    # 幂等：相同 request_id 直接返回首次判定快照，保证重试结果一致
    existing = crud.get_snapshot(db, request_id)
    if existing is not None:
        if existing.flag_key != flag_key or existing.user_id != req.user_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"request_id={request_id} 已用于其他判定（flag_key/user_id 不一致）",
            )
        return _replay_response(db, existing, flag, req)

    # 解析本次判定使用的配置版本：缺省用最新版，指定时必须存在
    if req.config_version is None:
        rules_json, variants_json = flag.rules_json, flag.variants_json
        window_json = flag.activation_window_json
        config_version = flag.version
    else:
        ver = crud.get_flag_version(db, flag_key, req.config_version)
        if ver is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"开关 {flag_key!r} 没有版本 {req.config_version}",
            )
        rules_json, variants_json = ver.rules_json, ver.variants_json
        window_json = ver.activation_window_json
        config_version = ver.version

    rules = crud.parse_rules(rules_json)
    variants = crud.parse_variants(variants_json)

    # 生效窗口按所选配置版本的 [active_from, active_until) 判定：
    # 窗口外跳过 override、规则与分桶，直接返回兜底变体（decision_path="inactive"）。
    decision = None
    window = crud.parse_activation_window(window_json)
    if window is not None:
        now = datetime.now(timezone.utc)
        if not eng.window_is_active(window, now):
            decision = eng.inactive_decision(window, now)

    # 仅在未钉住历史版本时允许临时强制变体：未过期且变体仍存在于当前配置才生效；
    # 到期或变体已移除的记录不参与，继续走规则与分桶。
    if decision is None and req.config_version is None:
        active_override = crud.get_active_override(
            db, flag_key, req.user_id, {v.key for v in variants}
        )
        if active_override is not None:
            decision = eng.override_decision(active_override)

    if decision is None:
        decision = decide(flag.flag_key, req.user_id, req.context, variants, rules)

    try:
        crud.save_snapshot(
            db,
            request_id=request_id,
            flag_key=flag.flag_key,
            config_version=config_version,
            user_id=req.user_id,
            context=req.context,
            decision=decision,
        )
    except IntegrityError:
        # 并发下 request_id 唯一键冲突：回退到已存在的快照
        db.rollback()
        existing = crud.get_snapshot(db, request_id)
        if existing is not None:
            return _replay_response(db, existing, flag, req)
        raise

    return _decision_response(request_id, flag, req, decision, config_version)


@app.post("/api/v1/evaluate", response_model=EvaluateResponse)
def evaluate_by_body(req: EvaluateRequest, db: Session = Depends(get_db)):
    if not req.flag_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="请求体必须提供 flag_key"
        )
    return _evaluate(req, db, req.flag_key)


@app.post("/api/v1/flags/{flag_key}/evaluate", response_model=EvaluateResponse)
def evaluate_by_path(flag_key: str, req: EvaluateRequest, db: Session = Depends(get_db)):
    if req.flag_key is not None and req.flag_key != flag_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="路径 flag_key 与请求体 flag_key 不一致",
        )
    return _evaluate(req, db, flag_key)


@app.post(
    "/api/v1/flags/{flag_key}/preview-impact",
    response_model=PreviewImpactResponse,
)
def preview_impact(
    flag_key: str, req: PreviewImpactRequest, db: Session = Depends(get_db)
) -> PreviewImpactResponse:
    """发布前评估候选配置：当前版本与候选配置分别做只读判定并对比。

    - 候选 variants/rules/activation_window 沿用创建接口的全部校验（Pydantic 层返回 422）；
    - 不写入配置版本，也不写入 DecisionSnapshot；
    - 两侧判定都应用各自配置的生效窗口（同一请求共用同一判定时刻）；
    - 仅返回 variant_key / decision_path / matched_rule_index 任一发生变化的用户，
      顺序与请求一致；汇总输入数、变化数及当前变体 -> 候选变体的迁移计数。
    """
    flag = crud.get_flag_or_none(db, flag_key)
    if flag is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"开关 {flag_key!r} 不存在"
        )

    changes, migration = _compute_preview_changes(
        db,
        flag=flag,
        candidate=req,
        users=req.users,
    )

    return PreviewImpactResponse(
        flag_key=flag_key,
        current_version=flag.version,
        total_users=len(req.users),
        changed_users=len(changes),
        changes=changes,
        migration_counts=migration,
    )


def _candidate_engine_config(body: FlagConfigBody):
    """把请求中的候选配置转成引擎使用的 Rule/Variant/ActivationWindow。

    候选配置仅存在于请求内存中；RuleIn/VariantIn 与创建接口同结构、同校验。
    """
    candidate_rules = [
        eng.Rule(**r.model_dump())
        for r in sorted(body.rules, key=lambda x: x.priority)
    ]
    candidate_variants = [eng.Variant(**v.model_dump()) for v in body.variants]
    candidate_window = (
        eng.ActivationWindow(
            inactive_variant_key=body.activation_window.inactive_variant_key,
            active_from=body.activation_window.active_from,
            active_until=body.activation_window.active_until,
        )
        if body.activation_window is not None
        else None
    )
    return candidate_rules, candidate_variants, candidate_window


def _decide_with_window(variants, rules, window, now: datetime, flag_key: str, user: PreviewUser):
    """单用户只读判定：窗口外返回兜底变体，否则走正常规则 + 分桶。

    预览与发布批次共用：整个批次使用同一时刻判定窗口，保证各用户、
    各开关结果可比；预览不落 DecisionSnapshot，也不应用 override。
    """
    if window is not None and not eng.window_is_active(window, now):
        return eng.inactive_decision(window, now)
    return decide(flag_key, user.user_id, user.context, variants, rules)


def _compute_preview_changes(
    db: Session,
    *,
    flag: FlagConfig,
    candidate: FlagConfigBody,
    users: list[PreviewUser],
) -> tuple[list[PreviewChange], dict[str, dict[str, int]]]:
    """对同一批用户分别用当前配置与候选配置做只读判定并对比。

    与 ``POST .../preview-impact`` 完全同源：仅收集
    variant_key / decision_path / matched_rule_index 任一变化的用户，
    顺序与请求一致；migration 只统计变体迁移。纯读操作，不写任何表。
    """
    current_rules = crud.parse_rules(flag.rules_json)
    current_variants = crud.parse_variants(flag.variants_json)
    current_window = crud.parse_activation_window(flag.activation_window_json)
    candidate_rules, candidate_variants, candidate_window = _candidate_engine_config(candidate)

    now = datetime.now(timezone.utc)
    changes: list[PreviewChange] = []
    migration: dict[str, dict[str, int]] = {}
    for user in users:
        current_decision = _decide_with_window(
            current_variants, current_rules, current_window, now, flag.flag_key, user
        )
        candidate_decision = _decide_with_window(
            candidate_variants, candidate_rules, candidate_window, now, flag.flag_key, user
        )

        current_outcome = PreviewOutcome(
            variant_key=current_decision.variant_key,
            decision_path=current_decision.decision_path,
            matched_rule_index=current_decision.matched_rule_index,
        )
        candidate_outcome = PreviewOutcome(
            variant_key=candidate_decision.variant_key,
            decision_path=candidate_decision.decision_path,
            matched_rule_index=candidate_decision.matched_rule_index,
        )

        if (
            current_outcome.variant_key != candidate_outcome.variant_key
            or current_outcome.decision_path != candidate_outcome.decision_path
            or current_outcome.matched_rule_index != candidate_outcome.matched_rule_index
        ):
            changes.append(
                PreviewChange(
                    user_id=user.user_id,
                    current=current_outcome,
                    candidate=candidate_outcome,
                )
            )
            # 仅变体迁移计入汇总；变体不变（仅判定路径 / 命中规则变化）不算迁移
            if current_outcome.variant_key != candidate_outcome.variant_key:
                migration.setdefault(current_outcome.variant_key, {})
                bucket = migration[current_outcome.variant_key]
                bucket[candidate_outcome.variant_key] = (
                    bucket.get(candidate_outcome.variant_key, 0) + 1
                )

    return changes, migration


# ---------------------------------------------------------------------------
# 多开关发布批次
# ---------------------------------------------------------------------------


@app.post(
    "/api/v1/releases",
    response_model=ReleaseResponse,
    status_code=status.HTTP_201_CREATED,
    summary="创建多开关发布批次（预览 + 乐观版本锁定，不改动当前配置）",
)
def create_release(req: ReleaseCreateRequest, response: Response, db: Session = Depends(get_db)):
    """接收多个开关的候选配置与共用预览用户，生成**待提交**批次。

    - 每个 flag 项的 ``variants`` / ``rules`` / ``activation_window`` 沿用创建
      接口的全部校验（Pydantic 层返回 ``422``）；批次内 ``flag_key`` 不可重复；
      ``users`` 为 1～500 个互异用户，批次内各开关共用同一批预览用户；
    - 创建时逐开关核对存在性与 ``expected_version``：开关不存在返回 ``404``，
      当前版本与期望版本不一致返回 ``409``，均不创建批次；
    - 复用 ``preview-impact`` 的只读判定逻辑生成逐开关影响预览摘要，
      **不改动任何当前配置、不追加版本、不写判定快照**；
    - 携带相同 ``idempotency_key`` 与相同请求体重放时返回首次创建结果
      （首次新建为 ``201``，幂等重放为 ``200``）；相同键搭配不同请求体返回 ``409``。
    """
    # Pydantic 序列化会把 activation_window 的 datetime 统一输出为 ISO 8601
    fingerprint = (
        req.model_dump_json(exclude={"idempotency_key"})
    )

    if req.idempotency_key is not None:
        existing = crud.get_release_by_idempotency_key(db, req.idempotency_key)
        if existing is not None:
            if existing.request_fingerprint != fingerprint:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"idempotency_key={req.idempotency_key!r} 已用于内容不同的发布批次",
                )
            response.status_code = status.HTTP_200_OK
            items = crud.list_release_items(db, existing.release_id)
            return crud.release_to_response(existing, items)

    prepared: list[dict] = []
    for candidate in req.flags:
        flag = crud.get_flag_or_none(db, candidate.flag_key)
        if flag is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"开关 {candidate.flag_key!r} 不存在",
            )
        if flag.version != candidate.expected_version:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"开关 {candidate.flag_key!r} 当前版本为 {flag.version}，与期望版本 "
                    f"{candidate.expected_version} 不一致"
                ),
            )
        changes, migration = _compute_preview_changes(
            db, flag=flag, candidate=candidate, users=req.users
        )
        prepared.append(
            {
                "candidate": candidate,
                "baseline_version": flag.version,
                "changes": changes,
                "migration_counts": migration,
            }
        )

    users_payload = [u.model_dump() for u in req.users]
    try:
        release, created = crud.create_release(
            db,
            idempotency_key=req.idempotency_key,
            fingerprint=fingerprint,
            users=users_payload,
            prepared=prepared,
        )
    except IntegrityError:
        # 并发下相同 idempotency_key 唯一键冲突：回退到已存在批次（首次结果）
        db.rollback()
        existing = crud.get_release_by_idempotency_key(db, req.idempotency_key)
        if existing is not None:
            if existing.request_fingerprint != fingerprint:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"idempotency_key={req.idempotency_key!r} 已用于内容不同的发布批次",
                )
            response.status_code = status.HTTP_200_OK
            items = crud.list_release_items(db, existing.release_id)
            return crud.release_to_response(existing, items)
        raise

    response.status_code = (
        status.HTTP_201_CREATED if created else status.HTTP_200_OK
    )
    items = crud.list_release_items(db, release.release_id)
    return crud.release_to_response(release, items)


@app.post(
    "/api/v1/releases/{release_id}/commit",
    response_model=ReleaseResponse,
    summary="提交发布批次（单事务再核版本、原子发布；重复提交幂等）",
)
def commit_release(release_id: str, db: Session = Depends(get_db)):
    """在**单一数据库事务**中核对并发布整个批次。

    - 事务内再次核对全部开关的当前版本：开关已不存在或当前版本与
      ``expected_version`` 不一致即返回 ``409``，整事务回滚，**不得部分写入**
      （不会有任何开关追加版本、批次保持 ``pending``）；
    - 全部一致时在同一事务内为各开关追加不可变版本（``version+1``）并更新
      当前配置，回填每开关的 ``committed_version``，批次翻为 ``committed``；
    - 对同一批次重复提交为幂等操作：已提交则直接返回首次提交结果，
      不会再次推进版本号；
    - 批次不存在返回 ``404``。
    """
    try:
        release, items = crud.commit_release(db, release_id)
    except crud.ReleaseNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"发布批次 {release_id!r} 不存在",
        )
    except crud.VersionConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return crud.release_to_response(release, items)


@app.get(
    "/api/v1/releases/{release_id}",
    response_model=ReleaseResponse,
    summary="查询发布批次状态与逐开关预览摘要",
)
def get_release(release_id: str, db: Session = Depends(get_db)):
    """返回批次状态（``pending`` / ``committed``）、逐开关影响预览摘要，
    以及提交成功后各开关新追加的版本号（``committed_version``）。

    批次不存在返回 ``404``。
    """
    release = crud.get_release(db, release_id)
    if release is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"发布批次 {release_id!r} 不存在",
        )
    items = crud.list_release_items(db, release_id)
    return crud.release_to_response(release, items)


@app.get(
    "/api/v1/decisions",
    response_model=DecisionListResponse,
    summary="判定快照审计列表（不透明游标分页）",
)
def list_decisions(
    flag_key: str | None = Query(default=None, max_length=128),
    user_id: str | None = Query(default=None, max_length=128),
    variant_key: str | None = Query(default=None, max_length=128),
    decided_from: str | None = Query(default=None, description="判定时间下界（含），ISO 8601"),
    decided_to: str | None = Query(default=None, description="判定时间上界（含），ISO 8601"),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None, description="上一页返回的不透明 next_cursor"),
    db: Session = Depends(get_db),
) -> DecisionListResponse:
    """按 (decided_at DESC, request_id DESC) 键集游标分页返回判定快照。

    - 支持 ``flag_key`` / ``user_id`` / ``variant_key`` 与
      ``decided_from`` / ``decided_to`` 时间区间的组合过滤；
    - ``limit`` 取 1～100，默认 20；
    - 游标记录排序边界并绑定本次过滤条件：翻页时过滤条件必须与首页一致，
      否则连同损坏游标、倒置时间区间一起返回 422；
    - 以首页请求发起时全表最大自增 id 为上界高水位，翻页期间新写入的
      快照不会进入后续页；
    - 无更多数据时 ``next_cursor`` 为 ``null``。
    """

    def _parse_dt(name: str, raw: str) -> datetime:
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{name} 必须是 ISO 8601 时间，例如 2026-09-17T08:00:00Z",
            ) from exc
        # 统一成 naive UTC 比较，与 SQLAlchemy 从 SQLite 读出的 decided_at 形态一致
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt

    dt_from = _parse_dt("decided_from", decided_from) if decided_from is not None else None
    dt_to = _parse_dt("decided_to", decided_to) if decided_to is not None else None
    if dt_from is not None and dt_to is not None and dt_from > dt_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="时间区间倒置：decided_from 不得晚于 decided_to",
        )

    filters = cursor_mod.CursorFilter(
        flag_key=flag_key,
        user_id=user_id,
        variant_key=variant_key,
        decided_from=dt_from,
        decided_to=dt_to,
    )

    boundary_ts = boundary_rid = None
    max_id = None
    if cursor is not None:
        if not cursor:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="游标损坏或已失效"
            )
        try:
            decoded = cursor_mod.decode_cursor(cursor)
        except cursor_mod.InvalidCursor as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"游标损坏或已失效：{exc}"
            ) from exc
        # 游标绑定本次过滤条件：任一字段不一致即拒绝（时间按规范化后的 UTC 比较）
        if decoded.filters != filters:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="游标与本次过滤条件不一致：翻页必须沿用首页的 flag_key/user_id/variant_key/decided_from/decided_to",
            )
        boundary_ts = decoded.boundary_ts
        boundary_rid = decoded.boundary_request_id
        max_id = decoded.watermark_id
    else:
        # 首页：在列表查询前冻结高水位，窗口固定为请求发起时已存在的行
        max_id = crud.max_snapshot_id(db)

    rows = list(
        crud.list_snapshots(
            db,
            flag_key=flag_key,
            user_id=user_id,
            variant_key=variant_key,
            decided_from=dt_from,
            decided_to=dt_to,
            limit=limit,
            boundary_ts=boundary_ts,
            boundary_request_id=boundary_rid,
            max_id=max_id,
        )
    )

    has_more = len(rows) > limit
    page = rows[:limit]
    # 首页游标携带上面冻结的 hwm；后续页沿用游标中的 hwm，窗口不漂移。
    # 翻页期间新写入快照的自增 id 必大于 hwm，被 id <= hwm 排除。

    next_cursor = None
    if has_more and page:
        # 有行返回时 hwm 必然非空（首页前已冻结、后续页由游标携带）
        assert max_id is not None
        last = page[-1]
        next_cursor = cursor_mod.encode_cursor(
            boundary_ts=last.decided_at,
            boundary_request_id=last.request_id,
            watermark_id=max_id,
            filters=filters,
        )

    return DecisionListResponse(
        items=[_snapshot_to_response(snap) for snap in page],
        next_cursor=next_cursor,
    )


@app.get("/api/v1/decisions/{request_id}")
def get_decision(request_id: str, db: Session = Depends(get_db)):
    snap = crud.get_snapshot(db, request_id)
    if snap is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"未找到 request_id={request_id} 的判定快照"
        )
    return _snapshot_to_response(snap)
