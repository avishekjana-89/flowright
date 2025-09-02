from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException
import re
from typing import List
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import os, json, subprocess, shlex, uuid, time, re, tempfile, asyncio, sys, contextlib, io, shutil, csv

from .utils import ROOT, STEPS_DIR, RUNS_DIR, DATA_DIR, RUNNER_SCRIPT, KEYWORDS_DIR, step_path, _sanitize_name, load_dataset_file, substitute_step, _atomic_write_bytes
from .utils import OBJECTS_DIR
from .db import list_object_folders
from . import db as dbmod
from .db import get_db, load_settings, init_db, tc_row_to_dict
from .db import list_folders

app = FastAPI()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), 'templates'))
app.mount('/static', StaticFiles(directory=os.path.join(os.path.dirname(__file__), 'static')), name='static')
app.mount('/runs', StaticFiles(directory=RUNS_DIR), name='runs')



@app.get('/', response_class=HTMLResponse)
async def index(request: Request):
    """Render the welcome page (root)."""
    return templates.TemplateResponse('welcome.html', {'request': request})

# register routers (imports will be performed later to avoid circular imports)

os.makedirs(DATA_DIR, exist_ok=True)


init_db()


# _atomic_write_bytes moved to webapp.utils

from runner_utils import resolve_selector_refs_in_step


def resolve_selector_refs_in_steps(steps):
    """Wrapper for backward compatibility that resolves selectorRef entries
    on each step by delegating to `runner_utils.resolve_selector_refs_in_step`.

    Preserves the original behavior for non-$ selectorRefs by setting
    `step['selectors'] = [selectorRef]`.
    """
    seen = {}
    for step in steps:
        sel_ref = step.get('selectorRef')
        if not sel_ref or not isinstance(sel_ref, str):
            continue
        if not sel_ref.startswith('$'):
            try:
                step['selectors'] = [sel_ref]
            except Exception:
                pass
            continue
        try:
            resolve_selector_refs_in_step(step, seen=seen)
        except Exception:
            # best-effort: don't let resolution errors break routes
            pass



def runner_env_from_settings(base_env=None):
    """Return an environment dict for subprocess.Popen based on DB settings and current env."""
    env = dict(os.environ) if base_env is None else dict(base_env)
    s = load_settings()
    # timeouts are stored in seconds in settings; convert to ms for runner env
    def _set_ms(k, env_name):
        if k in s and s[k] != '':
            try:
                val = int(float(s[k]))
                env[env_name] = str(val * 1000)
            except Exception:
                pass

    _set_ms('default_timeout_sec', 'PLAYWRIGHT_DEFAULT_TIMEOUT_MS')
    _set_ms('navigation_timeout_sec', 'PLAYWRIGHT_NAVIGATION_TIMEOUT_MS')
    _set_ms('assertion_timeout_sec', 'PLAYWRIGHT_ASSERTION_TIMEOUT_MS')

    # screenshot policy: 'every' or 'failure'
    if 'screenshot_policy' in s:
        env['RUNNER_SCREENSHOT_POLICY'] = s['screenshot_policy']

    # include selected profile key/value pairs as JSON so runner can use them
    try:
        sel = s.get('selected_profile')
        if sel:
            conn = get_db()
            cur = conn.cursor()
            # fetch profile name if available
            cur.execute('SELECT name FROM profiles WHERE id=?', (sel,))
            prow = cur.fetchone()
            pname = prow['name'] if prow else ''
            cur.execute('SELECT key, value FROM profile_kv WHERE profile_id=?', (sel,))
            rows = cur.fetchall()
            kv = {r['key']: r['value'] for r in rows}
            conn.close()
            env['RUNNER_PROFILE_JSON'] = json.dumps(kv)
            env['RUNNER_PROFILE_ID'] = str(sel)
            env['RUNNER_PROFILE_NAME'] = str(pname)
    except Exception:
        pass

    return env


# simple in-memory running map (process info for spawned runner processes)
running = {}


# import and include routers now that helper functions are defined (avoids circular imports)
from .routers.testcases import router as testcases_router
from .routers.suites import router as suites_router
from .routers.run import router as run_router
from .routers.reporting import router as reporting_router
app.include_router(testcases_router)
app.include_router(suites_router)
app.include_router(run_router)
app.include_router(reporting_router)


# Testcase-related routes moved to `webapp/routers/testcases.py` and are included
# via `app.include_router(testcases_router)`. The inline implementations were
# removed from `main.py` to avoid duplicate route definitions and keep this
# module focused on app wiring and shared helpers.



