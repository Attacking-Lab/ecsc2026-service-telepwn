FROM python:3.14

RUN apt update && apt install -y git

COPY --from=docker.io/astral/uv:latest /uv /uvx /bin

WORKDIR /app

RUN --mount=type=cache,target=/root/.cache/uv \
	--mount=type=bind,source=uv.lock,target=uv.lock \
	--mount=type=bind,source=pyproject.toml,target=pyproject.toml \
		uv sync --frozen --exact --compile-bytecode --no-install-project --no-dev

COPY uv.lock pyproject.toml NOTICE .

ADD src .
ADD lines /lines
ADD 9x18.bdf .
ADD 8x13.bdf .

EXPOSE 8000

CMD ["uv", "run", "--no-sync", "gunicorn", "-c", "gunicorn.conf.py", "checker:app"]
