# Render.com Deployment Guide

## Quick Deploy to Render.com

### Option 1: Using Render Dashboard (Recommended)

1. **Go to Render.com Dashboard**
   - https://dashboard.render.com

2. **Create New Web Service**
   - Click "New +" → "Web Service"
   - Connect your GitHub repository
   - Or use "Deploy from Git"

3. **Configure Service**
   ```
   Name: bozorlik-ai-web
   Environment: Python 3
   Region: Oregon (or closest to your database)
   Branch: main (or your default branch)
   
   Build Command: pip install -r requirements.txt
   Start Command: uvicorn app:app --host 0.0.0.0 --port $PORT
   ```

4. **Add Environment Variables**
   ```
   DATABASE_URL=postgresql://bushstep:9zhog9hAMrwCnpzuDewkt0zAGQ1lQ6qn@dpg-d5r8vhkhg0os73crbds0-a.oregon-postgres.render.com/postgresql_ldlv
   TELEGRAM_BOT_TOKEN=your_telegram_bot_token
   OPENAI_API_KEY=your_openai_api_key
   AISHA_API_KEY=your_aisha_api_key
   ```

5. **Deploy**
   - Click "Create Web Service"
   - Wait for build to complete
   - Your app will be live at: `https://your-service-name.onrender.com`

### Option 2: Using render.yaml (Infrastructure as Code)

The `render.yaml` file is already configured. Just:

1. Push your code to GitHub
2. Connect repository to Render
3. Render will auto-detect `render.yaml` and deploy

### Option 3: Manual Deploy

```bash
# Make build script executable
chmod +x render_build.sh

# Test build locally
./render_build.sh

# Push to Render
git add .
git commit -m "Add Render deployment configuration"
git push
```

## Troubleshooting

### asyncpg Build Errors

If you see errors like `gcc failed with exit code 1`:

**Solution 1**: Render should have PostgreSQL headers installed by default. If not, the build will use pre-compiled wheels.

**Solution 2**: The `requirements.txt` now uses a compatible version range:
```
asyncpg>=0.27.0,<0.30.0
```

This allows pip to choose a version with pre-built wheels for your platform.

### Database Connection Issues

Make sure `DATABASE_URL` environment variable is set in Render dashboard:
```
DATABASE_URL=postgresql://bushstep:9zhog9hAMrwCnpzuDewkt0zAGQ1lQ6qn@dpg-d5r8vhkhg0os73crbds0-a.oregon-postgres.render.com/postgresql_ldlv
```

### Port Binding

Render provides the PORT environment variable. The app is configured to use:
```python
uvicorn app:app --host 0.0.0.0 --port $PORT
```

This automatically binds to Render's assigned port.

## Files for Deployment

✅ `requirements.txt` - Python dependencies (asyncpg version fixed)
✅ `runtime.txt` - Python version specification
✅ `Procfile` - Process definition
✅ `render.yaml` - Render configuration (optional)
✅ `render_build.sh` - Custom build script (optional)

## Environment Variables Required

Set these in Render Dashboard → Environment:

```env
DATABASE_URL=postgresql://...
TELEGRAM_BOT_TOKEN=your_token
OPENAI_API_KEY=your_key
AISHA_API_KEY=your_key
AISHA_POST_URL=https://back.aisha.group/api/v2/stt/post/
AISHA_GET_URL=https://back.aisha.group/api/v2/stt/get/
```

## Deployment Checklist

- [x] requirements.txt updated with compatible asyncpg version
- [x] runtime.txt created (Python 3.11.0)
- [x] Procfile created for process management
- [x] render.yaml created for infrastructure as code
- [x] DATABASE_URL configured
- [ ] Push code to GitHub
- [ ] Connect repository to Render
- [ ] Set environment variables in Render
- [ ] Deploy!

## Expected Build Output

```
==> Building...
Installing Python dependencies...
Successfully installed asyncpg-0.28.0 fastapi-0.128.0 ...
Build completed successfully!

==> Deploying...
Starting service with uvicorn...
✅ PostgreSQL database connected successfully
✅ Database schema initialized
✅ Bozorlik AI Web Backend успешно запущен

==> Your service is live at https://your-app.onrender.com
```

## Testing After Deployment

1. Visit your Render URL
2. Check `/health` endpoint
3. Test creating a shopping list
4. Verify database synchronization with Telegram bot

## Success!

Your app should now be deployed and synchronized with PostgreSQL! 🎉
