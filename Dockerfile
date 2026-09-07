FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Secrets never come from the image (see .dockerignore excluding .env) —
# every setting in app/config.py is read from real process environment
# variables, set on the hosting platform itself (Render's dashboard, a
# `docker run -e ...`/--env-file, etc.), not baked in here.
COPY . .
# Render (and most PaaS hosts) assign a dynamic port via $PORT and route
# traffic to whatever the service actually binds — a hardcoded 8000 would
# silently never receive a request. Defaults to 8000 for a plain `docker
# run` with no PORT set (e.g. local testing), matching the port EXPOSE
# documents. sh -c is required here (not exec-form CMD) so $PORT is
# actually expanded — CMD's exec form does no shell substitution.
EXPOSE 8000
ENV PORT=8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
