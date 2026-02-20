# Bozorlik-AI

Bozorlik AI is a Telegram Mini App backend that generates grocery lists, adds estimated prices, and supports voice input.

## Mini App backend

### Requirements

- Python 3.10+

### Local run

1) Create and activate a virtual environment.
2) Install dependencies:

```
pip install -r requirements.txt
```

3) Create a local env file:

```
copy .env.example .env
```

4) Run the API:

```
uvicorn mini_app.app:app --host 0.0.0.0 --port 8000
```

### Environment variables

- `OPENAI_API_KEY`: required for GPT responses and Whisper.
- `AISHA_API_KEY`: optional, enables Uzbek STT.
- `TELEGRAM_BOT_TOKEN`: optional, reserved for Telegram bot usage.
- `CORS_ALLOWED_ORIGINS`: comma-separated list of allowed origins or `*`.
- `DATA_DIR`: path for user data JSON files.
- `PRICES_FILE`: path to `prices.json`.
- `HOST`, `PORT`, `LOG_LEVEL`, `ENVIRONMENT`: runtime settings.