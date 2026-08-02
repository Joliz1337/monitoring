import asyncio
import logging
from typing import AsyncGenerator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from sqlalchemy import func, delete

from app.database import get_db
from app.models import SharedNote, SharedTask
from app.auth import verify_auth
from app.services.notes_broadcaster import get_notes_broadcaster

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/notes", tags=["notes"])


class NoteUpdate(BaseModel):
    content: str
    version: int


class TaskCreate(BaseModel):
    text: str


class TaskToggle(BaseModel):
    is_done: bool


async def _get_or_create_note(db: AsyncSession) -> SharedNote:
    result = await db.execute(select(SharedNote).where(SharedNote.id == 1))
    note = result.scalar_one_or_none()
    if not note:
        note = SharedNote(id=1, content="", version=1)
        db.add(note)
        await db.commit()
        await db.refresh(note)
    return note


@router.get("/content")
async def get_note_content(
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_auth),
):
    note = await _get_or_create_note(db)
    return {"content": note.content, "version": note.version}


@router.post("/content")
async def save_note_content(
    data: NoteUpdate,
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_auth),
):
    note = await _get_or_create_note(db)

    if data.version < note.version:
        return {"status": "conflict", "content": note.content, "version": note.version}

    note.content = data.content
    note.version = note.version + 1
    await db.commit()
    await db.refresh(note)

    broadcaster = get_notes_broadcaster()
    await broadcaster.broadcast("note_update", {"content": note.content, "version": note.version})

    return {"status": "ok", "version": note.version}


# ==================== Tasks ====================

async def _tasks_to_list(db: AsyncSession) -> list[dict]:
    result = await db.execute(select(SharedTask).order_by(SharedTask.position, SharedTask.id))
    return [
        {"id": t.id, "text": t.text, "is_done": t.is_done}
        for t in result.scalars().all()
    ]


@router.get("/tasks")
async def get_tasks(db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    return {"tasks": await _tasks_to_list(db)}


@router.post("/tasks")
async def create_task(data: TaskCreate, db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    max_pos = await db.execute(select(func.coalesce(func.max(SharedTask.position), -1)))
    position = max_pos.scalar() + 1

    task = SharedTask(text=data.text, position=position)
    db.add(task)
    await db.commit()

    tasks = await _tasks_to_list(db)
    broadcaster = get_notes_broadcaster()
    await broadcaster.broadcast("tasks_changed", {"tasks": tasks})
    return {"success": True, "tasks": tasks}


@router.put("/tasks/{task_id}")
async def toggle_task(task_id: int, data: TaskToggle, db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    result = await db.execute(select(SharedTask).where(SharedTask.id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        from fastapi import HTTPException
        raise HTTPException(404, "Task not found")

    task.is_done = data.is_done
    await db.commit()

    tasks = await _tasks_to_list(db)
    broadcaster = get_notes_broadcaster()
    await broadcaster.broadcast("tasks_changed", {"tasks": tasks})
    return {"success": True, "tasks": tasks}


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: int, db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    await db.execute(delete(SharedTask).where(SharedTask.id == task_id))
    await db.commit()

    tasks = await _tasks_to_list(db)
    broadcaster = get_notes_broadcaster()
    await broadcaster.broadcast("tasks_changed", {"tasks": tasks})
    return {"success": True, "tasks": tasks}


@router.get("/stream")
async def stream_note_updates(_=Depends(verify_auth)):
    broadcaster = get_notes_broadcaster()
    listener_id, queue = broadcaster.subscribe()

    async def event_generator() -> AsyncGenerator[bytes, None]:
        try:
            yield b"event: connected\ndata: {}\n\n"
            while True:
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield f"event: update\ndata: {data}\n\n".encode()
                except asyncio.TimeoutError:
                    yield b": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            broadcaster.unsubscribe(listener_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
