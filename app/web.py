from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import config


class CachedStatic(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            immutable = path.endswith(".woff2")
            response.headers["Cache-Control"] = (
                "public, max-age=31536000, immutable" if immutable else "public, max-age=86400"
            )
        return response


def mount_fonts(app: FastAPI) -> None:
    app.mount("/fonts", CachedStatic(directory=config.STATIC / "fonts"), name="fonts")
