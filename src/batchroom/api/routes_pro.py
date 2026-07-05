"""Pro endpoints: license management and user administration."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..pro.license import parse_license, remove_license, save_license
from ..pro.users import UserError, create_user, delete_user, list_users, rotate_api_key, update_user
from .auth import (
    features,
    invalidate_user_cache,
    license_info,
    refresh_license,
    require_admin,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# license
# ---------------------------------------------------------------------------


@router.get("/license")
def get_license(request: Request) -> dict:
    return license_info(request.app).to_public_dict()


class LicenseBody(BaseModel):
    key: str = Field(min_length=8, max_length=8192)


@router.post("/license", dependencies=[Depends(require_admin)])
def install_license(request: Request, body: LicenseBody) -> dict:
    info = parse_license(body.key)
    if not info.valid:
        raise HTTPException(status_code=400, detail=f"invalid license: {info.error}")
    if info.expired:
        raise HTTPException(
            status_code=400,
            detail=f"this license expired on {info.expires} — request a renewal",
        )
    save_license(request.app.state.settings, body.key)
    refresh_license(request.app)
    return license_info(request.app).to_public_dict()


@router.delete("/license", dependencies=[Depends(require_admin)])
def delete_license_key(request: Request) -> dict:
    removed = remove_license(request.app.state.settings)
    refresh_license(request.app)
    return {"removed": removed, **license_info(request.app).to_public_dict()}


# ---------------------------------------------------------------------------
# users (admin + multi_user feature)
# ---------------------------------------------------------------------------


def _require_multiuser_feature(request: Request) -> None:
    if "multi_user" not in features(request.app):
        raise HTTPException(
            status_code=402,
            detail={
                "detail": "multi-user management requires an active Pro license",
                "feature": "multi_user",
            },
        )


@router.get("/users", dependencies=[Depends(require_admin)])
def get_users(request: Request) -> dict:
    # listing stays available even if the license lapsed (read-only view)
    return {"users": list_users(request.app.state.db), "seats": license_info(request.app).seats}


class CreateUserBody(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    name: str = Field(default="", max_length=200)
    password: str = Field(min_length=8, max_length=1024)
    role: str = Field(default="member", pattern="^(admin|member)$")


@router.post("/users", status_code=201, dependencies=[Depends(require_admin)])
def post_user(request: Request, body: CreateUserBody) -> dict:
    _require_multiuser_feature(request)
    try:
        user = create_user(
            request.app.state.db,
            email=body.email,
            name=body.name,
            password=body.password,
            role=body.role,
            seat_limit=license_info(request.app).seats,
        )
    except UserError as exc:
        detail = {"detail": exc.message}
        if exc.hint:
            detail["hint"] = exc.hint
        raise HTTPException(status_code=400, detail=detail) from exc
    invalidate_user_cache(request.app)
    return user


class PatchUserBody(BaseModel):
    role: str | None = Field(default=None, pattern="^(admin|member)$")
    password: str | None = Field(default=None, min_length=8, max_length=1024)
    disabled: bool | None = None
    name: str | None = Field(default=None, max_length=200)


@router.patch("/users/{user_id}", dependencies=[Depends(require_admin)])
def patch_user(request: Request, user_id: int, body: PatchUserBody) -> dict:
    _require_multiuser_feature(request)
    try:
        user = update_user(
            request.app.state.db, user_id,
            role=body.role, password=body.password, disabled=body.disabled, name=body.name,
        )
    except UserError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    invalidate_user_cache(request.app)
    return user


@router.delete("/users/{user_id}", dependencies=[Depends(require_admin)])
def remove_user(request: Request, user_id: int) -> dict:
    _require_multiuser_feature(request)
    try:
        delete_user(request.app.state.db, user_id)
    except UserError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    invalidate_user_cache(request.app)
    return {"deleted": user_id}


@router.post("/users/{user_id}/api-key", dependencies=[Depends(require_admin)])
def new_api_key(request: Request, user_id: int) -> dict:
    _require_multiuser_feature(request)
    try:
        raw = rotate_api_key(request.app.state.db, user_id)
    except UserError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    return {"api_key": raw, "note": "shown once — store it now"}
