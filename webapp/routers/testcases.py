from fastapi import APIRouter, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
import os, json, re, time
import shutil

from ..utils import STEPS_DIR, DATA_DIR, OBJECTS_DIR, KEYWORDS_DIR, step_path, load_dataset_file
from ..db import get_db, tc_row_to_dict
from ..db import list_folders
from .. import db as dbmod

router = APIRouter()

templates = Jinja2Templates(directory=os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'templates')))


@router.get('/testcases', response_class=HTMLResponse)
async def list_testcases(request: Request):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT t.*, COALESCE(f.name, "") as folder_name FROM testcases t LEFT JOIN folders f ON t.folder_id = f.id ORDER BY folder_name ASC, t.created_at DESC')
    rows = [tc_row_to_dict(r) for r in cur.fetchall()]
    conn.close()
    grouped = {}
    for tc in rows:
        fid = tc.get('folder_id') if tc.get('folder_id') else None
        name = tc.get('folder_name') or ''
        key = (fid, name)
        grouped.setdefault(key, []).append(tc)

    def _sort_key(k):
        name = k[1] or ''
        return (name == '', name)

    groups = []
    for k in sorted(grouped.keys(), key=_sort_key):
        fid, name = k
        groups.append({'id': fid or '', 'name': name or 'Root', 'cases': grouped[k]})

    conn2 = get_db()
    folders = list_folders(conn=conn2)
    conn2.close()
    tags_set = []
    seen = set()
    for tc in rows:
        for t in tc.get('tags', []) or []:
            if not t:
                continue
            tt = t.strip()
            if not tt:
                continue
            if tt not in seen:
                seen.add(tt)
                tags_set.append(tt)
    tags_set.sort()
    return templates.TemplateResponse('testcases.html', {'request': request, 'groups': groups, 'folders': folders, 'current_folder_id': '', 'all_tags': tags_set})



@router.get('/testcases/folder/{folder_id}', response_class=HTMLResponse)
async def list_testcases_in_folder(request: Request, folder_id: str):
    conn = get_db()
    cur = conn.cursor()
    if folder_id == 'root':
        cur.execute("SELECT t.*, COALESCE(f.name, '') as folder_name FROM testcases t LEFT JOIN folders f ON t.folder_id = f.id WHERE (t.folder_id IS NULL OR t.folder_id = '') ORDER BY t.created_at DESC")
        rows = [tc_row_to_dict(r) for r in cur.fetchall()]
        folder_display = 'Root'
    else:
        cur.execute('SELECT id, name FROM folders WHERE id=?', (folder_id,))
        prow = cur.fetchone()
        if not prow:
            conn.close()
            raise HTTPException(status_code=404)
        folder_display = prow['name']
        cur.execute("SELECT t.*, COALESCE(f.name, '') as folder_name FROM testcases t LEFT JOIN folders f ON t.folder_id = f.id WHERE t.folder_id = ? ORDER BY t.created_at DESC", (folder_id,))
        rows = [tc_row_to_dict(r) for r in cur.fetchall()]
    conn.close()

    group = {'id': folder_id if folder_id != 'root' else '', 'name': folder_display, 'cases': rows}
    conn2 = get_db()
    folders = list_folders(conn=conn2)
    conn2.close()
    tags_set = []
    seen = set()
    for tc in rows:
        for t in tc.get('tags', []) or []:
            if t and t not in seen:
                seen.add(t)
                tags_set.append(t)
    tags_set.sort()
    return templates.TemplateResponse('testcases.html', {'request': request, 'groups': [group], 'folders': folders, 'current_folder_id': ('' if folder_id=='root' else folder_id), 'all_tags': tags_set})



@router.get('/testcases/new', response_class=HTMLResponse)
async def new_testcase_form(request: Request):
    conn = get_db()
    from ..db import list_folders as _list_folders
    folders = _list_folders(conn=conn)
    conn.close()
    try:
        obj_folders = dbmod.list_object_folders()
    except Exception:
        obj_folders = []
    return templates.TemplateResponse('testcase_new.html', {'request': request, 'folders': folders, 'object_folders': obj_folders})



