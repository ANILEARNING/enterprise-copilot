from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from .routes import public_router, router

app = FastAPI(title="Enterprise Copilot")
app.include_router(router)
app.include_router(public_router)

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # Never leak stack traces or internals to the client.
    return JSONResponse(status_code=500, content={"detail": "Internal server error."})

app.mount("/", StaticFiles(directory="static", html=True), name="static")
