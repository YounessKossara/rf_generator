import asyncio
import json
import os
import httpx
from dotenv import load_dotenv

load_dotenv()

MC_BASE_URL = os.getenv("MC_BASE_URL", "http://localhost:3000")
MC_API_KEY  = os.getenv("MC_API_KEY", "")

# Track processed task IDs to avoid reprocessing
processed_task_ids: set = set()


async def register_agent():
    """Register youness agent in Mission Control."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            res = await client.post(
                f"{MC_BASE_URL}/api/agents/register",
                headers={"Authorization": f"Bearer {MC_API_KEY}"},
                json={
                    "name": "youness",
                    "role": "tester",
                    "capabilities": [
                        "rf-generation",
                        "rf-execution", 
                        "test-parsing",
                        "report-generation",
                        "trello-integration"
                    ],
                    "framework": "python-fastapi"
                }
            )
            print(f"✅ Youness registered in Mission Control (status {res.status_code})")
    except Exception as e:
        print(f"⚠️  Mission Control not available — running standalone. ({e})")


async def update_status(status: str):
    """Update agent status : idle or busy."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.put(
                f"{MC_BASE_URL}/api/agents",
                headers={"Authorization": f"Bearer {MC_API_KEY}"},
                json={"name": "youness", "status": status}
            )
    except Exception:
        pass


async def create_task(title: str, description: str,
                      assigned_to: str, priority: str = "high"):
    """Create a task in MC for another agent (handoff)."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            res = await client.post(
                f"{MC_BASE_URL}/api/tasks",
                headers={"Authorization": f"Bearer {MC_API_KEY}"},
                json={
                    "title": title,
                    "description": description,
                    "priority": priority,
                    "assigned_to": assigned_to
                }
            )
            print(f"✅ MC Task created → {assigned_to} : {title}")
            return res.json()
    except Exception as e:
        print(f"⚠️ MC Task creation failed: {e}")
        return None


async def complete_task(task_id: int, results: dict):
    """Mark a task as review with results."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.put(
                f"{MC_BASE_URL}/api/tasks",
                headers={"Authorization": f"Bearer {MC_API_KEY}"},
                json={
                    "id": task_id,
                    "status": "review",
                    "resolution": json.dumps({
                        "agent": "youness",
                        "total":  results.get("execution", {}).get("total", 0),
                        "passed": results.get("execution", {}).get("passed", 0),
                        "failed": results.get("execution", {}).get("failed", 0),
                        "healed": len(results.get("healing", {}).get("healed_tests", [])),
                        "report_url": f"http://localhost:8001/api/report/{results.get('test_name', '')}"
                    }, ensure_ascii=False)
                }
            )
            print(f"✅ MC Task {task_id} marked as review")
    except Exception as e:
        print(f"⚠️ Could not complete MC task {task_id}: {e}")


async def handoff_to_next_agent(md_content: str, base_url: str,
                                 results: dict,
                                 next_agent: str = "conformity-agent"):
    """Send handoff task to the next agent in the pipeline."""
    await create_task(
        title="Execute conformity tests",
        description=json.dumps({
            "md_content": md_content,
            "base_url":   base_url,
            "rf_summary": {
                "passed": results.get("execution", {}).get("passed", 0),
                "failed": results.get("execution", {}).get("failed", 0),
                "healed": len(results.get("healing", {}).get("healed_tests", []))
            }
        }, ensure_ascii=False),
        assigned_to=next_agent,
        priority="high"
    )
    print(f"✅ Handoff created → {next_agent}")


async def handle_incoming_task(task_id: int):
    """
    Process a task received from Mission Control pipeline.
    Called when heartbeat returns assigned_tasks.
    """
    # Import here to avoid circular imports
    from main import RFRequest, generate_rf

    # Skip already processed
    if task_id in processed_task_ids:
        return
    processed_task_ids.add(task_id)

    # Fetch full task from Mission Control
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            res = await client.get(
                f"{MC_BASE_URL}/api/tasks/{task_id}",
                headers={"Authorization": f"Bearer {MC_API_KEY}"}
            )
            full_task = res.json().get("task", {})
            description = full_task.get("description", "")
    except Exception as e:
        print(f"⚠️ Could not fetch task {task_id}: {e}")
        return

    # Extract md_content and base_url
    try:
        data = json.loads(description)
        base_url = data.get("base_url", "http://localhost")
        if "md_file_path" in data:
            try:
                with open(data["md_file_path"], "r", encoding="utf-8") as f:
                    md_content = f.read()
            except Exception as e:
                print(f"⚠️ Error reading md file: {e}")
                md_content = data.get("md_content", "")
        else:
            md_content = data.get("md_content", "")
    except Exception:
        print(f"⚠️ Could not parse MC task message")
        return

    if not md_content:
        print(f"⚠️ MC task has no md_content — skipping")
        return

    print(f"📬 Pipeline task received → starting RF pipeline")

    # Run the RF pipeline
    req     = RFRequest(markdown_content=md_content, base_url=base_url)
    results = await generate_rf(req)

    # Complete task in MC
    if task_id:
        await complete_task(task_id, results)

    # Handoff to next agent
    await handoff_to_next_agent(md_content, base_url, results)


async def heartbeat_loop():
    """
    Send heartbeat every 30s.
    Process any incoming pipeline tasks from work_items.
    """
    while True:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                res = await client.post(
                    f"{MC_BASE_URL}/api/agents/youness/heartbeat",
                    headers={"Authorization": f"Bearer {MC_API_KEY}"},
                    json={
                        "token_usage": {
                            "model": "llama-3.3-70b-versatile",
                            "inputTokens": 0,
                            "outputTokens": 0
                        }
                    }
                )
                if res.status_code == 200:
                    data = res.json()
                    print(f"💓 Heartbeat OK")

                    # Process incoming pipeline tasks
                    assigned_tasks = []
                    for work_item in data.get("work_items", []):
                        if work_item.get("type") == "assigned_tasks":
                            assigned_tasks.extend(work_item.get("items", []))

                    for task in assigned_tasks:
                        task_id = task.get("id")
                        if task_id and task_id not in processed_task_ids:
                            asyncio.create_task(
                                handle_incoming_task(task_id)
                            )
        except Exception:
            pass
        await asyncio.sleep(30)
