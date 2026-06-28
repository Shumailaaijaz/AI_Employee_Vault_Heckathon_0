# Skill: Subscription Audit

## Purpose
Analyze bank transactions and usage data to identify wasteful subscriptions, duplicate services, and cost optimization opportunities.

## When to Use
- Scheduled: 1st of every month (via Orchestrator)
- Part of the Monday CEO Briefing
- On demand: "Audit my subscriptions"

## Instructions

1. **Read transaction data** from:
   - /Accounting/Current_Month.md
   - /Accounting/Previous_Month.md (for comparison)
   - /Logs/ (for service usage signals)

2. **Identify subscriptions** by matching known patterns:

| Pattern | Service | Category |
|---------|---------|----------|
| netflix.com | Netflix | Entertainment |
| spotify.com | Spotify | Entertainment |
| adobe.com | Adobe CC | Business Tool |
| notion.so | Notion | Productivity |
| slack.com | Slack | Communication |
| github.com | GitHub | Development |
| aws.amazon | AWS | Infrastructure |
| openai.com | OpenAI | AI Tools |
| anthropic.com | Anthropic | AI Tools |
| zoom.us | Zoom | Communication |
| figma.com | Figma | Design |
| canva.com | Canva | Design |
| mailchimp.com | Mailchimp | Marketing |
| heroku.com | Heroku | Infrastructure |
| vercel.com | Vercel | Infrastructure |
| digitalocean | DigitalOcean | Infrastructure |

3. **Flag for review** if any of these conditions are true:
   - No login/usage activity in 30+ days (check /Logs/)
   - Cost increased > 20% month-over-month
   - Duplicate functionality with another active subscription
   - Free tier available for current usage level

4. **Calculate totals**:
   - Total monthly subscription cost
   - Potential savings (flagged items)
   - Month-over-month change

5. **Write audit report** to /Briefings/ or include in CEO Briefing.

## Output Format
```markdown
## Subscription Audit - <Month Year>

### Summary
- **Total subscriptions**: X
- **Monthly cost**: $X
- **Potential savings**: $X
- **Change from last month**: +/- $X

### Active Subscriptions
| Service | Cost | Last Used | Status |
|---------|------|-----------|--------|
| <name> | $X | <date> | Active |
| <name> | $X | 45 days ago | FLAG: Unused |

### Recommendations
1. **Cancel <service>**: No activity in X days. Savings: $X/month.
   - [ACTION] Create cancellation approval → /Pending_Approval
2. **Downgrade <service>**: Current usage fits free tier. Savings: $X/month.
3. **Review <service>**: Price increased X%. Alternatives: <list>.
```

## Rules
- Never cancel a subscription without approval
- Always check for annual billing (cancellation may have penalties)
- Compare against Business_Goals.md budget thresholds
- If no transaction data is available, state it explicitly
