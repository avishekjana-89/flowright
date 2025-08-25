from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import os, json, time, uuid, tempfile, subprocess, shlex, asyncio, sys
from typing import List

from ..utils import RUNS_DIR, step_path, load_dataset_file, _sanitize_name, RUNNER_SCRIPT
from ..db import get_db, load_settings
from .. import db as dbmod
from ..utils import _atomic_write_bytes
from ..utils import substitute_step
from ..main import templates  # reuse templates instance

router = APIRouter()


def load_suite_items(conn, suite_id):
    cur = conn.cursor()
    cur.execute('''SELECT si.*, t.name as tc_name, t.filename as tc_filename
                   FROM suite_items si
                   LEFT JOIN testcases t ON si.tc_id = t.id
                   WHERE si.suite_id = ? ORDER BY si.position ASC''', (suite_id,))
    rows = cur.fetchall()
    items = []
    for r in rows:
        items.append({'id': r['id'], 'tc_id': r['tc_id'], 'tc_name': r['tc_name'] or '(missing)', 'position': r['position'], 'filename': r['tc_filename']})
    return items


@router.get('/suites', response_class=HTMLResponse)
async def suites(request: Request):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM suites ORDER BY created_at DESC')
    rows = cur.fetchall()
    suites = []
    for r in rows:
        suites.append({'id': r['id'], 'name': r['name'], 'description': r['description'], 'created_at': r['created_at']})
    conn.close()
    return templates.TemplateResponse('suites.html', {'request': request, 'suites': suites})


@router.get('/suites/new', response_class=HTMLResponse)
async def suites_new(request: Request):
    # Render suite creation form and provide available testcases for selection
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute('SELECT id, name FROM testcases ORDER BY name ASC')
        rows = cur.fetchall()
        testcases = [{'id': r['id'], 'name': r['name']} for r in rows]
    except Exception:
        testcases = []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return templates.TemplateResponse('suite_new.html', {'request': request, 'testcases': testcases})


@router.post('/suites/create')
async def suites_create(name: str = Form(...), description: str = Form(''), tc_ids: List[str] = Form(None)):
    uid = str(uuid.uuid4())
    conn = get_db()
    cur = conn.cursor()
    cur.execute('INSERT INTO suites(id,name,description,created_at) VALUES (?,?,?,?)', (uid, name, description, time.time()))
    ids = tc_ids or []
    for idx, tcid in enumerate(ids):
        iid = str(uuid.uuid4())
        cur.execute('INSERT INTO suite_items(id,suite_id,tc_id,position) VALUES (?,?,?,?)', (iid, uid, tcid, idx))
    conn.commit()
    conn.close()
    return RedirectResponse(f'/suites/{uid}', status_code=303)


