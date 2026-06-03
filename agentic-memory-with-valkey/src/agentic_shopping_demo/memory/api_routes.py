"""
REST API endpoints for memory operations.
"""

import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from agentic_shopping_demo.memory.client import get_memory_client
from agentic_shopping_demo.memory.models import MemoryType
from agentic_shopping_demo.memory.privacy import export_user_data

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/memory", tags=["memory"])


# Request/Response models
class AddMemoryRequest(BaseModel):
    messages: list[dict[str, str]]
    user_id: str
    metadata: Optional[dict[str, Any]] = None
    memory_type: str = "long_term"


class SearchMemoryRequest(BaseModel):
    query: str
    user_id: str
    limit: int = 5
    filters: Optional[dict[str, Any]] = None


class UpdateMemoryRequest(BaseModel):
    data: dict[str, Any]


# Endpoints
@router.post("/add")
async def add_memory(req: AddMemoryRequest):
    """
    Add a new memory.
    
    POST /memory/add
    {
        "messages": [{"role": "user", "content": "..."}],
        "user_id": "user:123",
        "metadata": {"domain": "commerce"},
        "memory_type": "long_term"
    }
    """
    try:
        client = get_memory_client()
        
        # Validate memory type
        try:
            memory_type = MemoryType(req.memory_type)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid memory_type: {req.memory_type}"
            )
        
        # Add memory
        memory_id = client.add(
            messages=req.messages,
            user_id=req.user_id,
            metadata=req.metadata,
            memory_type=memory_type
        )
        
        return {
            "success": True,
            "memory_id": memory_id
        }
        
    except Exception as e:
        logger.error(f"[MEMORY API] Add failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/search")
async def search_memories(req: SearchMemoryRequest):
    """
    Search for relevant memories.
    
    POST /memory/search
    {
        "query": "running shoes",
        "user_id": "user:123",
        "limit": 5,
        "filters": {"domain": "commerce"}
    }
    """
    try:
        client = get_memory_client()
        
        # Search memories
        memories = client.search(
            query=req.query,
            user_id=req.user_id,
            limit=req.limit,
            filters=req.filters
        )
        
        return {
            "success": True,
            "count": len(memories),
            "memories": [
                {
                    "id": m.id,
                    "content": m.content,
                    "type": m.memory_type.value,
                    "relevance_score": m.relevance_score,
                    "created_at": m.created_at.isoformat(),
                    "metadata": m.metadata
                }
                for m in memories
            ]
        }
        
    except Exception as e:
        logger.error(f"[MEMORY API] Search failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/{memory_id}")
async def update_memory(memory_id: str, req: UpdateMemoryRequest):
    """
    Update an existing memory.
    
    PATCH /memory/{memory_id}
    {
        "data": {"metadata": {"updated": true}}
    }
    """
    try:
        client = get_memory_client()
        
        # Update memory
        success = client.update(
            memory_id=memory_id,
            data=req.data
        )
        
        if not success:
            raise HTTPException(
                status_code=404,
                detail=f"Memory not found: {memory_id}"
            )
        
        return {
            "success": True,
            "memory_id": memory_id
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[MEMORY API] Update failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{memory_id}")
async def delete_memory(memory_id: str):
    """
    Delete a specific memory.
    
    DELETE /memory/{memory_id}
    """
    try:
        client = get_memory_client()
        
        # Delete memory
        count = client.delete(memory_id=memory_id)
        
        if count == 0:
            raise HTTPException(
                status_code=404,
                detail=f"Memory not found: {memory_id}"
            )
        
        return {
            "success": True,
            "memory_id": memory_id
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[MEMORY API] Delete failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/user/{user_id}")
async def delete_user_memories(user_id: str):
    """
    Delete all memories for a user.
    
    DELETE /memory/user/{user_id}
    """
    try:
        client = get_memory_client()
        
        # Delete all memories
        count = client.delete(user_id=user_id)
        
        return {
            "success": True,
            "user_id": user_id,
            "deleted_count": count
        }
        
    except Exception as e:
        logger.error(f"[MEMORY API] Bulk delete failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/export/{user_id}")
async def export_memories(user_id: str):
    """
    Export all memories for a user (GDPR compliance).
    
    GET /memory/export/{user_id}
    """
    try:
        # Export user data
        export_data = export_user_data(user_id=user_id)
        
        if "error" in export_data:
            raise HTTPException(
                status_code=500,
                detail=export_data["error"]
            )
        
        return export_data
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[MEMORY API] Export failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/health")
async def health_check():
    """
    Check memory system health.
    
    GET /memory/health
    """
    try:
        client = get_memory_client()
        health = client.health_check()
        
        return health
        
    except Exception as e:
        logger.error(f"[MEMORY API] Health check failed: {e}")
        return {
            "status": "unhealthy",
            "error": str(e)
        }
