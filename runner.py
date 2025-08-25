import asyncio
import json
import os
import re
try:
    from dotenv import load_dotenv
except Exception:
    # dotenv optional: no-op loader when unavailable
    def load_dotenv(*args, **kwargs):
        return None
from playwright.async_api import async_playwright, expect
import time
import uuid
import copy
import traceback
import tempfile
try:
    import fcntl
except Exception:
    fcntl = None


# Load environment variables
load_dotenv()
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
# Timeouts (milliseconds) configurable via .env
def _env_ms(name, default):
    v = os.getenv(name)
    try:
        return int(v) if v is not None and v != "" else int(default)
    except Exception:
        return int(default)

PLAYWRIGHT_DEFAULT_TIMEOUT_MS = _env_ms("PLAYWRIGHT_DEFAULT_TIMEOUT_MS", 5000)
PLAYWRIGHT_NAVIGATION_TIMEOUT_MS = _env_ms("PLAYWRIGHT_NAVIGATION_TIMEOUT_MS", 30000)
PLAYWRIGHT_ASSERTION_TIMEOUT_MS = _env_ms("PLAYWRIGHT_ASSERTION_TIMEOUT_MS", 5000)

# Flag set to True when any locator is healed during a run
HEALED_ANY = False

# Load profile key/values from env (if provided by webapp)
from runner_utils import PROFILE_KV

# variables proxy is implemented in its own module to keep runner small.
from variables import variables, set_variables_dict, reset_variables_token

# Import small utilities from runner_utils to keep this file focused on flow control
from runner_utils import log, substitute_globals_in_step, substitute_locals_in_step, get_target_context, resolve_selector_refs_in_step

from keyword_registry import registry, keyword, import_by_path, run_keyword_async
from keyword_registry import load_keywords_from_dir
# access object repo utilities to persist healed locators and invalidate cache
try:
    from webapp.utils import FILES_DIR
    from webapp.utils import OBJECTS_DIR
    from webapp.db import get_object_folder
except Exception:
    # running outside webapp context: best-effort no-op fallbacks
    FILES_DIR = None
    OBJECTS_DIR = None
    def get_object_folder(_):
        return None
    

# Attempt to auto-load any keyword modules from the project's keywords/ directory.
# This lets user-created files (including names with hyphens) register themselves
# with the central registry when the runner process starts.
try:
    WORK_DIR = os.path.abspath(os.path.dirname(__file__))
    KEYWORDS_DIR = os.path.join(WORK_DIR, 'keywords')
    res = load_keywords_from_dir(KEYWORDS_DIR)
    # suppress verbose keyword load output
except Exception as e:
    log(f"Error auto-loading keywords: {e}")



# Some small utilities are provided by runner_utils to keep this file focused
# on orchestration and Playwright calls.


