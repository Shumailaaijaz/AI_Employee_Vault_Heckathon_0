---
type: approval_request
action_type: email_send
mcp_server: gmail-send
mcp_tool: send_email
priority: normal
requester: claude
created: 2026-02-14T19:56:49.365677+00:00
status: pending
---

#  Test Email to Client

## Action Details
- **to**: test@example.com
- **subject**: Test Subject
- **body_preview**: This is a test email...

## MCP Execution
```json
{
  "server": "gmail-send",
  "tool": "send_email",
  "args": {
    "to": "test@example.com",
    "subject": "Test Subject",
    "body": "This is a test email body."
}
}
```

## Instructions
- **To Approve**: Move this file to `/Approved/`
- **To Reject**: Move this file to `/Rejected/`

## MCP Arguments
```json
{
  "to": "test@example.com",
  "subject": "Test Subject",
  "body": "This is a test email body."
}
```
