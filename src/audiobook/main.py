import uvicorn

from audiobook.api.app import create_app
from audiobook.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(create_app(), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
