from fastapi import APIRouter

# Public on purpose: monitoring and load balancers call it without a key.
router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
