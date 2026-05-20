from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update, delete, func
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from typing import Optional

from app.database import get_db
from app.models import InfraAccount, InfraProject, InfraProjectServer, Server
from app.auth import verify_auth

router = APIRouter(prefix="/infra", tags=["infra"], dependencies=[Depends(verify_auth)])


# ==================== Schemas ====================

class AccountCreate(BaseModel):
    name: str

class AccountUpdate(BaseModel):
    name: Optional[str] = None

class ProjectCreate(BaseModel):
    account_id: int
    name: str

class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    account_id: Optional[int] = None

class AddServer(BaseModel):
    server_id: int


# ==================== Tree ====================

@router.get("/tree")
async def get_tree(db: AsyncSession = Depends(get_db)):
    accounts_result = await db.execute(
        select(InfraAccount).order_by(InfraAccount.position, InfraAccount.id)
    )
    accounts = accounts_result.scalars().all()

    projects_result = await db.execute(
        select(InfraProject).order_by(InfraProject.position, InfraProject.id)
    )
    projects = projects_result.scalars().all()

    links_result = await db.execute(
        select(InfraProjectServer).order_by(InfraProjectServer.position, InfraProjectServer.id)
    )
    links = links_result.scalars().all()

    projects_by_account: dict[int, list] = {}
    for p in projects:
        projects_by_account.setdefault(p.account_id, []).append(p)

    server_ids_by_project: dict[int, list[int]] = {}
    assigned_server_ids: set[int] = set()
    for link in links:
        server_ids_by_project.setdefault(link.project_id, []).append(link.server_id)
        assigned_server_ids.add(link.server_id)

    all_servers_result = await db.execute(select(Server.id))
    all_server_ids = {row[0] for row in all_servers_result.all()}
    unassigned = sorted(all_server_ids - assigned_server_ids)

    tree = []
    for acc in accounts:
        acc_projects = []
        for proj in projects_by_account.get(acc.id, []):
            acc_projects.append({
                "id": proj.id,
                "name": proj.name,
                "position": proj.position,
                "server_ids": server_ids_by_project.get(proj.id, []),
            })
        tree.append({
            "id": acc.id,
            "name": acc.name,
            "position": acc.position,
            "projects": acc_projects,
        })

    return {"accounts": tree, "unassigned_server_ids": unassigned}


# ==================== Accounts ====================

@router.post("/accounts")
async def create_account(data: AccountCreate, db: AsyncSession = Depends(get_db)):
    max_pos = await db.execute(select(func.coalesce(func.max(InfraAccount.position), -1)))
    position = max_pos.scalar() + 1

    account = InfraAccount(name=data.name, position=position)
    db.add(account)
    await db.commit()
    await db.refresh(account)

    return {
        "success": True,
        "account": {"id": account.id, "name": account.name, "position": account.position, "projects": []},
    }


@router.put("/accounts/{account_id}")
async def update_account(account_id: int, data: AccountUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(InfraAccount).where(InfraAccount.id == account_id))
    account = result.scalar_one_or_none()
    if not account:
        raise HTTPException(404, "Account not found")

    if data.name is not None:
        account.name = data.name
    await db.commit()
    return {"success": True}


@router.delete("/accounts/{account_id}")
async def delete_account(account_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(InfraAccount).where(InfraAccount.id == account_id))
    if not result.scalar_one_or_none():
        raise HTTPException(404, "Account not found")

    await db.execute(delete(InfraAccount).where(InfraAccount.id == account_id))
    await db.commit()
    return {"success": True}


# ==================== Projects ====================

@router.post("/projects")
async def create_project(data: ProjectCreate, db: AsyncSession = Depends(get_db)):
    acc_check = await db.execute(select(InfraAccount.id).where(InfraAccount.id == data.account_id))
    if not acc_check.scalar_one_or_none():
        raise HTTPException(404, "Account not found")

    max_pos = await db.execute(
        select(func.coalesce(func.max(InfraProject.position), -1))
        .where(InfraProject.account_id == data.account_id)
    )
    position = max_pos.scalar() + 1

    project = InfraProject(account_id=data.account_id, name=data.name, position=position)
    db.add(project)
    await db.commit()
    await db.refresh(project)

    return {
        "success": True,
        "project": {"id": project.id, "name": project.name, "position": project.position, "server_ids": []},
    }


@router.put("/projects/{project_id}")
async def update_project(project_id: int, data: ProjectUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(InfraProject).where(InfraProject.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(404, "Project not found")

    if data.name is not None:
        project.name = data.name
    if data.account_id is not None:
        acc_check = await db.execute(select(InfraAccount.id).where(InfraAccount.id == data.account_id))
        if not acc_check.scalar_one_or_none():
            raise HTTPException(404, "Target account not found")
        project.account_id = data.account_id
    await db.commit()
    return {"success": True}


@router.delete("/projects/{project_id}")
async def delete_project(project_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(InfraProject).where(InfraProject.id == project_id))
    if not result.scalar_one_or_none():
        raise HTTPException(404, "Project not found")

    await db.execute(delete(InfraProject).where(InfraProject.id == project_id))
    await db.commit()
    return {"success": True}


# ==================== Project ↔ Server links ====================

@router.post("/projects/{project_id}/servers")
async def add_server_to_project(project_id: int, data: AddServer, db: AsyncSession = Depends(get_db)):
    proj_check = await db.execute(select(InfraProject.id).where(InfraProject.id == project_id))
    if not proj_check.scalar_one_or_none():
        raise HTTPException(404, "Project not found")

    srv_check = await db.execute(select(Server.id).where(Server.id == data.server_id))
    if not srv_check.scalar_one_or_none():
        raise HTTPException(404, "Server not found")

    existing = await db.execute(
        select(InfraProjectServer)
        .where(InfraProjectServer.project_id == project_id, InfraProjectServer.server_id == data.server_id)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(409, "Server already in this project")

    max_pos = await db.execute(
        select(func.coalesce(func.max(InfraProjectServer.position), -1))
        .where(InfraProjectServer.project_id == project_id)
    )
    position = max_pos.scalar() + 1

    link = InfraProjectServer(project_id=project_id, server_id=data.server_id, position=position)
    db.add(link)
    await db.commit()
    return {"success": True}


@router.delete("/projects/{project_id}/servers/{server_id}")
async def remove_server_from_project(project_id: int, server_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(InfraProjectServer)
        .where(InfraProjectServer.project_id == project_id, InfraProjectServer.server_id == server_id)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(404, "Link not found")

    await db.execute(
        delete(InfraProjectServer)
        .where(InfraProjectServer.project_id == project_id, InfraProjectServer.server_id == server_id)
    )
    await db.commit()
    return {"success": True}