@app.get('/objects', response_class=HTMLResponse)
async def list_objects(request: Request):
    # Build a unified view of object folders from DB and filesystem so UI can show sync status
    entries = []
    try:
        db_rows = list_object_folders() or []
        db_map = {r['name']: r['id'] for r in db_rows}
    except Exception:
        db_map = {}
    try:
        fs_list = sorted([d for d in os.listdir(OBJECTS_DIR) if os.path.isdir(os.path.join(OBJECTS_DIR, d))])
    except Exception:
        fs_list = []

    # union of names, preserve alphabetical order
    all_names = sorted(set(list(db_map.keys()) + list(fs_list)))
    for name in all_names:
        entries.append({'name': name, 'id': db_map.get(name), 'on_fs': name in fs_list, 'in_db': name in db_map})

    return templates.TemplateResponse('objects.html', {'request': request, 'folders': entries})


@app.get('/objects/{name}', response_class=HTMLResponse)
async def object_folder_detail(request: Request, name: str):
    # safe-check: disallow path traversal
    if '..' in name or name.startswith('/'):
        raise HTTPException(status_code=400)
    obj_dir = os.path.join(OBJECTS_DIR, name)
    if not os.path.exists(obj_dir) or not os.path.isdir(obj_dir):
        raise HTTPException(status_code=404)
    locators_path = os.path.join(obj_dir, 'locators.json')
    locators = {}
    if os.path.exists(locators_path):
        try:
            with open(locators_path, 'r', encoding='utf-8') as lf:
                locators = json.load(lf) or {}
        except Exception:
            locators = {}
    # present locators as mapping key -> {selectors: [...]} in template
    return templates.TemplateResponse('object_detail.html', {'request': request, 'name': name, 'locators': locators})


@app.post('/objects/{name}/locators/save')
async def save_locator_edit(request: Request, name: str):
    # read JSON body
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({'ok': False, 'error': 'Invalid JSON'}, status_code=400)
    old = payload.get('oldKey')
    new = payload.get('newKey')
    selectors = payload.get('selectors') or []
    if not new or not isinstance(selectors, list):
        return JSONResponse({'ok': False, 'error': 'Missing newKey or selectors'}, status_code=400)
    # safety
    if '..' in name or name.startswith('/'):
        return JSONResponse({'ok': False, 'error': 'Invalid folder name'}, status_code=400)
    obj_dir = os.path.join(OBJECTS_DIR, name)
    if not os.path.exists(obj_dir) or not os.path.isdir(obj_dir):
        return JSONResponse({'ok': False, 'error': 'Folder not found'}, status_code=404)
    locators_path = os.path.join(obj_dir, 'locators.json')
    try:
        if os.path.exists(locators_path):
            with open(locators_path, 'r', encoding='utf-8') as lf:
                locators = json.load(lf) or {}
        else:
            locators = {}
    except Exception:
        locators = {}

    # perform rename/update
    try:
        if old and old in locators and old != new:
            # rename key
            locators[new] = locators.pop(old)
        # set selectors for new key
        locators.setdefault(new, {})
        locators[new]['selectors'] = selectors[:5]
        # if hash existed previously, keep it only if present in payload? We don't modify hash via UI
        # save file
        with open(locators_path, 'w', encoding='utf-8') as lf:
            json.dump(locators, lf, indent=2)
    except Exception as e:
        return JSONResponse({'ok': False, 'error': str(e)}, status_code=500)
    # Note: module-level locators cache removed; runtime resolution will read
    # latest `locators.json` on each request/run (per-call short-lived cache used).
    return JSONResponse({'ok': True, 'locators': locators})


@app.post('/objects/{name}/locators/delete')
async def delete_locator(request: Request, name: str):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({'ok': False, 'error': 'Invalid JSON'}, status_code=400)
    key = payload.get('key')
    if not key:
        return JSONResponse({'ok': False, 'error': 'Missing key'}, status_code=400)
    if '..' in name or name.startswith('/'):
        return JSONResponse({'ok': False, 'error': 'Invalid folder name'}, status_code=400)
    obj_dir = os.path.join(OBJECTS_DIR, name)
    if not os.path.exists(obj_dir) or not os.path.isdir(obj_dir):
        return JSONResponse({'ok': False, 'error': 'Folder not found'}, status_code=404)
    locators_path = os.path.join(obj_dir, 'locators.json')
    try:
        if os.path.exists(locators_path):
            with open(locators_path, 'r', encoding='utf-8') as lf:
                locators = json.load(lf) or {}
        else:
            locators = {}
    except Exception:
        locators = {}
    if key in locators:
        locators.pop(key, None)
        try:
            with open(locators_path, 'w', encoding='utf-8') as lf:
                json.dump(locators, lf, indent=2)
        except Exception as e:
            return JSONResponse({'ok': False, 'error': str(e)}, status_code=500)
    # Note: module-level locators cache removed; nothing to invalidate here.
    return JSONResponse({'ok': True, 'locators': locators})