async def try_locator(context, step, action):
    selectors = step.get("selectors", [])
    if not selectors or not isinstance(selectors, list):
        raise ValueError("Step requires a non-empty 'selectors' array")
    # treat the first entry in `selectors` as the primary selector
    primary = selectors[0] if selectors else None
    for sel in selectors:
        try:
            # run the provided action and capture its result. Many Playwright
            # actions return None on success (e.g., click), but some (like
            # text_content or get_attribute) return useful values. We want to
            # surface those values to callers while still treating None as
            # a successful truthy outcome.
            result = await action(sel, context)
            # If fallback worked and it wasn't the primary, promote it to primary
            if sel != primary:
                # mark healed and update selectors array by moving the working
                # selector to the front while preserving order and uniqueness.
                try:
                    log(f"✅ Promoting healed selector to primary: {sel}")
                    # capture the selector's position in the original runtime
                    # selectors list BEFORE we mutate step['selectors'] so we
                    # can map back to the corresponding template entry by
                    # position.
                    try:
                        promoted_index = selectors.index(sel)
                    except Exception:
                        promoted_index = None

                    new_selectors = [sel] + [s for s in selectors if s != sel]
                    step["selectors"] = new_selectors
                    global HEALED_ANY
                    HEALED_ANY = True
                    # Persist healed locator back to object's locators.json when possible
                    try:
                        # determine selectorRef and object folder id from step if present
                        sel_ref = step.get('selectorRef')
                        obj_id = step.get('object-folder-id') or step.get('object_folder_id')
                        if sel_ref and obj_id and OBJECTS_DIR:
                            try:
                                of = get_object_folder(obj_id)
                            except Exception:
                                of = None
                            if of and of.get('name'):
                                obj_dir = os.path.join(OBJECTS_DIR, of['name'])
                                loc_path = os.path.join(obj_dir, 'locators.json')
                                # read-modify-write with advisory lock + atomic replace to
                                # avoid corruption when multiple concurrent runs update
                                # the same locators.json
                                locs = {}
                                lock_fp = None
                                # create a per-selectorRef lock filename (sanitize sel_ref)
                                try:
                                    safe_ref = re.sub(r'[^A-Za-z0-9_.-]', '_', str(sel_ref))
                                except Exception:
                                    safe_ref = str(uuid.uuid4())
                                lock_path = loc_path + f'.{safe_ref}.lock'
                                try:
                                    # ensure directory exists
                                    os.makedirs(os.path.dirname(loc_path), exist_ok=True)

                                    # Acquire an exclusive advisory lock on the per-selectorRef lock file
                                    if fcntl:
                                        try:
                                            lock_fp = open(lock_path, 'w+')
                                            try:
                                                fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX)
                                            except Exception:
                                                # if locking fails, continue without it (best-effort)
                                                pass
                                        except Exception:
                                            try:
                                                if lock_fp:
                                                    lock_fp.close()
                                            except Exception:
                                                pass
                                            lock_fp = None

                                    # Read existing locators
                                    if os.path.exists(loc_path):
                                        try:
                                            with open(loc_path, 'r', encoding='utf-8') as lf:
                                                locs = json.load(lf) or {}
                                        except Exception:
                                            locs = {}
                                    else:
                                        locs = {}

                                    # build promoted list by index (template-aware)
                                    entry = locs.get(sel_ref) or {}
                                    existing_selectors = entry.get('selectors') if isinstance(entry.get('selectors'), list) else []
                                    step_selectors = step.get('selectors') or []

                                    # Build the promoted ordering using the actual working selector
                                    # Behavior: move the working selector `sel` to the first position
                                    # and keep the remaining existing selectors in their original order.
                                    # If `sel` is not present in the existing template, insert it
                                    # at the front while preserving the existing entries.
                                    try:
                                        if isinstance(existing_selectors, list) and existing_selectors:
                                            if sel in existing_selectors:
                                                promoted = [sel] + [s for s in existing_selectors if s != sel]
                                            else:
                                                # insert the healed selector at front, avoid duplicates
                                                promoted = [sel] + [s for s in existing_selectors if s != sel]
                                        else:
                                            promoted = [sel]
                                    except Exception:
                                        promoted = [sel]

                                    # Limit stored selectors to a reasonable number
                                    promoted = promoted[:5]

                                    if promoted != existing_selectors:
                                        entry['selectors'] = promoted
                                        if step.get('hash'):
                                            entry['hash'] = step.get('hash')

                                        # To avoid lost updates when different selectorRefs are
                                        # being healed concurrently, acquire a short-lived
                                        # global file lock before the final re-read+write.
                                        global_lock_fp = None
                                        global_lock_path = loc_path + '.global.lock'
                                        try:
                                            # Try to acquire the global lock (best-effort if fcntl unavailable)
                                            if fcntl:
                                                try:
                                                    global_lock_fp = open(global_lock_path, 'w+')
                                                    try:
                                                        fcntl.flock(global_lock_fp.fileno(), fcntl.LOCK_EX)
                                                    except Exception:
                                                        pass
                                                except Exception:
                                                    try:
                                                        if global_lock_fp:
                                                            global_lock_fp.close()
                                                    except Exception:
                                                        pass
                                                    global_lock_fp = None

                                            # Re-read the latest file contents while holding the global lock
                                            latest = {}
                                            if os.path.exists(loc_path):
                                                try:
                                                    with open(loc_path, 'r', encoding='utf-8') as lf:
                                                        latest = json.load(lf) or {}
                                                except Exception:
                                                    latest = {}

                                            # update only this selectorRef entry; avoid writing if on-disk is already up-to-date
                                            on_disk_entry = latest.get(sel_ref) or {}
                                            on_disk_selectors = on_disk_entry.get('selectors') if isinstance(on_disk_entry.get('selectors'), list) else []
                                            on_disk_hash = on_disk_entry.get('hash')

                                            # If the on-disk selectors already match the promoted ordering
                                            # (or hash matches), skip the write to avoid duplicate updates
                                            if on_disk_selectors == promoted or (step.get('hash') and on_disk_hash == step.get('hash')):
                                                try:
                                                    try:
                                                        rel_path = os.path.relpath(loc_path)
                                                    except Exception:
                                                        rel_path = loc_path
                                                    log(f"ℹ️ Skipping write: on-disk selectors already up-to-date for {sel_ref} ({rel_path})")
                                                except Exception:
                                                    pass
                                            else:
                                                latest[sel_ref] = entry
                                                try:
                                                    dirn = os.path.dirname(loc_path) or '.'
                                                    fd, tmp = tempfile.mkstemp(dir=dirn)
                                                    with os.fdopen(fd, 'w', encoding='utf-8') as tf:
                                                        json.dump(latest, tf, indent=2)
                                                        tf.flush()
                                                        try:
                                                            os.fsync(tf.fileno())
                                                        except Exception:
                                                            pass
                                                    os.replace(tmp, loc_path)
                                                    try:
                                                        rel_path = os.path.relpath(loc_path)
                                                    except Exception:
                                                        rel_path = loc_path
                                                    log(f"💾 Healed selector persisted to {rel_path} for {sel_ref}")
                                                except Exception:
                                                    pass
                                        finally:
                                            try:
                                                if global_lock_fp and not global_lock_fp.closed:
                                                    try:
                                                        if fcntl:
                                                            try:
                                                                fcntl.flock(global_lock_fp.fileno(), fcntl.LOCK_UN)
                                                            except Exception:
                                                                pass
                                                    except Exception:
                                                        pass
                                                    try:
                                                        global_lock_fp.close()
                                                    except Exception:
                                                        pass
                                            except Exception:
                                                pass
                                finally:
                                    try:
                                        if lock_fp and not lock_fp.closed:
                                            try:
                                                if fcntl:
                                                    try:
                                                        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
                                                    except Exception:
                                                        pass
                                            except Exception:
                                                pass
                                            try:
                                                lock_fp.close()
                                            except Exception:
                                                pass
                                    except Exception:
                                        pass
                    except Exception:
                        pass
                except Exception:
                    pass
            # If the action returned None (common for mutating actions),
            # return True to indicate success. Otherwise return the actual
            # result (e.g., text or attribute value).
            return True if result is None else result
        except Exception as e:
            log(f"⚠️ Failed with {sel}: {e}")
    return False


