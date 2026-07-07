"""
FastAPI application — server entry point.

Start with: uvicorn rf_agent.api.server:app --port 8001 --reload
"""

import sys
import asyncio
import os

if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

from dotenv import load_dotenv
load_dotenv()

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from rf_agent.platform.mission_control import register_agent, heartbeat_loop
from rf_agent.api.routes_generation import router as gen_router
from rf_agent.api.routes_execution import router as exec_router

app = FastAPI(title="RF Generator — OmniPlatform")

os.makedirs("frontend", exist_ok=True)
os.makedirs("output/robot_files", exist_ok=True)
os.makedirs("output/rf_reports", exist_ok=True)
os.makedirs("output/screenshots", exist_ok=True)

app.mount("/static", StaticFiles(directory="frontend"), name="static")
app.mount("/output", StaticFiles(directory="output"), name="output")

app.include_router(gen_router)
app.include_router(exec_router)


@app.on_event("startup")
async def startup():
    await register_agent()
    asyncio.create_task(heartbeat_loop())


@app.get("/")
async def serve_ui():
    return FileResponse("frontend/index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("rf_agent.api.server:app", host="127.0.0.1", port=8001, reload=False)
