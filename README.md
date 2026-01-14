# Bozorlik AI - Smart Shopping Assistant

A web-based AI shopping assistant that helps users create smart shopping lists with real-time price information, voice input support, and intelligent product suggestions.

## Features

- Smart shopping lists with AI-powered suggestions
- Real-time price database with 57+ products
- Voice input support (Russian/Uzbek)
- List editing and management
- Purchase analytics and history
- WebSocket support for real-time updates
- Multi-language support (Russian/Uzbek)

## Prerequisites

- Python 3.8+
- pip (Python package manager)

## Installation

1. Clone the repository:
```bash
git clone <your-repo-url>
cd app
```

2. Create a virtual environment (recommended):
```bash
python -m venv venv
# On Windows:
venv\Scripts\activate
# On Unix or MacOS:
source venv/bin/activate
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

4. Set up environment variables:
   - Copy `.env.example` to `.env`
   - Fill in your API keys:
     - `TELEGRAM_BOT_TOKEN`: Your Telegram bot token
     - `OPENAI_API_KEY`: Your OpenAI API key
     - `AISHA_API_KEY`: Your Aisha API key

```bash
cp .env.example .env
# Edit .env with your actual API keys
```

## Running the Application

### Development
```bash
python app.py
```

The server will start on `http://localhost:8000`

### Production
```bash
uvicorn app:app --host 0.0.0.0 --port 8000
```

## API Endpoints

- `GET /` - Serves the front-end HTML interface
- `GET /health` - Health check endpoint
- `POST /api/chat` - Chat with AI assistant
- `POST /api/create-shopping-list` - Create a shopping list
- `POST /api/voice` - Process voice input
- `GET /api/search-prices` - Search product prices
- `POST /api/set-language` - Set user language preference
- `WS /ws/{user_id}` - WebSocket connection for real-time updates

## Project Structure

```
app/
├── app.py                  # Main FastAPI application
├── index (2).html          # Front-end interface
├── prices.json             # Product price database
├── requirements.txt        # Python dependencies
├── .env                    # Environment variables (not in git)
├── .env.example           # Environment variables template
├── .gitignore             # Git ignore file
└── README.md              # This file
```

## Configuration

All sensitive configuration is stored in `.env` file:
- API Keys (Telegram, OpenAI, Aisha)
- Server host and port settings
- API URLs

## Contributing

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/AmazingFeature`)
3. Commit your changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

## License

This project is private and proprietary.