@app.post('/objects/rename')
async def rename_object_folder(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({'ok': False, 'error': 'Invalid JSON'}, status_code=400)
    old = payload.get('oldName')
    new = payload.get('newName')
    if not old or not new:
        return JSONResponse({'ok': False, 'error': 'Missing oldName or newName'}, status_code=400)
    # sanitize new name
    safe_new = re.sub(r"[^a-zA-Z0-9_\- ]+", '', new).strip().replace(' ', '-')
    if not safe_new:
        return JSONResponse({'ok': False, 'error': 'Invalid new name after sanitization'}, status_code=400)
    # ensure old exists
    old_dir = os.path.join(OBJECTS_DIR, old)
    new_dir = os.path.join(OBJECTS_DIR, safe_new)
    if not os.path.exists(old_dir) or not os.path.isdir(old_dir):
        return JSONResponse({'ok': False, 'error': 'Old folder not found'}, status_code=404)
    if os.path.exists(new_dir):
        return JSONResponse({'ok': False, 'error': 'Target folder already exists'}, status_code=400)
    # attempt rename on FS then update DB
    try:
        os.rename(old_dir, new_dir)
    except Exception as e:
        return JSONResponse({'ok': False, 'error': f'Failed to rename folder: {e}'}, status_code=500)
    # update DB row if present
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('SELECT id FROM object_folders WHERE name=?', (old,))
        prow = cur.fetchone()
        if prow:
            try:
                cur.execute('UPDATE object_folders SET name=? WHERE id=?', (safe_new, prow['id']))
                conn.commit()
            except Exception:
                pass
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass
    # Respond to AJAX callers with JSON so frontend can parse the result.
    return JSONResponse({'ok': True})


@app.get('/keywords/new', response_class=HTMLResponse)
async def keywords_new(request: Request):
    return templates.TemplateResponse('keywords_new.html', {'request': request})


@app.get('/keywords', response_class=HTMLResponse)
async def keywords_index(request: Request):
    """Render the keywords list page by listing files in KEYWORDS_DIR."""
    try:
        files = sorted([f for f in os.listdir(KEYWORDS_DIR) if os.path.isfile(os.path.join(KEYWORDS_DIR, f))])
    except Exception:
        files = []
    return templates.TemplateResponse('keywords_list.html', {'request': request, 'files': files})


@app.post('/keywords/create')
async def keywords_create(request: Request, filename: str = Form(...), body: str = Form('')):
    # sanitize filename: only allow alnum, _, -
    safe = re.sub(r'[^a-zA-Z0-9_\-]', '', filename or '')
    if not safe:
        return JSONResponse({'ok': False, 'error': 'Invalid filename'})
    fname = safe + '.py'
    path = os.path.join(KEYWORDS_DIR, fname)
    if os.path.exists(path):
        return JSONResponse({'ok': False, 'error': 'File already exists'})

    if not body or not body.strip():
        return JSONResponse({'ok': False, 'error': 'Empty body'})

    # write the user-provided body verbatim (user must include decorator & function)
    tmp_path = path + '.tmp'
    try:
        with open(tmp_path, 'w', encoding='utf-8') as tf:
            # ensure trailing newline
            tf.write(body if body.endswith('\n') else body + '\n')
        import py_compile
        py_compile.compile(tmp_path, doraise=True)
    except Exception as e:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return JSONResponse({'ok': False, 'error': str(e)})

    # move tmp to final path
    try:
        os.replace(tmp_path, path)
    except Exception as e:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return JSONResponse({'ok': False, 'error': f'Failed to write file: {e}'})

    # import module by file path so module-level decorators run and register keywords
    try:
        import importlib.util, sys
        mod_name = os.path.splitext(fname)[0].replace('-', '_')
        spec = importlib.util.spec_from_file_location(mod_name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
    except Exception as e:
        return JSONResponse({'ok': False, 'error': f'Created but import failed: {e}'})

    # respond (JSON for XHR/accept, otherwise redirect)
    xrw = request.headers.get('x-requested-with', '').lower()
    accept = request.headers.get('accept', '').lower()
    if 'xmlhttprequest' in xrw or 'application/json' in accept:
        return JSONResponse({'ok': True, 'redirect': '/keywords'})
    return RedirectResponse('/keywords', status_code=303)



@app.get('/keywords/edit/{filename}', response_class=HTMLResponse)
async def keywords_edit(request: Request, filename: str):
    # safe path
    if '..' in filename or filename.startswith('/'):
        raise HTTPException(status_code=400)
    path = os.path.join(KEYWORDS_DIR, filename)
    if not os.path.exists(path):
        raise HTTPException(status_code=404)
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    return templates.TemplateResponse('keywords_edit.html', {'request': request, 'filename': filename, 'content': content})


@app.post('/keywords/save')
async def keywords_save(filename: str = Form(...), content: str = Form(...)):
    if '..' in filename or filename.startswith('/'):
        raise HTTPException(status_code=400)
    path = os.path.join(KEYWORDS_DIR, filename)
    tmp_path = path + '.tmp'
    try:
        with open(tmp_path, 'w', encoding='utf-8') as tf:
            tf.write(content)
        import py_compile
        py_compile.compile(tmp_path, doraise=True)
    except Exception as e:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return JSONResponse({'ok': False, 'error': str(e)})

    try:
        os.replace(tmp_path, path)
    except Exception as e:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return JSONResponse({'ok': False, 'error': f'Failed to write file: {e}'})

    # import/reload by file path to avoid module name issues
    try:
        import importlib.util, sys
        mod_name = os.path.splitext(filename)[0].replace('-', '_')
        spec = importlib.util.spec_from_file_location(mod_name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
    except Exception as e:
        return JSONResponse({'ok': False, 'error': str(e)})

    return RedirectResponse('/keywords', status_code=303)


@app.post('/keywords/delete')
async def keywords_delete(filename: str = Form(...)):
    if '..' in filename or filename.startswith('/'):
        raise HTTPException(status_code=400)
    path = os.path.join(KEYWORDS_DIR, filename)
    if os.path.exists(path):
        os.remove(path)
    return RedirectResponse('/keywords', status_code=303)


@app.get('/keywords/import/{filename}')
async def keywords_import(filename: str):
    if '..' in filename or filename.startswith('/'):
        raise HTTPException(status_code=400)
    path = os.path.join(KEYWORDS_DIR, filename)
    if not os.path.exists(path):
        raise HTTPException(status_code=404)
    mod_name = os.path.splitext(filename)[0]
    import sys, importlib
    if KEYWORDS_DIR not in sys.path:
        sys.path.insert(0, KEYWORDS_DIR)
    try:
        if mod_name in sys.modules:
            importlib.reload(sys.modules[mod_name])
        else:
            importlib.import_module(mod_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return RedirectResponse('/keywords', status_code=303)


@app.get('/keywords/list.json')
async def keywords_list_json():
    """Return a JSON list of custom keyword names by loading modules from the keywords dir
    so that decorators run and register handlers in the webapp process.
    """
    try:
        # import registry helpers lazily to avoid startup ordering issues
        from keyword_registry import load_keywords_from_dir, registry
        # ensure latest modules loaded
        load_keywords_from_dir(KEYWORDS_DIR)
        names = sorted(list(registry.list().keys()))
        return JSONResponse({'ok': True, 'custom': names})
    except Exception as e:
        return JSONResponse({'ok': False, 'error': str(e)}, status_code=500)


@app.get('/files', response_class=HTMLResponse)
async def files_index(request: Request):
    # List files uploaded via the Files UI (stored in FILES_DIR)
    from .utils import FILES_DIR
    try:
        files = sorted(os.listdir(FILES_DIR))
    except Exception:
        files = []
    return templates.TemplateResponse('files.html', {'request': request, 'files': files})


@app.post('/files/upload')
async def files_upload(file: UploadFile = File(...)):
    from .utils import FILES_DIR
    # Validate filename and save uploaded file under FILES_DIR using original filename
    if not file or not getattr(file, 'filename', None):
        raise HTTPException(status_code=400, detail='No file selected')
    # Normalize to basename to avoid directory traversal
    safe_name = os.path.basename(file.filename)
    if not safe_name or safe_name in ('.', '..') or '/' in safe_name or '\\' in safe_name:
        raise HTTPException(status_code=400, detail='Invalid filename')
    dest = os.path.join(FILES_DIR, safe_name)
    # prevent accidentally writing to an existing directory path
    if os.path.isdir(dest):
        raise HTTPException(status_code=400, detail='Destination is a directory')
    # Ensure parent dir
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    content = await file.read()
    try:
        # write bytes atomically
        _atomic_write_bytes(dest, content)
    except Exception:
        # fallback to simple write
        try:
            with open(dest, 'wb') as f:
                f.write(content)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
    return RedirectResponse('/files', status_code=303)


@app.get('/files/download/{name}')
async def files_download(name: str):
    from .utils import FILES_DIR
    path = os.path.join(FILES_DIR, name)
    if not os.path.exists(path):
        raise HTTPException(status_code=404)
    return FileResponse(path, filename=name)


@app.post('/files/delete')
async def files_delete(name: str = Form(...)):
    from .utils import FILES_DIR
    # prevent path traversal by disallowing path separators
    if '/' in name or '\\' in name or name.startswith('..'):
        raise HTTPException(status_code=400, detail='Invalid filename')
    path = os.path.join(FILES_DIR, name)
    if not os.path.exists(path):
        raise HTTPException(status_code=404)
    try:
        os.remove(path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return RedirectResponse('/files', status_code=303)


@app.post('/upload')
async def upload(file: UploadFile = File(...)):
    if not file.filename.endswith('.json'):
        raise HTTPException(status_code=400, detail='Only .json allowed')
    dest = step_path(file.filename)
    content = await file.read()
    try:
        obj = json.loads(content)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f'Invalid JSON: {e}')
    with open(dest, 'w', encoding='utf-8') as f:
        json.dump(obj, f, indent=2)
    # create default metadata if missing
    meta_path = dest + '.meta.json'
    if not os.path.exists(meta_path):
        meta = {'tags': [], 'description': ''}
        with open(meta_path, 'w', encoding='utf-8') as mf:
            json.dump(meta, mf, indent=2)
    return RedirectResponse('/', status_code=303)


@app.get('/edit/{name}', response_class=HTMLResponse)
async def edit(request: Request, name: str):
    p = step_path(name)
    if not os.path.exists(p):
        raise HTTPException(status_code=404)
    with open(p, 'r', encoding='utf-8') as f:
        data = json.load(f)
    meta = {'tags': [], 'description': ''}
    meta_path = p + '.meta.json'
    if os.path.exists(meta_path):
        try:
            with open(meta_path, 'r', encoding='utf-8') as mf:
                meta = json.load(mf)
        except Exception:
            meta = {'tags': [], 'description': ''}
    return templates.TemplateResponse('editor.html', {'request': request, 'name': name, 'steps': data, 'meta': meta})


@app.post('/save')
async def save(name: str = Form(...), content: str = Form(...)):
    p = step_path(name)
    try:
        obj = json.loads(content)
        if not isinstance(obj, list):
            raise ValueError('Top-level JSON must be a list of steps')
    except Exception as e:
        return {'ok': False, 'error': str(e)}
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(obj, f, indent=2)
    return RedirectResponse(f'/edit/{name}', status_code=303)


# /run endpoint moved to `webapp/routers/run.py` and is included via `app.include_router(run_router)`.
# The full implementation lives in that router to keep `main.py` minimal and avoid duplicate
# route declarations. See `webapp/routers/run.py` for the handler.


@app.get('/status/{name}', response_class=HTMLResponse)
async def status(request: Request, name: str):
    info = running.get(name)
    if not info:
        return templates.TemplateResponse('status.html', {'request': request, 'name': name, 'running': False})

    proc = info['proc']
    ret = proc.poll()
    if ret is None:
        rel_run = os.path.relpath(info.get('run_dir') or '', RUNS_DIR) if info.get('run_dir') else None
        rel_log = os.path.relpath(info.get('log') or '', RUNS_DIR) if info.get('log') else None
        return templates.TemplateResponse('status.html', {'request': request, 'name': name, 'running': True, 'pid': info['pid'], 'logfile': rel_log, 'run_dir': rel_run, 'run_id': info.get('run_id')})
    else:
        # finished
        with open(info['log'], 'r', encoding='utf-8', errors='ignore') as f:
            tail = ''.join(f.readlines()[-1000:])
        # collect artifacts
        run_dir = info.get('run_dir')
        artifacts = []
        if run_dir and os.path.exists(run_dir):
                for fn in sorted(os.listdir(run_dir)):
                    # ignore macOS metadata files and other hidden files
                    if fn.startswith('.'):
                        continue
                    artifacts.append(fn)
        running.pop(name, None)
    rel_run = os.path.relpath(run_dir or '', RUNS_DIR) if run_dir else None
    return templates.TemplateResponse('status.html', {'request': request, 'name': name, 'running': False, 'exit_code': ret, 'log_tail': tail, 'artifacts': artifacts, 'run_dir': rel_run})


@app.get('/profiles', response_class=HTMLResponse)
async def profiles(request: Request):
    # list profiles
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT id, name, created_at FROM profiles ORDER BY created_at DESC')
    rows = cur.fetchall()
    profiles_list = [{'id': r['id'], 'name': r['name'], 'created_at': r['created_at']} for r in rows]
    conn.close()
    return templates.TemplateResponse('profiles.html', {'request': request, 'profiles': profiles_list})


@app.post('/profiles/create')
async def profiles_create(name: str = Form(...)):
    uid = str(uuid.uuid4())
    conn = get_db()
    cur = conn.cursor()
    cur.execute('INSERT INTO profiles(id,name,created_at) VALUES (?,?,?)', (uid, name, time.time()))
    conn.commit()
    conn.close()
    return RedirectResponse(f'/profiles/{uid}', status_code=303)


@app.get('/profiles/{profile_id}', response_class=HTMLResponse)
async def profile_detail(request: Request, profile_id: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT id,name,created_at FROM profiles WHERE id=?', (profile_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404)
    profile = {'id': row['id'], 'name': row['name'], 'created_at': row['created_at']}
    cur.execute('SELECT id, key, value FROM profile_kv WHERE profile_id=? ORDER BY id ASC', (profile_id,))
    kvs = [{'id': r['id'], 'key': r['key'], 'value': r['value']} for r in cur.fetchall()]
    conn.close()
    return templates.TemplateResponse('profiles_detail.html', {'request': request, 'profile': profile, 'kvs': kvs})


@app.post('/profiles/{profile_id}/save')
async def profile_save(profile_id: str, name: str = Form(...)):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('UPDATE profiles SET name=? WHERE id=?', (name, profile_id))
    conn.commit()
    conn.close()
    return RedirectResponse(f'/profiles/{profile_id}', status_code=303)


@app.post('/profiles/{profile_id}/add-kv')
async def profile_add_kv(profile_id: str, key: str = Form(...), value: str = Form('')):
    uid = str(uuid.uuid4())
    conn = get_db()
    cur = conn.cursor()
    cur.execute('INSERT INTO profile_kv(id,profile_id,key,value) VALUES (?,?,?,?)', (uid, profile_id, key, value))
    conn.commit()
    conn.close()
    return RedirectResponse(f'/profiles/{profile_id}', status_code=303)


@app.post('/profiles/{profile_id}/delete-kv')
async def profile_delete_kv(profile_id: str, kv_id: str = Form(...)):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('DELETE FROM profile_kv WHERE id=? AND profile_id=?', (kv_id, profile_id))
    conn.commit()
    conn.close()
    return RedirectResponse(f'/profiles/{profile_id}', status_code=303)


@app.post('/profiles/{profile_id}/delete')
async def profile_delete(profile_id: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('DELETE FROM profile_kv WHERE profile_id=?', (profile_id,))
    cur.execute('DELETE FROM profiles WHERE id=?', (profile_id,))
    conn.commit()
    conn.close()
    return RedirectResponse('/profiles', status_code=303)


@app.get('/settings', response_class=HTMLResponse)
async def settings(request: Request):
    # Load current settings from DB and render form
    s = load_settings()
    # normalize into template-friendly keys
    tpl = {
        'default_timeout_sec': s.get('default_timeout_sec'),
        'navigation_timeout_sec': s.get('navigation_timeout_sec'),
        'assertion_timeout_sec': s.get('assertion_timeout_sec'),
        'screenshot_policy': s.get('screenshot_policy', 'failure'),
        'selected_profile': s.get('selected_profile', '')
    }
    # load available profiles for selection
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT id, name FROM profiles ORDER BY name ASC')
    profiles = [{'id': r['id'], 'name': r['name']} for r in cur.fetchall()]
    conn.close()
    return templates.TemplateResponse('settings.html', {'request': request, 'settings': tpl, 'profiles': profiles})


@app.post('/settings/save')
async def settings_save(request: Request, default_timeout_sec: str = Form(''), navigation_timeout_sec: str = Form(''), assertion_timeout_sec: str = Form(''), screenshot_policy: str = Form('failure'), selected_profile: str = Form('')):
    conn = get_db()
    cur = conn.cursor()
    to_set = {
        'default_timeout_sec': default_timeout_sec or '',
        'navigation_timeout_sec': navigation_timeout_sec or '',
        'assertion_timeout_sec': assertion_timeout_sec or '',
        'screenshot_policy': screenshot_policy or 'failure',
        'selected_profile': selected_profile or ''
    }
    for k, v in to_set.items():
        cur.execute('INSERT OR REPLACE INTO settings(key, value) VALUES (?,?)', (k, v))
    conn.commit()
    conn.close()
    return RedirectResponse('/settings', status_code=303)


# Note: suites-related batching and job construction moved to `webapp/routers/suites.py`.
# The implementation lives in `webapp/routers/suites.py` and is included via
# `app.include_router(suites_router)`. The previous inline implementation was
# removed to avoid duplicate route definitions and to keep `main.py` minimal.


# /suites detail route moved to `webapp/routers/suites.py` and is included via
# `app.include_router(suites_router)`; keeping it here would duplicate
# definitions and cause runtime conflicts.


# Suite-related routes moved to `webapp/routers/suites.py` and are included
# via `app.include_router(suites_router)`. Inline suite handlers were removed
# from `main.py` to prevent duplicate route declarations. See
# `webapp/routers/suites.py` for the implementations.


@app.get('/testdata', response_class=HTMLResponse)
async def testdata(request: Request):
    files = sorted([f for f in os.listdir(DATA_DIR)])
    return templates.TemplateResponse('testdata.html', {'request': request, 'files': files})


@app.get('/data-files')
async def data_files():
    files = sorted([f for f in os.listdir(DATA_DIR) if f.lower().endswith(('.json', '.csv'))])
    return JSONResponse({'files': files})


@app.post('/upload-data')
async def upload_data(file: UploadFile = File(...)):
    dest = os.path.join(DATA_DIR, file.filename)
    content = await file.read()
    with open(dest, 'wb') as f:
        f.write(content)
    return RedirectResponse('/testdata', status_code=303)


@app.get('/testdata/new', response_class=HTMLResponse)
async def testdata_new(request: Request):
    # render empty editor for new test data
    return templates.TemplateResponse('testdata_edit.html', {'request': request, 'name': '', 'content': '', 'filetype': 'json', 'is_new': True})


@app.get('/testdata/edit/{name}', response_class=HTMLResponse)
async def testdata_edit(request: Request, name: str):
    # sanitize
    name = os.path.basename(name)
    p = os.path.join(DATA_DIR, name)
    if not os.path.exists(p):
        raise HTTPException(status_code=404)
    # read file as text
    with open(p, 'r', encoding='utf-8', errors='replace') as f:
        content = f.read()
    filetype = 'json' if name.lower().endswith('.json') else 'csv' if name.lower().endswith('.csv') else 'text'
    return templates.TemplateResponse('testdata_edit.html', {'request': request, 'name': name, 'content': content, 'filetype': filetype, 'is_new': False})


@app.post('/testdata/save')
async def testdata_save(request: Request, name: str = Form(...), content: str = Form(...)):
    # sanitize filename
    name = os.path.basename(name)
    if not (name.lower().endswith('.json') or name.lower().endswith('.csv')):
        msg = 'Filename must end with .json or .csv'
        accept = request.headers.get('accept', '').lower()
        xrw = request.headers.get('x-requested-with', '').lower()
        if 'application/json' in accept or xrw == 'xmlhttprequest':
            return JSONResponse({'ok': False, 'error': msg}, status_code=400)
        return templates.TemplateResponse('testdata_edit.html', {'request': request, 'name': name, 'content': content, 'filetype': 'json' if name.lower().endswith('.json') else 'csv', 'error': msg})

    # Validate content based on extension
    try:
        if name.lower().endswith('.json'):
            obj = json.loads(content)
            if not isinstance(obj, list):
                raise ValueError('Top-level JSON must be an array of objects')
            # ensure items are objects
            for i, it in enumerate(obj):
                if not isinstance(it, dict):
                    raise ValueError(f'Element at index {i} is not an object')
        else:
            # CSV validation: ensure parsable and consistent columns
            sio = io.StringIO(content)
            reader = csv.reader(sio)
            rows = [r for r in reader]
            if len(rows) == 0:
                raise ValueError('CSV must have at least one row (header)')
            header_len = len(rows[0])
            for ri, r in enumerate(rows[1:], start=2):
                if len(r) != header_len:
                    raise ValueError(f'Row {ri} has {len(r)} columns but header has {header_len}')
    except Exception as e:
        msg = f'Validation failed: {e}'
        accept = request.headers.get('accept', '').lower()
        xrw = request.headers.get('x-requested-with', '').lower()
        if 'application/json' in accept or xrw == 'xmlhttprequest':
            return JSONResponse({'ok': False, 'error': msg}, status_code=400)
        return templates.TemplateResponse('testdata_edit.html', {'request': request, 'name': name, 'content': content, 'filetype': 'json' if name.lower().endswith('.json') else 'csv', 'error': msg})

    # write file
    dest = os.path.join(DATA_DIR, name)
    # write as text
    with open(dest, 'w', encoding='utf-8', newline='') as f:
        f.write(content)

    accept = request.headers.get('accept', '').lower()
    xrw = request.headers.get('x-requested-with', '').lower()
    if 'application/json' in accept or xrw == 'xmlhttprequest':
        return JSONResponse({'ok': True})
    return RedirectResponse('/testdata', status_code=303)


@app.post('/testdata/delete')
async def testdata_delete(name: str = Form(...)):
    name = os.path.basename(name)
    p = os.path.join(DATA_DIR, name)
    if os.path.exists(p):
        try:
            os.remove(p)
        except Exception:
            pass
    return RedirectResponse('/testdata', status_code=303)


@app.get('/testdata/download/{name}')
async def testdata_download(name: str):
    name = os.path.basename(name)
    p = os.path.join(DATA_DIR, name)
    if not os.path.exists(p):
        raise HTTPException(status_code=404)
    return FileResponse(p, filename=name)


# Reporting routes moved to webapp.routers.reporting


@app.post('/meta/save')
async def meta_save(name: str = Form(...), tags: str = Form(''), description: str = Form('')):
    p = step_path(name)
    if not os.path.exists(p):
        raise HTTPException(status_code=404)
    meta = {'tags': [t.strip() for t in tags.split(',') if t.strip()], 'description': description}
    meta_path = p + '.meta.json'
    with open(meta_path, 'w', encoding='utf-8') as mf:
        json.dump(meta, mf, indent=2)
    return RedirectResponse(f'/edit/{name}', status_code=303)


@app.post('/step/move')
async def step_move(name: str = Form(...), index: int = Form(...), dir: str = Form(...)):
    p = step_path(name)
    if not os.path.exists(p): raise HTTPException(404)
    with open(p, 'r', encoding='utf-8') as f: steps = json.load(f)
    if not (0 <= index < len(steps)): raise HTTPException(400)
    if dir == 'up' and index > 0:
        steps[index-1], steps[index] = steps[index], steps[index-1]
    elif dir == 'down' and index < len(steps)-1:
        steps[index+1], steps[index] = steps[index], steps[index+1]
    with open(p, 'w', encoding='utf-8') as f: json.dump(steps, f, indent=2)
    return RedirectResponse(f'/edit/{name}', status_code=303)


@app.post('/step/delete')
async def step_delete(name: str = Form(...), index: int = Form(...)):
    p = step_path(name)
    if not os.path.exists(p): raise HTTPException(404)
    with open(p, 'r', encoding='utf-8') as f: steps = json.load(f)
    if not (0 <= index < len(steps)): raise HTTPException(400)
    steps.pop(index)
    with open(p, 'w', encoding='utf-8') as f: json.dump(steps, f, indent=2)
    return RedirectResponse(f'/edit/{name}', status_code=303)


@app.post('/step/add')
async def step_add(name: str = Form(...), type: str = Form(...), selector: str = Form(''), value: str = Form('')):
    p = step_path(name)
    if not os.path.exists(p): raise HTTPException(404)
    with open(p, 'r', encoding='utf-8') as f: steps = json.load(f)
    step = {'type': type, 'action': 'ui', 'selector': selector or '', 'value': value or ''}
    steps.append(step)
    with open(p, 'w', encoding='utf-8') as f: json.dump(steps, f, indent=2)
    return RedirectResponse(f'/edit/{name}', status_code=303)


@app.get('/download/{name}')
async def download(name: str):
    p = step_path(name)
    if not os.path.exists(p): raise HTTPException(404)
    return FileResponse(p, filename=name)

