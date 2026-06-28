# Gold Tier Setup Guide - Facebook & Odoo Integration

This guide walks you through completing the Gold Tier setup for your Personal AI Employee.

## Prerequisites

- Docker and Docker Compose installed
- Facebook Developer account (developers.facebook.com)
- Twitter Developer account (developer.twitter.com) - optional for Twitter integration
- Google Cloud account (for Gmail/YouTube) - already configured

---

## Part 1: Odoo Community 19 Setup (Docker)

### Step 1: Start Odoo with Docker Compose

```bash
cd /mnt/d/AI_Employee_Vault/AI_Employee_Vault
docker-compose up -d
```

Wait 2-3 minutes for Odoo to initialize.

### Step 2: Access Odoo

Open your browser and go to: http://localhost:8069

**Default credentials:**
- Email: `admin`
- Password: `admin`

### Step 3: Create Database

1. On first launch, you'll see the Odoo database creation screen
2. Database name: `mycompany` (or use the pre-configured `odoo-ai-FTE`)
3. Master password: `admin`
4. Email: `hdrshumaila@gmail.com`
5. Password: Choose a secure password

### Step 4: Install Accounting App

1. Go to **Apps** menu
2. Search for "Invoicing" or "Accounting"
3. Click **Install** on the Accounting app
4. Wait for installation to complete

### Step 5: Configure Accounting Settings

1. Go to **Accounting > Configuration > Settings**
2. Enable features you need:
   - Invoicing
   - Payments
   - Customer Invoices
3. Save settings

### Step 6: Create Test Customer

1. Go to **Accounting > Customers > New**
2. Fill in:
   - Company Name: `Test Customer Inc`
   - Email: `customer@example.com`
   - Phone: `+1234567890`
   - Address: Fill in as needed
3. Save

### Step 7: Update .env File

```bash
cp .env.example .env
```

Edit `.env` and set:
```
ODOO_URL=http://localhost:8069
ODOO_DB=mycompany
ODOO_USER=admin
ODOO_PASS=admin  # or the password you set
ODOO_CURRENCY=USD
```

---

## Part 2: Facebook & Instagram Integration

### Step 1: Create Facebook App