async def run_step(state, step):
    """Run a single step. `state` is a mutable dict with at least the key 'page'.
    This allows steps (like switchToWindow) to update the current page so the
    caller can pick up the change for subsequent steps.
    """
    page = state.get('page')
    context = await get_target_context(page, step)
    step_type = step.get("action")
    value = step.get("value")

    if step_type == "goto":
        full_url = step.get("url").strip()
        if not full_url:
            raise ValueError("goto step requires a non-empty 'url' field")
        log(f"🌐 Navigating to {full_url}")
        await page.goto(full_url)
        return True

    elif step_type == "click":
        return await try_locator(context, step, lambda sel, ctx: ctx.locator(sel).click(force=True))
    
    elif step_type == "doubleClick":
        return await try_locator(context, step, lambda sel, ctx: ctx.locator(sel).dblclick())
    
    elif step_type == "hover":
        return await try_locator(context, step, lambda sel, ctx: ctx.locator(sel).hover())

    elif step_type == "fill":
        return await try_locator(context, step, lambda sel, ctx: ctx.locator(sel).fill(value))
    
    elif step_type == "press":
        return await try_locator(context, step, lambda sel, ctx: ctx.locator(sel).press(value))
    
    elif step_type == "selectDate":
        async def selectDate(sel, ctx):
            locator = ctx.locator(sel)
            await locator.fill(value)
            await locator.press("Enter")

        return await try_locator(context, step, selectDate)


    elif step_type == "selectDropdownByValue":
        return await try_locator(context, step, lambda sel, ctx: ctx.select_option(sel, value=value))
    
    elif step_type == "upload":
        files = value.split(",")
        # normalize single vs multiple file inputs; value may be a single filename or comma-separated list
        # if value contains no commas, files will still be a list of one element
        if not isinstance(files, list) or len(files) == 1:
            file_val = value.strip() if '/' in value.strip() else f"{FILES_DIR}/{value.strip()}"
            log(f"📁 Uploading file: {os.path.relpath(file_val)}")
            return await try_locator(context, step, lambda sel, ctx: ctx.locator(sel).set_input_files(file_val))
        else:
            processed = []
            for _f in files:
                f = _f.strip()
                if not f:
                    continue
                if '/' in f:
                    processed.append(f)
                else:
                    processed.append(f"{FILES_DIR}/{f}")

            log(f"📁 Uploading file(s): {', '.join([os.path.relpath(p) for p in processed])}")
            return await try_locator(context, step, lambda sel, ctx, _p=processed: ctx.locator(sel).set_input_files(_p))
    

    elif step_type == "verifyElementText":
        async def verify(sel, ctx):
            locator = ctx.locator(sel)
            # run assertion
            await expect(locator).to_have_text(value)

        return await try_locator(context, step, verify)
    
    elif step_type == "verifyElementContainsText":
        async def verify(sel, ctx):
            locator = ctx.locator(sel)
            # run contains assertion
            await expect(locator).to_have_text(re.compile(r".*" + value +".*", re.IGNORECASE))

        return await try_locator(context, step, verify)
    
    elif step_type == "verifyElementNotContainsText":
        async def verify(sel, ctx):
            locator = ctx.locator(sel)
            # run negative contains assertion
            await expect(locator).not_to_contain_text(re.compile(r".*" + value +".*", re.IGNORECASE))

        return await try_locator(context, step, verify)
    
    elif step_type == "verifyElementValue":
        async def verify(sel, ctx):
            locator = ctx.locator(sel)
            # run value assertion
            await expect(locator).to_have_value(value)
            
        return await try_locator(context, step, verify)
    
    elif step_type == "verifyElementVisible":
        async def verify_displayed(sel, ctx):
            locator = ctx.locator(sel)
            await expect(locator).to_be_visible()
            return True
        
        return await try_locator(context, step, verify_displayed)
        
    elif step_type == "verifyElementHidden":
        async def verify_hidden(sel, ctx):
            locator = ctx.locator(sel)
            await expect(locator).to_be_hidden()
            return True

        return await try_locator(context, step, verify_hidden)
    
    elif step_type == "verifyElementDisabled":
        async def verify_disabled(sel, ctx):
            locator = ctx.locator(sel)
            await expect(locator).to_be_disabled()
            return True

        return await try_locator(context, step, verify_disabled)
    
    elif step_type == "verifyElementChecked":
        async def verify_checked(sel, ctx):
            locator = ctx.locator(sel)
            await expect(locator).to_be_checked()
            return True

        return await try_locator(context, step, verify_checked)
    
    elif step_type == "verifyElementEnabled":
        async def verify_enabled(sel, ctx):
            locator = ctx.locator(sel)
            await expect(locator).to_be_enabled()
            return True

        return await try_locator(context, step, verify_enabled)
    
    elif step_type == "verifyElementAttribute":
        async def verify_attribute(sel, ctx):
            locator = ctx.locator(sel)
            data = json.loads(value) if isinstance(value, str) else value
            if not isinstance(data, dict): 
                raise ValueError("verifyElementAttribute requires a dictionary value with 'name' and 'value' keys")
            
            for key in data.keys():
                attr_name = key
                attr_value = data[key]
                # attribute verification
                if attr_value is not None:
                    await expect(locator).to_have_attribute(attr_name, attr_value)
            return True

        return await try_locator(context, step, verify_attribute)
    
    elif step_type == "verifyElementCount":
        async def verifyCount(sel, ctx):
            locator = ctx.locator(sel)
            if not value.isdigit():
                raise ValueError("verifyElementCount requires a numeric value")
            await expect(locator).to_have_count(int(value))
            return True

        return await try_locator(context, step, verifyCount)
    
    elif step_type == "verifyPageTitle":
        await expect(context).to_have_title(value)
        return True

    elif step_type == "check":
        return await try_locator(context, step, lambda sel, ctx: ctx.locator(sel).check(force=True))

    elif step_type == "uncheck":
        return await try_locator(context, step, lambda sel, ctx: ctx.locator(sel).uncheck(force=True))
    
    elif step_type == "scroll":
        return True
    
    elif step_type == "switchToWindow":
        if not value:
            raise ValueError("switchToWindow step requires a non-empty 'value' field with the window name or index")
        
        # Determine the Playwright BrowserContext from the current context/page/frame
        try:
            # If we're inside a Frame, it has a .page property
            if hasattr(context, 'page'):
                current_page = context.page
            else:
                current_page = page

            browser_ctx = getattr(current_page, 'context', None)
            if browser_ctx is None:
                raise ValueError("Unable to determine browser context for switchToWindow")

            pages = list(getattr(browser_ctx, 'pages', []) or [])

            # Persist the new page into the provided state so the caller's loop
            # will use the switched-to page for subsequent steps.
            if isinstance(value, str) and value.isdigit():
                idx = int(value)
                if idx < 0 or idx >= len(pages):
                    raise ValueError(f"switchToWindow index {idx} out of range (0-{len(pages)-1})")
                state['page'] = pages[idx]
                page = state['page']
            elif isinstance(value, str):
                v = value.lower()
                if v in ("main", "default", "first"):
                    # switch to the main/default page
                    if pages:
                        state['page'] = pages[0]
                        page = state['page']
                    else:
                        raise ValueError("No pages available to switch to")
                elif v == "last":
                    # Prefer an existing last page; if only one page exists, wait briefly for a new one
                    if len(pages) <= 1:
                        log("🔎 Waiting for a new page to open for 'last' switch")
                        try:
                            new_page = await browser_ctx.wait_for_event('page', timeout=PLAYWRIGHT_DEFAULT_TIMEOUT_MS)
                            # give Playwright a moment to populate pages list
                            await asyncio.sleep(0.05)
                            pages = list(getattr(browser_ctx, 'pages', []) or [])
                            # prefer the page object returned by the event if available
                            picked = new_page or (pages[-1] if pages else None)
                        except Exception:
                            # fallback: pick last if any
                            pages = list(getattr(browser_ctx, 'pages', []) or [])
                            picked = pages[-1] if pages else None
                    else:
                        picked = pages[-1]

                    if picked:
                        state['page'] = picked
                        page = state['page']
                    else:
                        raise ValueError("No pages available to switch to")
        except Exception as e:
            log(f"⚠️ switchToWindow failed: {e}")
            raise

        return True
    
    elif step_type == "getText":
        text = await try_locator(context, step, lambda sel, ctx: ctx.locator(sel).text_content())

        # try_locator now returns the raw result (or True/False for success/failure)
        # If locator lookup failed, text will be False. If it succeeded but
        # returned no content, Playwright returns None. Normalize to string.
        if text is False:
            return False
        text_str = '' if text is None else str(text)

        if "store_as" in step:
            variables[step["store_as"]] = text_str.strip()
            try:
                log(f"💾 Stored local variable: {step['store_as']} = {variables.get(step['store_as'])}")
            except Exception:
                pass

        return text_str
    
    elif step_type == "getAttribute":
        if "attributeName" not in step:
            raise ValueError("getAttribute step requires an 'attributeName' field")
        
        attribute_name = step["attributeName"]
        if not attribute_name:
            raise ValueError("getAttribute step requires a non-empty 'attributeName' field")
        
        attribute = await try_locator(context, step, lambda sel, ctx: ctx.locator(sel).get_attribute(attribute_name))

        if attribute is False:
            return False
        attr_str = '' if attribute is None else str(attribute)

        if "store_as" in step:
            variables[step["store_as"]] = attr_str.strip()
            try:
                log(f"💾 Stored local variable: {step['store_as']} = {variables.get(step['store_as'])}")
            except Exception:
                pass

        return attr_str
    
    elif step_type == "dragAndDrop":
        async def drag_and_drop(sel, ctx):
            locator = ctx.locator(sel)
            target_selector = step.get("targetSelector")
            if not target_selector:
                raise ValueError("dragAndDrop step requires a 'targetSelector' field")

            target_locator = ctx.locator(target_selector[0]) if isinstance(target_selector, list) and target_selector else ctx.locator(target_selector)
            await locator.drag_to(target_locator)

        return await try_locator(context, step, drag_and_drop)
    
    else:
            # unknown step type -> attempt to resolve as custom keyword

        # Attempt to resolve custom/user-defined keywords.
        # Resolution order:
        # 1) exact registry lookup by step_type
        # 2) treat step_type as import path 'module:callable' or 'module.attr'
        try:
            target = None
            try:
                target = registry.get(step_type)
            except Exception:
                target = None

            if target is None:
                # try to import by path
                try:
                    target = import_by_path(step_type)
                except Exception:
                    target = None

            # If still not found, attempt to (re)load keyword files from the keywords/ dir
            if target is None:
                try:
                    # this will execute module-level code in keyword files which should
                    # call the @keyword decorator and register handlers in the registry
                    load_keywords_from_dir(KEYWORDS_DIR)
                    try:
                        target = registry.get(step_type)
                    except Exception:
                        target = None
                except Exception:
                    target = None

            if target is None:
                log(f"No custom keyword found for: {step_type}")
                return False

            # invoke the found keyword callable
            try:
                res = await run_keyword_async(target, context, step)
                log(f"🔑 Custom keyword {step_type} returned: {res}")
                # consider truthy return as success; allow keywords to return True/False or richer dicts
                if isinstance(res, dict):
                    for k in res:
                        if k not in ('success', 'ok', 'error', 'message'):
                            variables[k] = res[k]

                    return bool(res.get('success', True))
                return bool(res)
            except asyncio.TimeoutError:
                log(f"⚠️ Custom keyword timed out: {step_type}")
                return False
            except Exception as e:
                log(f"⚠️ Custom keyword raised: {e}")
                return False

        except Exception as e:
            log(f"⚠️ Error resolving custom keyword {step_type}: {e}")
            return False


