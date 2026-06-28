# Skill: LinkedIn Post Generation

## Purpose
Generate engaging LinkedIn business posts for brand awareness, thought leadership, and sales generation. Posts go through HITL approval before publishing.

## When to Use
- Scheduled: Weekly content calendar (e.g., every Tuesday and Thursday)
- On demand: When asked to create a LinkedIn post
- Triggered: After a business milestone (project completed, new client, etc.)

## Instructions

1. **Determine post type**:

| Type | Tone | Goal | Length |
|------|------|------|--------|
| Product Launch | Excited, professional | Awareness + CTA | 150-250 words |
| Thought Leadership | Insightful, authoritative | Engagement | 200-300 words |
| Case Study | Story-driven, results-focused | Social proof | 200-350 words |
| Tip / How-To | Helpful, educational | Value-giving | 100-200 words |
| Milestone | Grateful, forward-looking | Community building | 100-150 words |
| Behind the Scenes | Authentic, casual | Humanize brand | 100-200 words |

2. **Write the post** following LinkedIn best practices:
   - Strong hook in the first line (this shows in the preview)
   - Use line breaks for readability (LinkedIn rewards whitespace)
   - Include 3-5 relevant hashtags at the end
   - End with a question or CTA to drive engagement
   - Avoid salesy language — provide value first

3. **Use the LinkedIn MCP tool** `linkedin_draft_post` to save the draft:
   - Pass the full post text
   - Set visibility (PUBLIC or CONNECTIONS)
   - Add an internal commentary note explaining the post strategy

4. **The draft lands in /Pending_Approval** for human review.

5. **After human approves** (moves to /Approved), use `linkedin_publish_post` to publish.

## Post Templates

### Product Launch
```
We just launched [Product Name] and I couldn't be more excited.

Here's the problem we kept hearing from [target audience]:
→ [Pain point 1]
→ [Pain point 2]

So we built [solution].

[1-2 sentences on key differentiator]

[CTA: Link, DM me, or comment]

#hashtag1 #hashtag2 #hashtag3
```

### Thought Leadership
```
[Contrarian or surprising opening statement]

Most people think [common belief].

But after [experience/data], I've learned that [insight].

Here's why:

1. [Point 1]
2. [Point 2]
3. [Point 3]

[Conclusion + question to audience]

#hashtag1 #hashtag2 #hashtag3
```

## Rules
- NEVER publish without approval (use linkedin_draft_post first)
- Do not post more than once per day
- Avoid controversial topics (politics, religion)
- Do not tag people without approval
- Read Business_Goals.md for current business context before writing
