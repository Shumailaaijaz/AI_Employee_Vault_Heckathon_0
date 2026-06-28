# Skill: Social Media Post & Engagement

## Purpose
Draft, approve, publish, and summarise posts across Facebook, Instagram, and Twitter/X
using the `social-post` MCP server tools.

## When to Use
- `FACEBOOK_*.md`, `INSTAGRAM_*.md`, or `TWITTER_*.md` in /Needs_Action contains a post request
- User asks to "post on Facebook/Instagram/Twitter", "draft a social post", or "get post metrics"
- Weekly CEO briefing — include social engagement summary
- Cross-domain item routed to `social-post` MCP server

## MCP Tools Available (social-post server)

| Tool                       | Platform         | HITL? | Description                                    |
|----------------------------|------------------|-------|------------------------------------------------|
| `facebook_post_message`    | Facebook         | Yes   | Post text + optional link to a Facebook Page   |
| `instagram_post_message`   | Instagram        | Yes   | Publish image or Reel to Instagram Business    |
| `twitter_post_tweet`       | Twitter/X        | Yes   | Post a tweet (with optional reply threading)   |
| `social_generate_summary`  | All 3 platforms  | No    | Fetch likes, comments, shares, reach for a post|
| `social_rate_limit_status` | All 3 platforms  | No    | Show current rate-limit windows before posting |

## Instructions

### A. Publishing a Post (from /Needs_Action)

1. Read the action file to extract: platform, content/caption, image_url (if IG), target audience.
2. Check `social_rate_limit_status` before any batch posting.
3. Check Company_Handbook.md for social media rules (tone, forbidden topics, approval thresholds).
4. If DRY_RUN=true (default):
   - Call the appropriate publish tool → draft goes to /Pending_Approval automatically.
   - Update Dashboard.md: "Social post pending approval: <filename>".
5. If action file came from /Approved (human already approved):
   - Set DRY_RUN=false context and call the publish tool.
   - Log the result to /Logs.
   - Write a summary file to /Done.
6. After publishing, call `social_generate_summary` after a delay (add as a follow-up task).

### B. Generating Engagement Summaries

1. Get the post_id from the published post's Done file (or Needs_Action item).
2. Call `social_generate_summary(platform, post_id)`.
3. If summarising for CEO briefing, write the metrics to the briefing file under `## Social Media`.
4. Flag posts with low engagement (< 10 likes after 24h) for review.

### C. Urgent Message Response Workflow

When a watcher writes a FACEBOOK_*.md or TWITTER_*.md to /Needs_Action with `priority: high`:
1. Read the original message/mention content.
2. Draft a reply using appropriate tone (see Company_Handbook.md).
3. For Facebook comments: use `facebook_post_message` with the comment's post as context
   (note: direct replies to comments require the Graph API `/{comment_id}/comments` endpoint).
4. For Twitter mentions: use `twitter_post_tweet` with `reply_to_tweet_id` set.
5. All replies go through /Pending_Approval unless auto-approved in Company_Handbook.md.

## Output Format (Post Draft in /Pending_Approval)

Auto-created by the MCP tools. Each draft contains:
```markdown
---
type: facebook_post | instagram_post | twitter_tweet
platform_target: <page_id or ig_user_id>
preview: "<first 100 chars>"
status: pending_approval
created: <ISO timestamp>
dry_run: true
---

# <Platform> Post Draft
<content>

## To Approve
Move to /Approved and ask Claude to publish.
```

## Output Format (Engagement Summary for CEO Briefing)

```markdown
## Social Media Summary (Last 7 Days)

| Platform   | Posts | Total Likes | Comments | Shares/RT |
|------------|-------|-------------|----------|-----------|
| Facebook   | N     | N           | N        | N         |
| Instagram  | N     | N           | N        | —         |
| Twitter    | N     | N           | N        | N         |

### Top Performing Post
- Platform: Facebook
- Content: "first 100 chars..."
- Engagement: 45 likes, 12 comments, 8 shares

### Action Items
- [ ] Reply to 3 unanswered comments on Facebook post <ID>
- [ ] Follow up on high-engagement tweet <ID>
```

## Rate Limits Reference

| Platform  | Guard Limit          | Platform Hard Limit        |
|-----------|----------------------|----------------------------|
| Facebook  | 25 posts / hour      | ~200 API calls/hour        |
| Instagram | 24 posts / day       | 50 content publishes/day   |
| Twitter   | 15 tweets / day      | 17 tweets/day (free tier)  |

## Rules
- **NEVER** post without checking Company_Handbook.md for tone/content guidelines.
- **ALWAYS** run `social_rate_limit_status` before batch operations (3+ posts).
- **NEVER** post to Instagram without a valid image_url (Graph API will reject it).
- **ALWAYS** log all publish actions to /Logs and archive in /Done.
- Twitter replies require `reply_to_tweet_id` — do NOT post standalone tweets as replies.
- If a token is expired (Facebook error code 190), create a /Needs_Action item for the human to refresh it.
- Posts about payments, prices, or legal matters **always** require human approval.