@router.post('/testcases/create')
async def create_testcase(name: str = Form(...), description: str = Form(''), tags: str = Form(''), folder_id: str = Form(''), folder_name: str = Form(''), file: UploadFile = File(...), object_folder_id: str = Form(''), object_folder_name: str = Form('')):
    if not file.filename.endswith('.json'):
        raise HTTPException(status_code=400, detail='Only .json allowed')
    content = await file.read()
    try:
        obj = json.loads(content)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f'Invalid JSON: {e}')
    import uuid
    uid = str(uuid.uuid4())
    fn = f'{uid}.json'
    dest = step_path(fn)

    obj_folder_name = None
    safe_obj = None
    if object_folder_name and object_folder_name.strip():
        obj_folder_name = object_folder_name.strip()
        safe_obj = re.sub(r"[^a-zA-Z0-9_\- ]+", '', obj_folder_name).strip().replace(' ', '-')
    elif object_folder_id:
        try:
            of = dbmod.get_object_folder(object_folder_id)
            if of:
                obj_folder_name = of['name']
                safe_obj = obj_folder_name
            else:
                safe_obj = None
        except Exception:
            safe_obj = None

    if not safe_obj:
        raise HTTPException(status_code=400, detail='Invalid object folder')

    obj_dir = os.path.join(OBJECTS_DIR, safe_obj)
    os.makedirs(obj_dir, exist_ok=True)

    locators_path = os.path.join(obj_dir, 'locators.json')
    try:
        if os.path.exists(locators_path):
            with open(locators_path, 'r', encoding='utf-8') as lf:
                locators = json.load(lf) or {}
        else:
            locators = {}
    except Exception:
        locators = {}

    use_obj_folder_id = None
    try:
        if object_folder_name and object_folder_name.strip():
            obj_name = object_folder_name.strip()
            safe_obj_db = re.sub(r"[^a-zA-Z0-9_\- ]+", '', obj_name).strip().replace(' ', '-')
            dest_obj = os.path.join(OBJECTS_DIR, safe_obj_db)
            os.makedirs(dest_obj, exist_ok=True)
            conn2 = get_db()
            cur2 = conn2.cursor()
            cur2.execute('SELECT id FROM object_folders WHERE name=?', (safe_obj_db,))
            prow = cur2.fetchone()
            if prow:
                use_obj_folder_id = prow['id']
            else:
                import uuid as _uuid
                oid = str(_uuid.uuid4())
                cur2.execute('INSERT INTO object_folders(id,name,created_at) VALUES (?,?,?)', (oid, safe_obj_db, time.time()))
                conn2.commit()
                use_obj_folder_id = oid
            conn2.close()
        elif object_folder_id:
            use_obj_folder_id = object_folder_id
    except Exception:
        use_obj_folder_id = None

    processed_steps = []
    locators_changed = False
    for step in obj:
        new_step = {}
        for k, v in step.items():
            if k in ('id', 'hash', 'timestamp', 'selectors'):
                continue
            new_step[k] = v

        sel_ref = step.get('selectorRef')
        if sel_ref:
            new_step['selectorRef'] = sel_ref
            new_step['object-folder-id'] = object_folder_id or use_obj_folder_id or None
            hashval = step.get('hash')
            selectors = step.get('selectors') or []
            if hashval and isinstance(selectors, list) and len(selectors) > 0:
                existing = locators.get(sel_ref)
                if existing and existing.get('hash') == hashval:
                    existing_selectors = existing.get('selectors', [])
                    for s in selectors:
                        if s not in existing_selectors:
                            existing_selectors.append(s)
                    existing['selectors'] = existing_selectors[:5]
                    locators[sel_ref] = existing
                else:
                    locators[sel_ref] = {'hash': hashval, 'selectors': (selectors or [])[:5]}
                locators_changed = True

        processed_steps.append(new_step)

    if locators_changed:
        try:
            with open(locators_path, 'w', encoding='utf-8') as lf:
                json.dump(locators, lf, indent=2)
        except Exception:
            pass

    try:
        with open(dest, 'w', encoding='utf-8') as f:
            json.dump(processed_steps, f, indent=2)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'Failed to save processed testcase: {e}')

    tags_list = [t.strip() for t in tags.split(',') if t.strip()]
    seen = set()
    tags_clean_list = []
    for t in tags_list:
        if t not in seen:
            seen.add(t)
            tags_clean_list.append(t)
    tags_clean = ','.join(tags_clean_list)

    conn = get_db()
    cur = conn.cursor()
    use_folder_id = None
    if folder_name and folder_name.strip():
        import uuid
        fname = folder_name.strip()
        cur.execute('SELECT id FROM folders WHERE name=?', (fname,))
        prow = cur.fetchone()
        if prow:
            use_folder_id = prow['id']
        else:
            fid = str(uuid.uuid4())
            cur.execute('INSERT INTO folders(id,name,created_at) VALUES (?,?,?)', (fid, fname, time.time()))
            use_folder_id = fid
    elif folder_id:
        use_folder_id = folder_id

    if not use_obj_folder_id:
        conn.close()
        return JSONResponse({'ok': False, 'error': 'Object Repository folder required'}, status_code=400)

    cur.execute('INSERT INTO testcases(id,name,description,tags,filename,data_filename,folder_id,object_folder_id,created_at) VALUES (?,?,?,?,?,?,?,?,?)', (uid, name, description, tags_clean, fn, None, use_folder_id, use_obj_folder_id, time.time()))
    conn.commit()
    conn.close()
    return RedirectResponse(f'/testcases/{uid}', status_code=303)



