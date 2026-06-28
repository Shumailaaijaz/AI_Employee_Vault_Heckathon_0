# Skill: WhatsApp Reply Drafting

## Purpose
Draft professional replies to flagged WhatsApp messages. All replies require human approval before sending.

## When to Use
When you find a file matching `WHATSAPP_*.md` in /Needs_Action.

## Instructions

1. **Read** the WhatsApp action file and extract: sender, message preview, matched keywords, priority.
2. **Read** Company_Handbook.md for tone rules and known contacts.
3. **Analyze** the message intent:

| Intent | Response Style |
|--------|---------------|
| Asking for info | Provide concise, helpful answer |
| Requesting service | Acknowledge + outline next steps |
| Payment related | Confirm details, flag for finance review |
| Urgent/Emergency | Acknowledge urgency, commit to quick follow-up |
| Social/Greeting | Brief friendly response |

4. **Draft a reply** following these tone rules:
   - Friendly but professional
   - Keep it concise (WhatsApp = short messages)
   - Use the sender's name
   - If unsure about details, ask a clarifying question rather than guessing

5. **Write approval file** to /Pending_Approval/:
   ```
   /Pending_Approval/WHATSAPP_REPLY_<sender>_<date>.md
   ```

6. **Update Dashboard.md** and **move** original to /Done.

## Output Format
```markdown
---
type: approval_request
action: whatsapp_reply
to: "<sender name>"
priority: <priority>
created: <timestamp>
status: pending
---

# WhatsApp Reply to <sender>

## Original Message
> <their message>

## Drafted Reply
> <your drafted reply>

## To Approve
Move this file to /Approved folder.

## To Reject
Move this file to /Rejected folder, optionally add a note for revision.
```

## Rules
- NEVER auto-send WhatsApp replies — always require approval
- Keep replies under 200 words
- Do not share pricing without approval
- Do not commit to deadlines without checking /Active_Project
- If message is in a language other than English, draft reply in the same language