1. Go to [developers.facebook.com](https://developers.facebook.com)
2. Click **My Apps** → **Create App**
3. Select **Business** as app type
4. Fill in:
   - App Name: `AI Employee Integration`
   - Business Account: Select or create
5. Click **Create App**

### Step 2: Add Products

Add these products to your app:
1. **Facebook Login** → Configure for Web
2. **Pages API** → Enable page management
3. **Instagram Graph API** → Enable Instagram integration

### Step 3: Generate Long-Lived Page Access Token

1. Go to [Graph API Explorer](https://developers.facebook.com/tools/explorer/)
2. Select your app from dropdown
3. Click **Get Token** → **Get Page Access Token**
4. Select your Page
5. Request these permissions:
   ```
   pages_read_engagement
   pages_read_user_content
   pages_messaging
   pages_manage_posts
   pages_manage_engagement
   instagram_basic
   instagram_content_publish
   instagram_manage_comments
   instagram_manage_insights
   ```
6. Click **Generate Token** and copy it

### Step 4: Get Page ID and Instagram User ID

**Get Page ID:**
```bash
curl "https://graph.facebook.com/v19.0/me?fields=id,name&access_token=YOUR_TOKEN"
```

**Get Instagram Business User ID:**
```bash
curl "https://graph.facebook.com/v19.0/YOUR_PAGE_ID?fields=instagram_business_account&access_token=YOUR_TOKEN"
```

### Step 5: Update .env File

Add to your `.env`:
```
FACEBOOK_PAGE_ACCESS_TOKEN=your_long_lived_token_here
FACEBOOK_PAGE_ID=your_page_id_here
INSTAGRAM_USER_ID=your_instagram_business_id_here
FACEBOOK_GRAPH_VERSION=v19.0
```

---

## Part 3: Twitter Integration (Optional)

### Step 1: Create Twitter Developer App

1. Go to [developer.twitter.com](https://developer.twitter.com)
2. Apply for a developer account (if you don't have one)
3. Create a new Project and App
4. Set app permissions to **Read + Write + Direct Messages**

### Step 2: Get Credentials

In your app dashboard, get:
- API Key
- API Secret
- Access Token
- Access Token Secret
- Bearer Token

### Step 3: Update .env File

```
TWITTER_BEARER_TOKEN=your_bearer_token
TWITTER_API_KEY=your_api_key
TWITTER_API_SECRET=your_api_secret
TWITTER_ACCESS_TOKEN=your_access_token
TWITTER_ACCESS_TOKEN_SECRET=your_access_token_secret
TWITTER_MAX_TWEETS_PER_DAY=15
```

---

## Part 4: Verify Setup

### Test Odoo Connection

```bash
cd /mnt/d/AI_Employee_Vault/AI_Employee_Vault
uv run python -c "
import requests
resp = requests.get('http://localhost:8069')
print('Odoo Status:', resp.status_code)
"
```

### Test Facebook Token

```bash
curl "https://graph.facebook.com/v19.0/me?access_token=YOUR_TOKEN"
```

Should return your Page info.

### Test MCP Servers

```bash
# Check pm2 status
pm2 status

# Check logs for errors
pm2 logs mcp-odoo --lines 50
pm2 logs mcp-facebook --lines 50
pm2 logs mcp-social --lines 50
```

---

## Part 5: Start the System

### Start All Services

```bash
# Start Odoo
docker-compose up -d

# Start orchestrator (if not running)
pm2 restart orchestrator

# Restart all MCP servers
pm2 restart all

# Check status
pm2 status
```

### Monitor Dashboard

Check `Dashboard.md` for system status updates.

---

## Troubleshooting

### Odoo Won't Start

```bash
# Check logs
docker-compose logs odoo

# Restart
docker-compose restart odoo

# Reset (WARNING: deletes data)
docker-compose down -v
docker-compose up -d
```

### Facebook Token Expired

Error code 190 means token expired. Generate a new long-lived token:
1. Go to Graph API Explorer
2. Generate new token with required permissions
3. Update `.env` and restart watchers

### MCP Server Crashes

```bash
# Check error logs
pm2 logs mcp-odoo --err
pm2 logs mcp-facebook --err

# Restart specific server
pm2 restart mcp-odoo
pm2 restart mcp-facebook
```

### High Restart Counts

If watchers show high restart counts (like Gmail 2640+):
1. Check credentials in `.env`
2. Re-authenticate OAuth tokens
3. Clear session files if needed

---

## Gold Tier Features Checklist

- [ ] Odoo running in Docker
- [ ] Accounting app installed in Odoo
- [ ] Test customer created
- [ ] `.env` configured with Odoo credentials
- [ ] Facebook app created
- [ ] Long-lived Page Access Token generated
- [ ] Page ID and Instagram User ID obtained
- [ ] `.env` configured with Facebook credentials
- [ ] (Optional) Twitter credentials configured
- [ ] All MCP servers healthy (check `pm2 status`)
- [ ] Dashboard shows all systems green

---

## Next Steps

1. **Test Invoice Creation**: Use `odoo_create_invoice` MCP tool
2. **Test Facebook Posting**: Use `facebook_post_message` MCP tool
3. **Test Instagram Posting**: Use `instagram_post_message` MCP tool
4. **Monitor Cross-Domain Integration**: Check `/Cross_Domain/Needs_Action/`
5. **Review CEO Briefing**: Run weekly audit on Monday 07:00

---

## Support

For issues:
1. Check logs in `/Logs/` directory
2. Review `Company_Handbook.md` for approval rules
3. Check `README.md` for architecture details
4. Review skill files in `/Skills/` for workflow instructions