@router.get('/testcases/folders', response_class=JSONResponse)
async def list_folders_endpoint():
    conn = get_db()
    from ..db import list_folders as _list_folders
    rows = _list_folders(conn=conn)
    conn.close()
    return JSONResponse({'ok': True, 'folders': rows})



@router.post('/testcases/folders/create')
async def create_folder(request: Request, name: str = Form(...)):
    if not name or not name.strip():
        return JSONResponse({'ok': False, 'error': 'Folder name required'}, status_code=400)
    name = name.strip()
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT id FROM folders WHERE name=?', (name,))
    prow = cur.fetchone()
    if prow:
        fid = prow['id']
        conn.close()
        accept = request.headers.get('accept', '').lower()
        xrw = request.headers.get('x-requested-with', '').lower()
        if 'application/json' in accept or 'xmlhttprequest' in xrw:
            return JSONResponse({'ok': True, 'id': fid, 'name': name})
        return RedirectResponse('/testcases', status_code=303)
    import uuid
    fid = str(uuid.uuid4())
    cur.execute('INSERT INTO folders(id,name,created_at) VALUES (?,?,?)', (fid, name, time.time()))
    conn.commit()
    conn.close()
    accept = request.headers.get('accept', '').lower()
    xrw = request.headers.get('x-requested-with', '').lower()
    if 'application/json' in accept or 'xmlhttprequest' in xrw:
        return JSONResponse({'ok': True, 'id': fid, 'name': name})
    return RedirectResponse('/testcases', status_code=303)



@router.post('/testcases/folders/{folder_id}/update')
async def update_folder(request: Request, folder_id: str, name: str = Form(...)):
    name = (name or '').strip()
    if not name:
        return JSONResponse({'ok': False, 'error': 'Folder name required'}, status_code=400)
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT id FROM folders WHERE id=?', (folder_id,))
    prow = cur.fetchone()
    if not prow:
        conn.close()
        raise HTTPException(status_code=404)
    cur.execute('SELECT id FROM folders WHERE name=? AND id<>?', (name, folder_id))
    dup = cur.fetchone()
    if dup:
        conn.close()
        return JSONResponse({'ok': False, 'error': 'Folder name already exists'}, status_code=400)
    cur.execute('UPDATE folders SET name=? WHERE id=?', (name, folder_id))
    conn.commit()
    conn.close()
    accept = request.headers.get('accept', '').lower()
    xrw = request.headers.get('x-requested-with', '').lower()
    if 'application/json' in accept or 'xmlhttprequest' in xrw:
        return JSONResponse({'ok': True, 'id': folder_id, 'name': name})
    return RedirectResponse('/testcases', status_code=303)



