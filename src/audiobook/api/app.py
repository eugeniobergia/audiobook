from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI(
        title="Audiobook Catalog",
        description="Ingest audiobooks from the iTunes Search API, then browse, "
        "search and export them.",
    )

    @app.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