@router.post('/suites/{suite_id}/run')
async def suite_run(request: Request, suite_id: str, tc_ids: List[str] = Form(None), concurrency: int = Form(1), browser: str = Form('chrome')):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT si.tc_id, t.filename, t.data_filename FROM suite_items si LEFT JOIN testcases t ON si.tc_id = t.id WHERE si.suite_id = ? ORDER BY si.position ASC', (suite_id,))
    rows = cur.fetchall()
    if not rows:
        conn.close()
        raise HTTPException(status_code=404, detail='Suite not found or empty')
    id_to_row = {r['tc_id']: r for r in rows}
    conn.close()

    ids = tc_ids or list(id_to_row.keys())
    jobs = []
    job_meta = []

    for tcid in ids:
        r = id_to_row.get(tcid)
        if not r or not r['filename']:
            continue
        p = step_path(r['filename'])
        if not os.path.exists(p):
            continue
        with open(p, 'r', encoding='utf-8') as f:
            try:
                orig_steps = json.load(f)
            except Exception:
                orig_steps = []

        try:
            conn2 = get_db()
            cur2 = conn2.cursor()
            cur2.execute('SELECT name FROM testcases WHERE id=?', (tcid,))
            rown2 = cur2.fetchone()
            conn2.close()
            tc_display_name = rown2['name'] if rown2 and 'name' in rown2.keys() and rown2['name'] else r['filename']
        except Exception:
            tc_display_name = r['filename']

        dataset = r['data_filename'] if 'data_filename' in r.keys() else None
        if dataset:
            try:
                rowsdata = load_dataset_file(dataset)
            except Exception as e:
                return JSONResponse({'ok': False, 'error': f'Failed to load dataset for tc {tcid}: {e}'}, status_code=400)
            dataset_ns = os.path.splitext(dataset)[0]
            for idx_row, row in enumerate(rowsdata):
                ctx = {dataset_ns: row}
                try:
                    s = load_settings()
                    sel = s.get('selected_profile')
                    if sel:
                        conn2 = get_db()
                        cur2 = conn2.cursor()
                        cur2.execute('SELECT key, value FROM profile_kv WHERE profile_id=?', (sel,))
                        rows_kv2 = cur2.fetchall()
                        conn2.close()
                        gv2 = {r['key']: r['value'] for r in rows_kv2}
                        ctx['GlobalVariables'] = gv2
                except Exception:
                    pass
                if isinstance(row, dict):
                    for k, v in row.items():
                        if k not in ctx:
                            ctx[k] = v
                import copy as _copy
                instanced = _copy.deepcopy(orig_steps)
                from ..main import resolve_selector_refs_in_steps
                from ..utils import substitute_step as _substitute_step
                resolve_selector_refs_in_steps(instanced)
                instanced = [_substitute_step(s, ctx) for s in instanced]
                row_name = None
                if isinstance(row, dict):
                    row_name = row.get('name') or row.get('testcase') or None
                job_name = str(row_name) if row_name is not None else f"{tc_display_name}-{idx_row}"
                jobs.append({'name': job_name, 'steps': instanced})
                job_meta.append({'tc_id': tcid, 'tc_filename': r['filename'], 'row_index': idx_row})
        else:
            import copy as _copy
            instanced = _copy.deepcopy(orig_steps)
            from ..main import resolve_selector_refs_in_steps
            resolve_selector_refs_in_steps(instanced)
            jobs.append({'name': str(tc_display_name), 'steps': instanced})
            job_meta.append({'tc_id': tcid, 'tc_filename': r['filename'], 'row_index': None})

    if not jobs:
        return JSONResponse({'ok': False, 'error': 'No jobs found for selected testcases'}, status_code=400)

    try:
        concurrency = int(concurrency)
    except Exception:
        concurrency = 1
    concurrency = max(1, min(20, concurrency))

    try:
        conn3 = get_db()
        cur3 = conn3.cursor()
        cur3.execute('SELECT name FROM suites WHERE id=?', (suite_id,))
        row_s = cur3.fetchone()
        conn3.close()
        suite_display_name = row_s['name'] if row_s and 'name' in row_s.keys() and row_s['name'] else suite_id
    except Exception:
        suite_display_name = suite_id

    run_id = str(uuid.uuid4())[:8]
    ts = time.strftime('%Y-%m-%d_%H-%M-%S', time.localtime())
    run_name = f"{_sanitize_name(suite_display_name)}-{ts}-{run_id}"
    run_dir = os.path.join(RUNS_DIR, run_name)
    os.makedirs(run_dir, exist_ok=True)
    logfile = os.path.join(run_dir, 'run.log')

    # Write jobs file into run_dir and spawn runner without waiting for completion.
    jobs_file = os.path.join(run_dir, 'jobs.json')
    try:
        with open(jobs_file, 'wb') as jf:
            jf.write(json.dumps(jobs).encode('utf-8'))
    except Exception:
        pass

    browser_arg = (browser or 'chrome').lower()
    cmd = f'{shlex.quote(sys.executable)} {shlex.quote(RUNNER_SCRIPT)} --stdin {shlex.quote(run_dir)} {shlex.quote(str(concurrency))} {shlex.quote(browser_arg)}'

    proc = None
    fin = None
    logf = None
    try:
        fin = open(jobs_file, 'rb')
        logf = open(logfile, 'wb')
        from ..main import runner_env_from_settings
        proc = subprocess.Popen(cmd, shell=True, stdin=fin, stdout=logf, stderr=subprocess.STDOUT, env=runner_env_from_settings())
    except Exception as e:
        try:
            _atomic_write_bytes(logfile, str(e).encode('utf-8'))
        except Exception:
            pass
        return JSONResponse({'ok': False, 'error': f'Failed to spawn suite runner: {e}'}, status_code=500)
    finally:
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

    from ..main import running as _running
    _running[suite_id] = {'pid': proc.pid, 'proc': proc, 'log': logfile, 'run_dir': run_dir, 'run_id': run_id, 'started_at': time.time()}

    runs = []
    for idx in range(len(jobs)):
        rel_run = os.path.relpath(os.path.join(run_dir, f'job_{idx}'), RUNS_DIR)
        rel_log = os.path.relpath(logfile, RUNS_DIR)
        runs.append({'row_index': idx, 'pid': proc.pid, 'returncode': None, 'run_dir': rel_run, 'run_id': f'{run_id}_{idx}', 'logfile': rel_log, 'meta': job_meta[idx]})

    return JSONResponse({'ok': True, 'runs': runs, 'run_dir': os.path.relpath(run_dir, RUNS_DIR)})