@router.post('/testcases/folders/{folder_id}/delete')
async def delete_folder(request: Request, folder_id: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT id, name FROM folders WHERE id=?', (folder_id,))
    prow = cur.fetchone()
    if not prow:
        conn.close()
        raise HTTPException(status_code=404)
    try:
        cur.execute('UPDATE testcases SET folder_id=NULL WHERE folder_id=?', (folder_id,))
    except Exception:
        pass
    cur.execute('DELETE FROM folders WHERE id=?', (folder_id,))
    conn.commit()
    conn.close()
    accept = request.headers.get('accept', '').lower()
    xrw = request.headers.get('x-requested-with', '').lower()
    if 'application/json' in accept or 'xmlhttprequest' in xrw:
        return JSONResponse({'ok': True})
    return RedirectResponse('/testcases', status_code=303)



@router.get('/testcases/{tc_id}', response_class=HTMLResponse)
async def testcase_detail(request: Request, tc_id: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM testcases WHERE id=?', (tc_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404)
    tc = tc_row_to_dict(row)
    p = step_path(tc['filename'])
    steps = []
    if os.path.exists(p):
        with open(p, 'r', encoding='utf-8') as f:
            steps = json.load(f)
    files = sorted([f for f in os.listdir(DATA_DIR) if f.lower().endswith(('.json', '.csv'))])
    conn2 = get_db()
    folders = list_folders(conn=conn2)
    conn2.close()
    try:
        object_folder = None
        of_id = tc.get('object_folder_id')
        if of_id:
            object_folder = dbmod.get_object_folder(of_id)
    except Exception:
        object_folder = None

    return templates.TemplateResponse('testcase_detail.html', {'request': request, 'tc': tc, 'steps': steps, 'datasets': files, 'folders': folders, 'current_folder_id': tc.get('folder_id') or '', 'object_folder': object_folder})



@router.post('/testcases/{tc_id}/save-steps')
async def testcase_save_steps(request: Request, tc_id: str, content: str = Form(...)):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM testcases WHERE id=?', (tc_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404)
    tc = tc_row_to_dict(row)
    p = step_path(tc['filename'])
    accept = request.headers.get('accept', '').lower()
    xrw = request.headers.get('x-requested-with', '').lower()
    try:
        obj = json.loads(content)
    except Exception as e:
        msg = f'Invalid JSON: {e}'
        if 'application/json' in accept or xrw == 'xmlhttprequest':
            return JSONResponse({'ok': False, 'error': msg}, status_code=400)
        return templates.TemplateResponse('testcase_detail.html', {'request': request, 'tc': tc_row_to_dict(row), 'steps': [], 'error': msg})

    def _strip_blank_strings(item):
        if isinstance(item, dict):
            out = {}
            for kk, vv in item.items():
                if vv is None:
                    continue
                if isinstance(vv, str):
                    if vv.strip() == '':
                        continue
                out[kk] = vv
            return out
        return item

    if isinstance(obj, list):
        processed = [_strip_blank_strings(s) if isinstance(s, dict) else s for s in obj]
    else:
        processed = _strip_blank_strings(obj) if isinstance(obj, dict) else obj

    with open(p, 'w', encoding='utf-8') as f:
        json.dump(processed, f, indent=2)

    if 'application/json' in accept or xrw == 'xmlhttprequest':
        return JSONResponse({'ok': True})
    return RedirectResponse(f'/testcases/{tc_id}?saved=1', status_code=303)



@router.post('/testcases/{tc_id}/delete')
async def testcase_delete(request: Request, tc_id: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT filename FROM testcases WHERE id=?', (tc_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404)
    filename = row['filename']
    try:
        cur.execute('DELETE FROM suite_items WHERE tc_id=?', (tc_id,))
    except Exception:
        pass
    cur.execute('DELETE FROM testcases WHERE id=?', (tc_id,))
    conn.commit()
    conn.close()
    p = step_path(filename)
    try:
        if os.path.exists(p): os.remove(p)
    except Exception:
        pass
    return RedirectResponse('/testcases', status_code=303)



@router.post('/testcases/{tc_id}/update-meta')
async def testcase_update_meta(tc_id: str, name: str = Form(...), description: str = Form(''), tags: str = Form(''), dataset: str = Form(''), folder_id: str = Form(''), folder_name: str = Form('')):
    tags_list = [t.strip() for t in tags.split(',') if t.strip()]
    seen = set(); tags_clean_list = []
    for t in tags_list:
        if t not in seen:
            seen.add(t); tags_clean_list.append(t)
    tags_clean = ','.join(tags_clean_list)

    conn = get_db()
    cur = conn.cursor()
    data_val = dataset or None
    use_folder_id = None
    use_folder_name = None
    if folder_name and folder_name.strip():
        fname = folder_name.strip()
        cur.execute('SELECT id FROM folders WHERE name=?', (fname,))
        prow = cur.fetchone()
        if prow:
            use_folder_id = prow['id']
        else:
            import uuid
            fid = str(uuid.uuid4())
            cur.execute('INSERT INTO folders(id,name,created_at) VALUES (?,?,?)', (fid, fname, time.time()))
            use_folder_id = fid
        use_folder_name = fname
    elif folder_id:
        use_folder_id = folder_id
    cur.execute('UPDATE testcases SET name=?, description=?, tags=?, data_filename=?, folder_id=? WHERE id=?', (name, description, tags_clean, data_val, use_folder_id, tc_id))
    conn.commit()
    conn.close()
    return RedirectResponse(f'/testcases/{tc_id}?saved=1', status_code=303)



@router.post('/testcases/{tc_id}/clone')
async def testcase_clone(request: Request, tc_id: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM testcases WHERE id=?', (tc_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404)
    tc = tc_row_to_dict(row)
    src = step_path(tc['filename'])
    import uuid
    new_id = str(uuid.uuid4())
    new_fn = f'{new_id}.json'
    dest = step_path(new_fn)
    try:
        if os.path.exists(src):
            shutil.copyfile(src, dest)
        else:
            # create an empty steps file if source missing
            with open(dest, 'w', encoding='utf-8') as f:
                json.dump([], f)
    except Exception:
        # on copy failure, attempt to write empty file
        try:
            with open(dest, 'w', encoding='utf-8') as f:
                json.dump([], f)
        except Exception:
            pass

    # make a sensible default name for the cloned testcase
    src_name = tc.get('name') or 'Testcase'
    new_name = f"{src_name} (copy)"
    tags = tc.get('tags') or []
    tags_str = ','.join([t for t in tags if t])
    try:
        cur.execute('INSERT INTO testcases(id,name,description,tags,filename,data_filename,folder_id,object_folder_id,created_at) VALUES (?,?,?,?,?,?,?,?,?)', (
            new_id, new_name, tc.get('description') or '', tags_str, new_fn, tc.get('data_filename'), tc.get('folder_id'), tc.get('object_folder_id'), time.time()
        ))
        conn.commit()
    finally:
        conn.close()

    accept = request.headers.get('accept', '').lower()
    xrw = request.headers.get('x-requested-with', '').lower()
    if 'application/json' in accept or 'xmlhttprequest' in xrw:
        return JSONResponse({'ok': True, 'id': new_id})
    return RedirectResponse(f'/testcases/{new_id}', status_code=303)

