---
type: mcp_server_down
server_name: "LinkedIn-Post MCP"
pm2_app: "mcp-linkedin"
consecutive_failures: 4
tools_affected: ['linkedin_get_profile', 'linkedin_draft_post', 'linkedin_publish_post', 'linkedin_delete_post']
fallback_mode: queue
detected: 2026-04-16T15:16:11.245099+00:00
status: pending_approval
---

# MCP Server Down: LinkedIn-Post MCP

The `mcp-linkedin` MCP server has been unresponsive for
4 consecutive health checks.

## Affected Tools
- `linkedin_get_profile`
- `linkedin_draft_post`
- `linkedin_publish_post`
- `linkedin_delete_post`

## What to Do
1. Check logs: `pm2 logs mcp-linkedin`
2. Restart: `pm2 restart mcp-linkedin`
3. If credentials expired, update `.env` and restart.
4. Once fixed, move this file to `/Done`.

## Queued Actions
Any actions requiring these tools have been queued here in
/Pending_Approval until the server is restored.
