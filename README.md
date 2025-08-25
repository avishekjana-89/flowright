# Flowwright — README

This README explains how to use the Flowwright web UI, recorder extension, test assets (testcases / suites / testdata), variable bindings (Global, Local, Testdata), profiles and settings, custom keywords, and how to build/run the app locally or with Docker.

## Quick overview

- ![Test Cases UI](assets/testcases.png)
- *Screenshot: the Test Cases page (place the attached image at `assets/testcases.png` to display inline).* 
-
- Web UI entrypoint: the FastAPI app in `webapp/` (templates and routes live in `webapp/templates` and `webapp/routers`).
- Recorded steps live in `webapp/steps/` and are saved as JSON arrays (one file per testcase). Test data files live in `webapp/data/`.
- Object repository (locators) live in `webapp/objects/<object-folder>/locators.json`.
- Custom keywords live in `keywords/` (Python `.py` files). They register themselves with `keyword_registry`.

## Quick start (development)

1. Install Python dependencies:

```bash
# Use your virtualenv/venv. Example (macOS / zsh):
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Run the web app (dev):

```bash
# from project root
uvicorn webapp.main:app --reload --port 8000
```

3. Open http://127.0.0.1:8000 in your browser.

## Run with Docker

Build the image (optional) and run with docker-compose (the repo contains `Dockerfile` and `docker-compose.yml`):

```bash
# build locally (optional)
docker build -t flowwright:latest .

# start services with docker-compose (from project root)
docker compose up -d

# tail logs
docker compose logs -f
```

If you prefer the image built by `docker compose` itself, the `docker compose up` call will build as needed.

## Chrome recorder extension

Files live in the `extension/` folder. The extension name is `FlowCapture` (manifest v3). The recorder UI provides a persistent recorder window and a popup with these main actions:

- Start / Pause / Stop recording
- Refresh / Deduplicate / Clear recorded events
- Download recorded steps as `recorded_steps.json`
- Pin site — attaches sidebar/recorder to a site origin

How to load the extension into Chrome (developer flow):

1. Open Chrome and go to `chrome://extensions/`.
2. Enable **Developer mode** (top-right).
3. Click **Load unpacked** and select the `extension/` directory from this repo.
4. Click the extension icon to open the popup. Use **Download** to export `recorded_steps.json`.

The downloaded `recorded_steps.json` is a JSON array of steps compatible with the webapp's testcase creation upload form.

## Importing recorded steps into a testcase

1. Use the extension popup **Download** button to get `recorded_steps.json`.
2. Go to Web UI -> Testcases -> New, give a name and choose an object folder, then upload that JSON file.
3. Inspect steps in the testcase detail page and adjust selectors, selectorRefs, or values as needed.

## Creating a testcase

Flowwright testcases are JSON files containing an ordered array of step objects. The web UI expects a `.json` file when you create a testcase (`/testcases/new` -> upload JSON file).

Example minimal testcase (save as `my_tc.json` and upload):

```json
[
  {"action": "goto", "url": "https://example.com"},
  {"action": "click", "selectors": ["#login"]},
  {"action": "fill", "selectors": ["#email"], "value": "{{ users.email }}"},
  {"action": "fill", "selectors": ["#password"], "value": "{{ password }}"},
  {"action": "click", "selectors": ["#submit"]}
]
```

Notes:
- Steps are objects with at least an `action` field (supported built-in actions include `goto`, `click`, `fill`, `press`, `upload`, `verifyElementText`, etc.).
- Targeting: use `selectors` array (ordered fallbacks) or `selectorRef` to reference a key in the object repository: `selectorRef: "$myButton"` (the runner resolves `$...` against `webapp/objects/<object-folder>/locators.json`).
- When creating a testcase via the UI you must select or create an object repository folder (used to persist healed locators).

Saved testcases are written under `webapp/steps/` as `<uuid>.json`. The DB keeps a record with `filename` referencing that file.

## Creating testdata (datasets)

Supported formats (put under `webapp/data/`):

- JSON — must be an array of objects. Example `users.json`:

```json
[
  {"name": "Alice", "email": "alice@example.com", "password": "pass1"},
  {"name": "Bob", "email": "bob@example.com", "password": "pass2"}
]
```

- CSV — first row must be header. Example `users.csv`:

```csv
name,email,password
Alice,alice@example.com,pass1
Bob,bob@example.com,pass2
```

How to use dataset values in a testcase step:

- When a dataset is used for a run, the runner builds a context with the dataset namespace equal to the filename without extension. If your dataset is `users.json`, reference fields as `{{ users.email }}`.
- For convenience the runner also injects the current row's keys into the top-level substitution context. So if a row has `email`, you can also use `{{ email }}`.
- Substitution is performed server-side before a run by `webapp.utils.substitute_step`.

Example step with dataset placeholders:

```json
{ "action": "fill", "selectors": ["#email"], "value": "{{ users.email }}" }
```

Or, since row keys are merged into the context:

```json
{ "action": "fill", "selectors": ["#email"], "value": "{{ email }}" }
```

## Creating a suite

- Use the web UI: `Suits -> New` or `/suites/new`. Select testcases to compose into a suite and save.
- Run a suite from the suite detail page; you can set concurrency and choose a browser.
- Suites that reference testcases with attached dataset filenames will expand the dataset rows into individual jobs automatically.

## Global / Local / Testdata variable bindings

Flowwright supports three variable kinds used in step value substitution:

1. Testdata placeholders — replaced when you run a testcase with a dataset. Syntax: `{{ datasetNamespace.field }}` or `{{ field }}` for top-level merged fields. Implemented in `webapp/utils.py` `substitute_step`.

2. Global variables (profile KV pairs) — syntax: `{{ GlobalVariables.MY_KEY }}`.
   - Profiles are managed in the web UI under **Profiles** (`/profiles`). Each profile contains key/value pairs.
   - The selected profile (set from **Settings**) is passed to the runner as `RUNNER_PROFILE_JSON` environment variable and to substitution logic; the runner substitutes `{{ GlobalVariables.KEY }}` at runtime.

3. Local variables (per-run/test) — syntax: `{{ LocalVariables.MY_KEY }}`.
   - Local variables are managed at runtime by the runner using the `variables` context proxy (`variables` in `variables.py`).
   - Custom keywords (Python code) and runtime steps can read/write local variables through that proxy.

Substitution order/notes:

- When running with a dataset the webapp first instantiates step copies per row and runs `substitute_step` to replace dataset placeholders.
- The runner also applies global and local substitutions at runtime (see `runner_utils.substitute_globals_in_step` and `substitute_locals_in_step`).

Example of using all three in one step:

```json
{
  "action": "fill",
  "selectors": ["#token"],
  "value": "{{ GlobalVariables.API_TOKEN }}:{{ LocalVariables.sessionId }}:{{ session_user }}"
}
```

In the example above `session_user` could come from the dataset row, `API_TOKEN` from the selected profile, and `sessionId` could be set earlier by a custom keyword.

## Profiles and Settings page

- Settings page: `/settings` exposes runner timeouts (default, navigation, assertion) and `screenshot_policy` and a **Default profile** selector. Settings are stored in the DB and used when spawning runner subprocesses.
- Profiles page: `/profiles` lets you create/edit profiles. Each profile has key/value pairs; these are injected into runs as the `GlobalVariables` mapping.

When a profile is selected in settings, its key/value pairs will be available to runs automatically.

## Custom keywords (user-defined Python keywords)

Location: `keywords/` directory. Keywords are Python functions that register via the `@keyword(...)` decorator from `keyword_registry.py`.

Example custom keyword (save as `keywords/my_keyword.py`):

```python
from keyword_registry import keyword

@keyword('custom_click', description='Click and return status')
async def custom_click(page, step):
    selector = step.get('selectors', [None])[0]
    try:
        await page.locator(selector).click(force=True)
        return {'success': True}
    except Exception as e:
        return {'success': False, 'message': str(e)}

```

How to use in a testcase step:

```json
{ "action": "custom_click", "selectors": ["#my-button"] }
```

Notes:
- The runner auto-loads any `.py` files in the `keywords/` directory on startup (see `keyword_registry.load_keywords_from_dir`).
- The registered `name` (first arg to `@keyword`) is used as the `action` value in step JSON.
- Keyword functions can be async or sync. If sync returns an awaitable, the runner awaits it.

## Where files are stored (paths)

- Steps: `webapp/steps/*.json`
- Data: `webapp/data/*.json | *.csv`
- Objects (locators): `webapp/objects/<folder>/locators.json`
- Keywords (custom): `keywords/*.py`

## Troubleshooting / tips

- If locators get healed during runs the runner attempts to persist healed selectors to `webapp/objects/<folder>/locators.json` using an advisory lock.
- If a keyword import or syntax error occurs when creating keywords via the UI, the web UI reports the Python compile/import error — fix the code and try again.
- Use the `Runs` page to inspect run directories and `run.log` files when debugging runner failures.