@router.get('/suites/{suite_id}', response_class=HTMLResponse)
async def suite_detail(request: Request, suite_id: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM suites WHERE id=?', (suite_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404)
    suite = {'id': row['id'], 'name': row['name'], 'description': row['description'], 'created_at': row['created_at']}
    items = load_suite_items(conn, suite_id)
    cur.execute('SELECT id,name FROM testcases')
    all_tcs = [dict(id=r['id'], name=r['name']) for r in cur.fetchall()]
    existing = set([it['tc_id'] for it in items])
    avail = [t for t in all_tcs if t['id'] not in existing]
    conn.close()
    return templates.TemplateResponse('suite_detail.html', {'request': request, 'suite': suite, 'items': items, 'available_tcs': avail})


@router.post('/suites/{suite_id}/update-meta')
async def suite_update_meta(suite_id: str, name: str = Form(...), description: str = Form('')):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('UPDATE suites SET name=?, description=? WHERE id=?', (name, description, suite_id))
    conn.commit()
    conn.close()
    return RedirectResponse(f'/suites/{suite_id}?saved=1', status_code=303)


@router.post('/suites/{suite_id}/add-tcs')
async def suite_add_tcs(suite_id: str, tc_ids: List[str] = Form(...)):
    ids = tc_ids or []
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT MAX(position) as m FROM suite_items WHERE suite_id=?', (suite_id,))
    row = cur.fetchone()
    m = row['m'] if row and row['m'] is not None else -1
    for off, tcid in enumerate(ids, start=1):
        cur.execute('SELECT 1 FROM suite_items WHERE suite_id=? AND tc_id=?', (suite_id, tcid))
        if cur.fetchone():
            continue
        iid = str(uuid.uuid4())
        pos = m + off
        cur.execute('INSERT INTO suite_items(id,suite_id,tc_id,position) VALUES (?,?,?,?)', (iid, suite_id, tcid, pos))
    conn.commit()
    conn.close()
    return RedirectResponse(f'/suites/{suite_id}', status_code=303)


@router.post('/suites/{suite_id}/remove-tc')
async def suite_remove_tc(suite_id: str, tc_id: str = Form(...)):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('DELETE FROM suite_items WHERE suite_id=? AND tc_id=?', (suite_id, tc_id))
    cur.execute('SELECT id FROM suite_items WHERE suite_id=? ORDER BY position ASC', (suite_id,))
    rows = [r['id'] for r in cur.fetchall()]
    for idx, iid in enumerate(rows):
        cur.execute('UPDATE suite_items SET position=? WHERE id=?', (idx, iid))
    conn.commit()
    conn.close()
    return RedirectResponse(f'/suites/{suite_id}', status_code=303)


@router.post('/suites/{suite_id}/move-tc')
async def suite_move_tc(suite_id: str, tc_id: str = Form(...), dir: str = Form(...)):
    if dir not in ('up', 'down'):
        raise HTTPException(status_code=400)
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT id,position FROM suite_items WHERE suite_id=? AND tc_id=?', (suite_id, tc_id))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404)
    cur.execute('SELECT id,position FROM suite_items WHERE suite_id=? ORDER BY position ASC', (suite_id,))
    rows = cur.fetchall()
    ids = [r['id'] for r in rows]
    pos_map = {r['id']: r['position'] for r in rows}
    cur_idx = None
    for idx, r in enumerate(rows):
        if r['id'] == row['id']:
            cur_idx = idx
            break
    if cur_idx is None:
        conn.close()
        raise HTTPException(status_code=404)
    if dir == 'up' and cur_idx > 0:
        other = rows[cur_idx-1]
        cur.execute('UPDATE suite_items SET position=? WHERE id=?', (other['position'], row['id']))
        cur.execute('UPDATE suite_items SET position=? WHERE id=?', (row['position'], other['id']))
    elif dir == 'down' and cur_idx < len(rows)-1:
        other = rows[cur_idx+1]
        cur.execute('UPDATE suite_items SET position=? WHERE id=?', (other['position'], row['id']))
        cur.execute('UPDATE suite_items SET position=? WHERE id=?', (row['position'], other['id']))
    conn.commit()
    conn.close()
    return RedirectResponse(f'/suites/{suite_id}', status_code=303)
