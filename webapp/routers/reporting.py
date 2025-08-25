from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
import os, shutil

from ..utils import RUNS_DIR
from ..main import templates

router = APIRouter()


@router.get('/reporting', response_class=HTMLResponse)
async def reporting(request: Request):
    # list available runs, newest first (by mtime)
    entries = []
    for d in os.listdir(RUNS_DIR):
        # ignore hidden files created by macOS (e.g. .DS_Store)
        if d.startswith('.'):
            continue
        path = os.path.join(RUNS_DIR, d)
        try:
            mtime = os.path.getmtime(path)
        except Exception:
            mtime = 0
        entries.append((d, mtime))
    # sort by mtime desc (latest first)
    entries.sort(key=lambda x: x[1], reverse=True)
    runs = [e[0] for e in entries]
    return templates.TemplateResponse('reporting.html', {'request': request, 'runs': runs})


@router.post('/reporting/delete')
async def reporting_delete(request: Request):
    # delete a run directory on the server; expects JSON body: {"run": "<name>"}
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail='Invalid JSON')
    run = data.get('run')
    if not run:
        raise HTTPException(status_code=400, detail='Missing run')
    # sanitize and ensure we only delete things under RUNS_DIR
    run_name = os.path.basename(str(run))
    if run_name.startswith('.') or run_name in ('', '..'):
        raise HTTPException(status_code=400, detail='Invalid run name')
    target = os.path.join(RUNS_DIR, run_name)
    if not os.path.exists(target):
        raise HTTPException(status_code=404, detail='Run not found')
    # remove file or directory
    try:
        if os.path.isdir(target):
            shutil.rmtree(target)
        else:
            os.remove(target)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'Failed to delete: {e}')
    return JSONResponse({'ok': True})
