# Skill: Email Triage

## Purpose
Classify, prioritize, and route incoming emails from /Needs_Action into the correct workflow.

## When to Use
When you find a file matching `EMAIL_*.md` in /Needs_Action.

## Instructions

1. **Read** the email action file and extract: sender, subject, body snippet, priority.
2. **Read** Company_Handbook.md for known contacts and communication rules.
3. **Classify** the email into one of these categories:

| Category | Action | Example |
|----------|--------|---------|
| Client Request | Create plan + draft reply | "Can you send the invoice?" |
| Lead / Sales | Flag as high priority, draft reply | "Interested in your services" |
| Billing / Invoice | Route to /Accounting, create plan | "Invoice attached", "Payment received" |
| Newsletter / Spam | Archive to /Done, no action needed | Marketing emails, subscriptions |
| Urgent | Flag critical, create approval for reply | Contains "urgent", "asap", "emergency" |
| Personal | Flag for human review | Non-business emails |

4. **Create a plan** in /Plans/:
   ```
   /Plans/PLAN_email_<sender>_<date>.md
   ```

5. **If reply needed** and sender is a known contact → draft reply in plan file.
   If sender is unknown → write approval request to /Pending_Approval/.

6. **Update Dashboard.md** with the new activity.

7. **Move** the original email file to /Done after processing.

## Output Format
```markdown
---
type: plan
source: email_triage
original_file: EMAIL_xxx.md
created: <timestamp>
---

# Plan: Email from <sender>

## Classification
- **Category**: <category>
- **Priority**: <low/medium/high/critical>
- **Requires Approval**: <yes/no>

## Summary
<1-2 sentence summary of the email>

## Action Steps
- [ ] <step 1>
- [ ] <step 2>

## Draft Reply (if applicable)
> <draft reply text>
```

## Rules
- NEVER auto-reply to unknown senders
- NEVER forward emails without approval
- Flag any email mentioning money amounts > $100
- Preserve original email file until plan is complete