async def run_test(filename_or_steps, run_dir=None, concurrency=None, browser='chrome'):
    # filename_or_steps may be a path to a JSON file, an in-memory list of steps,
    # or a batch (list of step-lists) to run in one invocation.
    # If filename_or_steps is a list of lists, treat it as a suite: launch
    # Playwright/browser once and run each job in its own context.
    global HEALED_ANY
    HEALED_ANY = False

    # Detect batch: can be a list of step-lists (legacy) or a list of job objects {'name', 'steps'}
    if isinstance(filename_or_steps, list) and filename_or_steps and (isinstance(filename_or_steps[0], list) or isinstance(filename_or_steps[0], dict)):
        log(f"🔄 Running batch of {len(filename_or_steps)} jobs")
        steps_batch = filename_or_steps
        # ensure a parent run_dir exists
        if not run_dir:
            run_dir = os.path.abspath(f'./suite_run_{uuid.uuid4().hex[:8]}')
        os.makedirs(run_dir, exist_ok=True)
        HEADLESS = os.getenv('PLAYWRIGHT_HEADLESS', '1') not in ('0', 'false', 'False')
        results_batch = []
        async with async_playwright() as p:
            # choose browser implementation based on requested browser
            bname = (browser or 'chrome').lower()
            if bname == 'firefox':
                browser = await p.firefox.launch(headless=HEADLESS)
            elif bname == 'webkit':
                browser = await p.webkit.launch(headless=HEADLESS)
            else:
                # default to chromium; if 'chrome' requested try using the chrome channel
                try:
                    if bname == 'chrome':
                        browser = await p.chromium.launch(channel='chrome', headless=HEADLESS)
                    else:
                        browser = await p.chromium.launch(headless=HEADLESS)
                except TypeError:
                    # older playwright might not support channel param; fall back
                    browser = await p.chromium.launch(headless=HEADLESS)
            try:
                # determine concurrency for batch runs (defaults to 1)
                try:
                    concurrency = int(concurrency) if concurrency is not None else 1
                except Exception:
                    concurrency = 1
                concurrency = max(1, min(20, concurrency))

                sem = asyncio.Semaphore(concurrency)

                async def _run_job(idx, job_item):
                    async with sem:
                        # job_item may be a plain list (legacy) or a dict with 'name' and 'steps'
                        if isinstance(job_item, dict):
                            job_name = job_item.get('name')
                            job_steps = job_item.get('steps') or []
                        else:
                            job_name = None
                            job_steps = job_item
                        # ensure each job works on its own copy of steps to avoid
                        # cross-job mutation when running concurrently
                        try:
                            job_steps = copy.deepcopy(job_steps)
                        except Exception:
                            pass
                        this_run_dir = os.path.join(run_dir, f'job_{idx}')
                        os.makedirs(this_run_dir, exist_ok=True)
                        # do not fully substitute now; let executor substitute per-step
                        steps_sub = [substitute_globals_in_step(s) for s in job_steps]
                        results = await _execute_steps_with_browser(browser, steps_sub, this_run_dir, input_was_file=False, input_filename=None, job_name=job_name)
                        return {'name': job_name, 'run_dir': os.path.basename(this_run_dir), 'results': results}
                tasks = [asyncio.create_task(_run_job(idx, item)) for idx, item in enumerate(steps_batch)]
                results_batch = await asyncio.gather(*tasks)
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass

        # Write suite-level summary and report
        try:
            # Build aggregated suite summary
            suite_summary = {'run_time': time.time(), 'jobs': []}
            total_steps = 0
            total_passed = 0
            total_failed = 0
            # results_batch now contains per-job dicts with keys: name, run_dir, results
            for idx, job_entry in enumerate(results_batch):
                job_name = job_entry.get('name')
                # if job_name is None or empty, prefer RUNNER_TESTCASE_NAME env var
                if not job_name:
                    job_name = os.getenv('RUNNER_TESTCASE_NAME') or None
                job_dir_basename = job_entry.get('run_dir') or f'job_{idx}'
                job_results = job_entry.get('results', [])
                passed = sum(1 for r in job_results if r.get('ok'))
                failed = sum(1 for r in job_results if not r.get('ok'))
                steps = len(job_results)
                duration = sum(float(r.get('duration', 0) or 0) for r in job_results)
                total_steps += steps
                total_passed += passed
                total_failed += failed
                suite_summary['jobs'].append({'job_index': idx, 'name': job_name, 'run_dir': job_dir_basename, 'steps': steps, 'passed': passed, 'failed': failed, 'duration': duration, 'results': job_results})

            # ensure browser is serializable (may be a Playwright Browser object)
            browser_name = bname if isinstance(bname, str) else (type(browser).__name__ if browser is not None else None)
            meta = {
                'run_dir': os.path.basename(run_dir),
                'run_time': time.time(),
                'browser': browser_name,
                'profile_id': os.getenv('RUNNER_PROFILE_ID', ''),
                'profile_name': os.getenv('RUNNER_PROFILE_NAME', ''),
                'profile_kv': PROFILE_KV,
                'total_jobs': len(results_batch),
                'total_steps': total_steps,
                'total_passed': total_passed,
                'total_failed': total_failed,
                'duration': sum(j.get('duration', 0) for j in suite_summary['jobs'])
            }

            with open(os.path.join(run_dir, 'summary.json'), 'w', encoding='utf-8') as sf:
                json.dump({'meta': meta, 'jobs': suite_summary['jobs']}, sf, indent=2)

            # Build a single HTML report for the entire run
            html_lines = ['<html><head><meta charset="utf-8"><title>Execution Report</title></head><body>']
            html_lines.append(f"<h1>Execution Report: {os.path.basename(run_dir)}</h1>")
            html_lines.append('<h2>Summary</h2>')
            html_lines.append('<ul>')
            # link to parent run log (assumed served under /runs/<parent_basename>/run.log)
            parent_basename = os.path.basename(run_dir)
            log_link = f"/runs/{parent_basename}/run.log"
            html_lines.append(f"<li>Run log: <a href=\"{log_link}\">run.log</a></li>")
            html_lines.append(f"<li>Browser: {meta['browser']}</li>")
            html_lines.append(f"<li>Profile: {meta['profile_name']} ({meta['profile_id']})</li>")
            html_lines.append(f"<li>Total Testcases: {meta['total_jobs']}</li>")
            html_lines.append(f"<li>Total Steps: {meta['total_steps']}</li>")
            html_lines.append(f"<li>Passed: {meta['total_passed']}</li>")
            html_lines.append(f"<li>Failed: {meta['total_failed']}</li>")
            html_lines.append(f"<li>Aggregate Duration (s): {meta['duration']:.2f}</li>")
            html_lines.append('</ul>')

            # label as Testcases (user-facing) and provide friendly testcase/run links
            html_lines.append('<h2>Testcases</h2>')
            html_lines.append('<table border="1" cellpadding="6" cellspacing="0">')
            html_lines.append('<tr><th>Testcase</th><th>Steps</th><th>Passed</th><th>Failed</th><th>Duration (s)</th><th>Artifacts</th></tr>')
            for j in suite_summary['jobs']:
                # friendly name: prefer an explicit name field, then run_dir basename, then job index
                tc_name = j.get('name') or j.get('run_dir') or f"job_{j.get('job_index')}"
                # create an absolute link under the webapp static '/runs' mount so the link works
                parent_basename = os.path.basename(run_dir)
                # prefer linking to the per-job report.html (static file) rather than directory
                artifacts_link = f"/runs/{parent_basename}/{j['run_dir']}/report.html" if j.get('run_dir') else ''
                html_lines.append(f"<tr><td>{tc_name}</td><td>{j['steps']}</td><td>{j['passed']}</td><td>{j['failed']}</td><td>{j['duration']:.2f}</td><td><a href=\"{artifacts_link}\">artifacts</a></td></tr>")
            html_lines.append('</table>')

            # Optionally show profile key/values
            if meta.get('profile_kv'):
                html_lines.append('<h3>Profile Variables</h3>')
                html_lines.append('<ul>')
                for k, v in meta['profile_kv'].items():
                    html_lines.append(f"<li>{k}: {v}</li>")
                html_lines.append('</ul>')

            html_lines.append('</body></html>')
            with open(os.path.join(run_dir, 'report.html'), 'w', encoding='utf-8') as hf:
                hf.write('\n'.join(html_lines))
        except Exception as e:
            log(f"⚠️ Failed to write suite summary/report: {e}")

        return results_batch

    # Single-test run (either a list of steps or a filename)
    if isinstance(filename_or_steps, list):
        steps = filename_or_steps
        input_was_file = False
        input_filename = None
    else:
        input_was_file = True
        input_filename = filename_or_steps
        with open(filename_or_steps, "r") as f:
            steps = json.load(f)

    # create a fresh playwright browser for this invocation
    HEADLESS = os.getenv('PLAYWRIGHT_HEADLESS', '1') not in ('0', 'false', 'False')
    async with async_playwright() as p:
        bname = (browser or 'chrome').lower()
        if bname == 'firefox':
            browser = await p.firefox.launch(headless=HEADLESS)
        elif bname == 'webkit':
            browser = await p.webkit.launch(headless=HEADLESS)
        else:
            try:
                if bname == 'chrome':
                    browser = await p.chromium.launch(channel='chrome', headless=HEADLESS)
                else:
                    browser = await p.chromium.launch(headless=HEADLESS)
            except TypeError:
                browser = await p.chromium.launch(headless=HEADLESS)
        try:
            # substitute profile globals for single-run steps
            steps_sub = [substitute_globals_in_step(s) for s in steps]
            # try to provide a friendly job_name for single-run (use profile name or filename)
            # prefer friendly name provided via env by the webapp
            job_name = os.getenv('RUNNER_TESTCASE_NAME') or (os.path.basename(input_filename) if input_was_file and input_filename else None)
            results = await _execute_steps_with_browser(browser, steps_sub, run_dir, input_was_file=input_was_file, input_filename=input_filename, job_name=job_name)
        finally:
            try:
                await browser.close()
            except Exception:
                pass
    return results


