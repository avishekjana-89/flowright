from fastapi import APIRouter, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from typing import List
import os, json, asyncio, tempfile, subprocess, shlex, uuid, time, sys

from ..utils import step_path, RUNS_DIR, _sanitize_name, load_dataset_file, substitute_step, _atomic_write_bytes, RUNNER_SCRIPT
from ..db import get_db, load_settings
from ..main import resolve_selector_refs_in_steps, runner_env_from_settings

router = APIRouter()


@router.post('/run')
async def run(request: Request, name: str = Form(...), dataset: str = Form(None), concurrency: int = Form(1), browser: str = Form('chrome')):
    p = step_path(name)
    if not os.path.exists(p):
        raise HTTPException(status_code=404)

    # If this testcase is registered in DB, consult & possibly update its data_filename
    # Try to consult DB for testcase metadata: data_filename and display name
    display_name = name
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('SELECT id, data_filename, name FROM testcases WHERE filename = ?', (name,))
        row = cur.fetchone()
        db_data_filename = None
        if row:
            db_data_filename = row['data_filename'] if 'data_filename' in row.keys() else None
            # prefer DB-provided display name when available
            if 'name' in row.keys() and row['name']:
                display_name = row['name']
            if not dataset and db_data_filename:
                dataset = db_data_filename
        conn.close()
    except Exception:
        # leave dataset and display_name as-is
        dataset = dataset

    # If no dataset provided, behave as before (single run)
    if not dataset:
        # Delegate to main's running map and process management
        from ..main import running
        info = running.get(name)
        if info:
            proc = info.get('proc')
            try:
                ret = proc.poll() if proc else None
            except Exception:
                running.pop(name, None)
                info = None
                ret = 1
            if info and ret is None:
                return {'ok': False, 'error': 'Already running'}
            elif info and ret is not None:
                running.pop(name, None)

        run_id = str(uuid.uuid4())[:8]
        ts = time.strftime('%Y-%m-%d_%H-%M-%S', time.localtime())
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute('SELECT name FROM testcases WHERE filename=?', (name,))
            rown = cur.fetchone()
            conn.close()
            display_name = rown['name'] if rown and 'name' in rown.keys() and rown['name'] else name
        except Exception:
            display_name = name
        run_name = f"{_sanitize_name(display_name)}-{ts}-{run_id}"
        run_dir = os.path.join(RUNS_DIR, run_name)
        os.makedirs(run_dir, exist_ok=True)
        logfile = os.path.join(run_dir, 'run.log')
        browser_arg = (browser or 'chrome').lower()

        try:
            with open(p, 'r', encoding='utf-8') as sf:
                orig_steps = json.load(sf)
        except Exception:
            orig_steps = None

        run_steps_path = p
        if isinstance(orig_steps, list):
            try:
                import copy as _copy
                tmp_steps = _copy.deepcopy(orig_steps)
                resolve_selector_refs_in_steps(tmp_steps)
                tmpf = tempfile.NamedTemporaryFile(delete=False, suffix='.json')
                try:
                    tmpf.write(json.dumps(tmp_steps).encode('utf-8'))
                    tmpf.flush()
                    tmpf.close()
                    run_steps_path = tmpf.name
                except Exception:
                    try:
                        tmpf.close()
                    except Exception:
                        pass
            except Exception:
                run_steps_path = p

            cmd = f'{shlex.quote(sys.executable)} {shlex.quote(RUNNER_SCRIPT)} {shlex.quote(run_steps_path)} {shlex.quote(run_dir)} {shlex.quote(browser_arg)}'
            with open(logfile, 'wb') as logf:
                env_with_tc = runner_env_from_settings({'RUNNER_TESTCASE_NAME': display_name})
                proc = subprocess.Popen(cmd, shell=True, stdout=logf, stderr=subprocess.STDOUT, env=env_with_tc)
            from ..main import running as _running
            _running[name] = {'pid': proc.pid, 'proc': proc, 'log': logfile, 'run_dir': run_dir, 'run_id': run_id, 'started_at': time.time()}
            accept = request.headers.get('accept', '').lower()
            xrw = request.headers.get('x-requested-with', '').lower()
            if 'application/json' in accept or xrw == 'xmlhttprequest':
                rel_run = os.path.relpath(run_dir, RUNS_DIR)
                return JSONResponse({'ok': True, 'runs': [{'run_id': run_id, 'run_dir': rel_run, 'status_url': f'/status/{name}'}]})
            return RedirectResponse(f'/status/{name}', status_code=303)

    # dataset provided -> spawn one run per row (batching via stdin is handled elsewhere)
    try:
        rows = load_dataset_file(dataset)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f'Failed to load dataset: {e}')

    # Build instanced jobs and stream to runner via temporary file and subprocess
    jobs = []
    with open(p, 'r', encoding='utf-8') as f:
        orig_steps = json.load(f)
    dataset_ns = os.path.splitext(dataset)[0]
    for idx, row in enumerate(rows):
        ctx = {dataset_ns: row}
        try:
            s = load_settings()
            sel = s.get('selected_profile')
            if sel:
                conn = get_db()
                cur = conn.cursor()
                cur.execute('SELECT key, value FROM profile_kv WHERE profile_id=?', (sel,))
                rows_kv = cur.fetchall()
                conn.close()
                gv = {r['key']: r['value'] for r in rows_kv}
                ctx['GlobalVariables'] = gv
        except Exception:
            pass
        if isinstance(row, dict):
            for k, v in row.items():
                if k not in ctx:
                    ctx[k] = v
        import copy as _copy
        instanced = _copy.deepcopy(orig_steps)
        resolve_selector_refs_in_steps(instanced)
        instanced = [substitute_step(s, ctx) for s in instanced]
        row_name = row.get('name') or row.get('testcase') if isinstance(row, dict) else None
        job_name = str(row_name) if row_name is not None else f"{display_name}-{idx}"
        jobs.append({'name': job_name, 'steps': instanced})

    run_id = str(uuid.uuid4())[:8]
    ts = time.strftime('%Y-%m-%d_%H-%M-%S', time.localtime())
    run_name = f"{_sanitize_name(display_name)}-{ts}-{run_id}"
    run_dir = os.path.join(RUNS_DIR, run_name)
    os.makedirs(run_dir, exist_ok=True)
    logfile = os.path.join(run_dir, 'run.log')

    # Write jobs to a file inside the run directory and spawn the runner without waiting.
    jobs_file = os.path.join(run_dir, 'jobs.json')
    try:
        with open(jobs_file, 'wb') as jf:
            jf.write(json.dumps(jobs).encode('utf-8'))
    except Exception:
        # best-effort: if writing jobs file fails, continue and let runner fail/record error
        pass

    browser_arg = (browser or 'chrome').lower()
    cmd = f'{shlex.quote(sys.executable)} {shlex.quote(RUNNER_SCRIPT)} --stdin {shlex.quote(run_dir)} {shlex.quote(str(concurrency))} {shlex.quote(browser_arg)}'

    proc = None
    fin = None
    logf = None
    try:
        fin = open(jobs_file, 'rb')
        logf = open(logfile, 'wb')
        proc = subprocess.Popen(cmd, shell=True, stdin=fin, stdout=logf, stderr=subprocess.STDOUT, env=runner_env_from_settings({'RUNNER_TESTCASE_NAME': display_name}))
    except Exception as e:
        try:
            with open(logfile, 'ab') as lf:
                lf.write(str(e).encode('utf-8'))
        except Exception:
            pass
        return JSONResponse({'ok': False, 'error': f'Failed to spawn batch runner: {e}'}, status_code=500)
    finally:
        # parent can close its file handles; child keeps its own copies
        try:
            if fin:
                fin.close()
        except Exception:
            pass
        try:
            if logf:
                logf.close()
        except Exception:
            pass

    # register the running process so status endpoint / UI can track it
    from ..main import running as _running
    _running[name] = {'pid': proc.pid, 'proc': proc, 'log': logfile, 'run_dir': run_dir, 'run_id': run_id, 'started_at': time.time()}

    runs = []
    for idx in range(len(jobs)):
        rel_run = os.path.relpath(os.path.join(run_dir, f'job_{idx}'), RUNS_DIR)
        rel_log = os.path.relpath(logfile, RUNS_DIR)
        runs.append({'row_index': idx, 'pid': proc.pid, 'returncode': None, 'run_dir': rel_run, 'run_id': f'{run_id}_{idx}', 'logfile': rel_log})

    return JSONResponse({'ok': True, 'runs': runs})
