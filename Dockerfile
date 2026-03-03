FROM python:3.12-slim

RUN pip install --no-cache-dir poetry \
    && poetry config virtualenvs.create false

WORKDIR /app

COPY pyproject.toml poetry.lock ./
RUN poetry install --no-root --only main

COPY . .

CMD ["python", "bot.py"]