async def _execute_steps_with_browser(browser, steps, run_dir=None, input_was_file=False, input_filename=None, job_name=None):
    """Execute a list of steps using an existing Playwright browser instance and return results list."""
    # Create a fresh browser context per job for isolation and resource cleanup.
    context = await browser.new_context()
    # provide a per-test variables dict accessible to run_step and custom keywords
    # Use ContextVar.set() which returns a token; we'll reset it in the finally
    # block to restore previous state and prevent leakage between concurrent runs.
    token = set_variables_dict({})
    try:
        page = await context.new_page()
        page.set_default_timeout(PLAYWRIGHT_DEFAULT_TIMEOUT_MS)
        page.set_default_navigation_timeout(PLAYWRIGHT_NAVIGATION_TIMEOUT_MS)
        expect.set_options(timeout=PLAYWRIGHT_ASSERTION_TIMEOUT_MS)
        # shared mutable state so run_step can update the current page (e.g. switchToWindow)
        state = {'page': page}

        results = []
        os.makedirs(run_dir or '.', exist_ok=True)
        step_i = 0
        # screenshot policy: 'every' or 'failure' (default 'failure')
        SS_POLICY = os.getenv('RUNNER_SCREENSHOT_POLICY', 'failure')

        # per-run cache for loaded object locators (object-folder-id -> locators dict)
        _per_run_locators_cache = {}

        for idx, step in enumerate(steps):
            step_i += 1
            # perform substitution once (globals then locals) and assign back
            # into the steps list so healed-locator updates inside run_step
            # mutate the same object and will be persisted when saving.
            # Keep the original step (which may contain placeholders) intact.
            original_step = step
            # Create a substituted view for execution and display, but do not
            # replace the original in the `steps` list — we only want to copy
            # back locator/healed changes after execution so placeholders stay.
            # Resolve selectorRef placeholders (selectors/targetSelector/frameSelector)
            # against object's locators.json before substituting locals/globals.
            try:
                # operate on a shallow copy to avoid mutating original template yet
                temp_step = copy.deepcopy(original_step)
                resolve_selector_refs_in_step(temp_step, seen=_per_run_locators_cache)
            except Exception:
                temp_step = copy.deepcopy(original_step)

            substituted = substitute_locals_in_step(substitute_globals_in_step(temp_step))
            display_step = substituted
            start = time.time()
            try:
                ok = await run_step(state, substituted)
            except Exception as e:
                # Prevent an exception in a single step from bubbling out and
                # closing the whole browser when running batches concurrently.
                tb = traceback.format_exc()
                log(f"⚠️ Step raised exception: {e}\n{tb}")
                # attempt a best-effort screenshot for debugging
                try:
                    if run_dir and page:
                        ss_err = os.path.join(run_dir, f'step_{step_i:03}_error.png')
                        await page.screenshot(path=ss_err, full_page=True)
                        # include screenshot info in the results when possible
                except Exception:
                    pass
                ok = False
            # ensure local page variable follows any changes made by run_step
            page = state.get('page')
            duration = time.time() - start
            # Copy back only locator-related changes from the substituted
            # (executed) step into the original step which may contain
            # placeholders. This preserves placeholders while persisting
            # healed selector values.
            try:
                def _copy_locators(orig, sub):
                    # Sync selectors array and other locator-like keys
                    for key in ('selectors', 'targetSelector'):
                        if key in sub and sub.get(key) != orig.get(key):
                            orig[key] = sub.get(key)

                    # If frameInfo is a list, copy frameSelector/selectors entries
                    if isinstance(sub.get('frameInfo'), list) and isinstance(orig.get('frameInfo'), list):
                        for i, sub_f in enumerate(sub.get('frameInfo')):
                            try:
                                orig_f = orig['frameInfo'][i]
                            except Exception:
                                continue
                            if isinstance(sub_f, dict) and isinstance(orig_f, dict):
                                for fk in ('frameSelector', 'selectors', 'targetSelector'):
                                    if fk in sub_f and sub_f.get(fk) != orig_f.get(fk):
                                        orig_f[fk] = sub_f.get(fk)
                                # recurse in case of nested structures
                                _copy_locators(orig_f, sub_f)

                _copy_locators(original_step, substituted)
            except Exception:
                # best-effort: don't fail the run because copy-back failed
                pass
            # Store the substituted display_step in results so reports show the
            # resolved placeholders instead of the raw template.
            result = {'index': step_i-1, 'step': display_step, 'ok': bool(ok), 'duration': duration}
            # If the step returned a non-boolean value (e.g. getText/getAttribute
            # now return the captured string) include it in the result for
            # easier debugging and reporting.
            if not isinstance(ok, bool) and ok is not None:
                try:
                    result['value'] = ok
                except Exception:
                    # best-effort: if value can't be stored, skip it
                    pass
            # capture screenshot depending on policy
            try:
                if run_dir and (SS_POLICY == 'every' or (SS_POLICY == 'failure' and not ok)):
                    ss_path = os.path.join(run_dir, f'step_{step_i:03}.png')
                    await page.screenshot(path=ss_path, full_page=True)
                    result['screenshot'] = os.path.basename(ss_path)
            except Exception:
                # ignore screenshot failures
                pass
            results.append(result)
            if not ok:
                # stop on failure
                break
    finally:
        # clear module-local variables to avoid leaking between runs by
        # resetting the contextvar to its previous value.
        try:
            reset_variables_token(token)
        except Exception:
            try:
                # best-effort fallback
                set_variables_dict(None)
            except Exception:
                pass
        # Close remaining pages and the context. Keep this simple and tolerant of errors.
        try:
            pages_before = len(getattr(context, 'pages', []))
        except Exception:
            pages_before = 'unknown'

        log(f"🧹 Closing {pages_before} page(s) in context for run_dir={os.path.relpath(run_dir)}")

        for pg in list(getattr(context, 'pages', [])):
            try:
                await pg.close()
            except Exception:
                pass

        try:
            await context.close()
        except Exception:
            pass

        # Brief pause to allow Playwright to flush child processes / windows
        try:
            await asyncio.sleep(0.05)
        except Exception:
            pass

        try:
            rem = len(getattr(browser, 'contexts', []))
            pages_after = sum(len(c.pages) for c in getattr(browser, 'contexts', []))
        except Exception:
            rem = 'unknown'
            pages_after = 'unknown'
        
        log(f"🔒 Closed browser context for run_dir={os.path.relpath(run_dir)}; remaining_contexts={rem}; total_pages_remaining={pages_after}")
    

    # write run summary
    if run_dir:
        summary = {'run_time': time.time(), 'results': results}
        try:
            with open(os.path.join(run_dir, 'summary.json'), 'w', encoding='utf-8') as sf:
                json.dump(summary, sf, indent=2)
        except Exception as e:
            log(f"⚠️ Failed to write summary: {e}")

        # small HTML report (detailed table per step)
        try:
            from html import escape as _escape
            title_name = _escape(job_name) if job_name else _escape(os.path.basename(run_dir))
            # determine parent run basename for linking to the central run.log
            if os.path.basename(run_dir).startswith('job_'):
                parent_basename = os.path.basename(os.path.dirname(run_dir))
            else:
                parent_basename = os.path.basename(run_dir)
            parent_basename_esc = _escape(parent_basename)
            log_link = f"/runs/{parent_basename_esc}/run.log"
            html_lines = ["<html><head><meta charset=\"utf-8\"><title>Run Report</title>",
                          "<style>table{border-collapse:collapse}td,th{border:1px solid #666;padding:6px;text-align:left}th{background:#eee}</style>",
                          "</head><body>", f"<h1>Run Report: {title_name}</h1>"]
            # link to the overall run log
            html_lines.append(f"<p>Run log: <a href=\"{_escape(log_link)}\">run.log</a></p>")
            html_lines.append('<table>')
            html_lines.append('<tr><th>Step #</th><th>Payload</th><th>Status</th><th>Duration (s)</th><th>Screenshot</th></tr>')
            for r in results:
                step_no = (r.get('index') or 0) + 1
                step_payload = r.get('step') or {}
                try:
                    payload_str = _escape(json.dumps(step_payload, ensure_ascii=False))
                except Exception:
                    payload_str = _escape(str(step_payload))
                status = 'PASS' if r.get('ok') else 'FAIL'
                duration = f"{(r.get('duration') or 0):.2f}"
                screenshot_cell = ''
                if r.get('screenshot'):
                    # link relative to run dir
                    screenshot_cell = f"<a href=\"{_escape(r['screenshot'])}\">{_escape(r['screenshot'])}</a>"
                html_lines.append(f"<tr><td>{step_no}</td><td><pre style=\"white-space:pre-wrap;margin:0;font-family:monospace;\">{payload_str}</pre></td><td>{status}</td><td>{duration}</td><td>{screenshot_cell}</td></tr>")
            html_lines.append('</table>')
            html_lines.append('</body></html>')
            with open(os.path.join(run_dir, 'report.html'), 'w', encoding='utf-8') as hf:
                hf.write('\n'.join(html_lines))
        except Exception as e:
            log(f"⚠️ Failed to write HTML report: {e}")

    return results


