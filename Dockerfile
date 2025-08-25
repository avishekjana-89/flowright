FROM mcr.microsoft.com/playwright/python:v1.54.0

# keep python output unbuffered
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

# Install Playwright browsers to a shared, predictable location so runtime user doesn't need a passwd/home entry
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

ARG APP_USER=flow
ARG APP_UID=1000

WORKDIR /app

# Install python deps first to leverage Docker layer caching
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy the application code. Use .dockerignore to keep build context small.
COPY . /app

# Ensure Playwright browsers are installed into $PLAYWRIGHT_BROWSERS_PATH (idempotent)
RUN mkdir -p ${PLAYWRIGHT_BROWSERS_PATH} \
	&& PLAYWRIGHT_BROWSERS_PATH=${PLAYWRIGHT_BROWSERS_PATH} playwright install --with-deps

# Create system chrome path pointing to Playwright's installed Chromium so channel='chrome' works
RUN set -eux; \
	mkdir -p /opt/google/chrome; \
	CHROMIUM_DIR=$(sh -c 'ls -d ${PLAYWRIGHT_BROWSERS_PATH}/chromium_* 2>/dev/null | head -n1' || true); \
	if [ -n "$CHROMIUM_DIR" ]; then \
		if [ -e "$CHROMIUM_DIR/chrome-linux/headless_shell" ]; then \
			ln -sfn "$CHROMIUM_DIR/chrome-linux/headless_shell" /opt/google/chrome/chrome; \
		elif [ -e "$CHROMIUM_DIR/chrome-linux/chrome" ]; then \
			ln -sfn "$CHROMIUM_DIR/chrome-linux/chrome" /opt/google/chrome/chrome; \
		fi; \
	fi

# Install gosu and curl for runtime healthchecks and safe privilege drop
RUN apt-get update && apt-get install -y --no-install-recommends gosu ca-certificates curl && rm -rf /var/lib/apt/lists/*

# Create a non-root user and make app directories writable in a distro-robust way
RUN set -eux; \
	mkdir -p /app/webapp/data /app/webapp/databases /app/webapp/runs /app/webapp/steps /app/keywords; \
	if id -u "${APP_USER}" >/dev/null 2>&1; then \
		echo "user ${APP_USER} already exists"; \
	else \
		if command -v addgroup >/dev/null 2>&1 && command -v adduser >/dev/null 2>&1; then \
			addgroup -g "${APP_UID}" "${APP_USER}" || true; \
			adduser -D -u "${APP_UID}" -G "${APP_USER}" "${APP_USER}" || true; \
		elif command -v groupadd >/dev/null 2>&1 && command -v useradd >/dev/null 2>&1; then \
			groupadd -g "${APP_UID}" "${APP_USER}" || true; \
			useradd -m -u "${APP_UID}" -g "${APP_UID}" -s /bin/bash "${APP_USER}" || true; \
		else \
			echo "no useradd/adduser available in base image; skipping user creation"; \
		fi; \
	fi; \
	if id -u "${APP_USER}" >/dev/null 2>&1; then \
		chown -R "${APP_USER}":"${APP_USER}" /app; \
		mkdir -p /home/${APP_USER}; \
		chown ${APP_UID}:${APP_UID} /home/${APP_USER} || true; \
		# ensure per-user playwright cache path points to shared browsers folder
		mkdir -p /home/${APP_USER}/.cache; \
		ln -sfn ${PLAYWRIGHT_BROWSERS_PATH} /home/${APP_USER}/.cache/ms-playwright || true; \
		chown -h ${APP_UID}:${APP_UID} /home/${APP_USER}/.cache/ms-playwright || true; \
	else \
		echo "user ${APP_USER} not present; leaving ownership as-is"; \
	fi

# Ensure /etc/passwd contains an entry for the runtime UID so getpwuid()/uv_os_homedir work
RUN awk -F: -v uid="${APP_UID}" 'BEGIN{found=0} $3==uid{found=1} END{if(found==0) exit 1}' /etc/passwd || \
	echo "${APP_USER}:x:${APP_UID}:${APP_UID}:${APP_USER}:/home/${APP_USER}:/bin/bash" >> /etc/passwd

# Ensure /app is owned by the runtime UID and provide a writable HOME to Node/Playwright
RUN mkdir -p /app/.cache /app/.config ${PLAYWRIGHT_BROWSERS_PATH} || true \
	&& chown -R ${APP_UID}:${APP_UID} /app ${PLAYWRIGHT_BROWSERS_PATH} || true
# Use /app as HOME so uv_os_homedir can return a valid directory even if passwd lacks an entry
ENV HOME=/app

# Copy entrypoint and make executable (run as root during build)
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

# Healthcheck: perform an HTTP GET to / (gives a clearer app-level health) and allow longer startup
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=5 \
	CMD curl -fsS --max-time 5 http://127.0.0.1:8000/ || exit 1

# Expose default webapp port
EXPOSE 8000

# Default command — run the FastAPI app
CMD ["uvicorn", "webapp.main:app", "--host", "0.0.0.0", "--port", "8000"]