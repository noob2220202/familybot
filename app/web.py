from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
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


def create_app(headers: dict[str, str]) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/fonts", CachedStatic(directory=config.STATIC / "fonts"), name="fonts")

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, exc: RequestValidationError):
        return JSONResponse({"detail": "입력값을 확인하세요."}, 422, headers=headers)

    @app.exception_handler(Exception)
    async def crashed(request: Request, exc: Exception):
        return JSONResponse({"detail": "서버 오류가 발생했습니다. 잠시 후 다시 시도하세요."}, 500, headers=headers)

    return app