if __name__ == "__main__":
    import sys
    # Support reading steps JSON from stdin to avoid temp files. Usage:
    # python runner.py --stdin <run_dir>
    if len(sys.argv) < 2:
        log("Usage: python runner.py recorded_steps.json OR python runner.py --stdin [run_dir]")
        sys.exit(1)
    if sys.argv[1] in ("--stdin", "-"):
        # read full stdin
        try:
            payload = sys.stdin.read()
            obj = json.loads(payload)
        except Exception as e:
            log(f"Failed to read JSON from stdin: {e}")
            sys.exit(2)
        run_dir = sys.argv[2] if len(sys.argv) >= 3 else None
        # optional concurrency and browser args may be passed
        concurrency = None
        browser = 'chrome'
        if len(sys.argv) >= 4:
            try:
                concurrency = int(sys.argv[3])
            except Exception:
                concurrency = None
        if len(sys.argv) >= 5:
            browser = sys.argv[4]
        asyncio.run(run_test(obj, run_dir, concurrency=concurrency, browser=browser))
    else:
        if len(sys.argv) >= 4:
            # argv: runner.py <steps.json> <run_dir> <browser?>
            browser = sys.argv[3]
            asyncio.run(run_test(sys.argv[1], sys.argv[2], browser=browser))
        elif len(sys.argv) >= 3:
            asyncio.run(run_test(sys.argv[1], sys.argv[2]))
        else:
            asyncio.run(run_test(sys.argv[1]))
