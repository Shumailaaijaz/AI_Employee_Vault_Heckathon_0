---
last_updated: 2026-06-16T11:08:59.907794+00:00
updated_by: orchestrator
---

# AI Employee Dashboard

## System Status
| Component | Status | Last Check |
|-----------|--------|------------|
| Orchestrator | 🟢 Running | 2026-06-16 11:08:59 |
| pm2 | 🟢 Active | 11:08:59 |

## MCP Servers
| Server | Health | pm2 App | Tools | Failures | Last Check |
|--------|--------|---------|-------|----------|------------|
| 🟢 Gmail-Send MCP | HEALTHY | mcp-gmail | 2 tools | 0 fails | 2026-06-16 11:08 |
| 🟢 Odoo-Accounting MCP | HEALTHY | mcp-odoo | 6 tools | 0 fails | 2026-06-16 11:08 |
| 🟢 LinkedIn-Post MCP | HEALTHY | mcp-linkedin | 4 tools | 0 fails | 2026-06-16 11:08 |
| 🟢 Twitter MCP | HEALTHY | mcp-twitter | 6 tools | 0 fails | 2026-06-16 11:08 |
| 🟢 Social-Post MCP | HEALTHY | mcp-social | 4 tools | 0 fails | 2026-06-16 11:08 |
| 🟢 YouTube MCP | HEALTHY | mcp-youtube | 2 tools | 0 fails | 2026-06-16 11:08 |
| 🟢 Facebook-Instagram MCP | HEALTHY | mcp-facebook | 9 tools | 0 fails | 2026-06-16 11:08 |

## Watchers
| Watcher | State | Last Start | Restarts |
|---------|-------|------------|---------|
| - | Unknown | - | - |

## Queue Counts
- **Needs Action**: 20
- **Pending Approval**: 3
- **Approved (ready)**: 0
- **Done Today**: 0

## Awaiting Approval
| File | Age |
|------|-----|
| APPROVAL_MCP_DOWN_MCP_LINKEDIN_2026-04-16_151611.md | 87592 min ago |
| APPROVAL_MCP_DOWN_MCP_ODOO_2026-04-16_151556.md | 87593 min ago |
| APPROVAL_email_send_Test_Email_to_Client_20260214_195649.md | 175152 min ago |

## Recent Activity (last 10)
| Timestamp | Action | Detail |
|-----------|--------|--------|
| 2026-06-16 11:08:58 | needs_action_dry_run | EMAIL_16e611c1edec70b9_A seller answered |
| 2026-06-16 11:08:59 | needs_action_dry_run | EMAIL_1919d69cb5dc931c_Daraz Affiliate V |
| 2026-06-16 11:08:59 | needs_action_dry_run | EMAIL_192351ed25b5ed67_Daraz Affiliate V |
| 2026-06-16 11:08:59 | needs_action_dry_run | EMAIL_19c2f2eaf9455972_Yay your Order 23 |
| 2026-06-16 11:08:59 | needs_action_dry_run | EMAIL_19c2f82039def014_Order Being Proce |
| 2026-06-16 11:08:59 | needs_action_dry_run | EMAIL_19cc9fbc3c632eec_Confirm your emai |
| 2026-06-16 11:08:59 | needs_action_dry_run | EMAIL_19cca00a406c21a5_Did you just add  |
| 2026-06-16 11:08:59 | needs_action_dry_run | FILE_test_file_2026-06-04.md |
| 2026-06-16 11:08:59 | needs_action_dry_run | FILE_test_file_2026-06-15.md |
| 2026-06-16 11:08:59 | needs_action_dry_run | FILE_test_file_2026-06-16.md |

## Configuration
- **DRY_RUN**: True
- **Poll Interval**: 15s
- **MCP Health Check**: every 5 cycles
- **Process manager**: pm2 active — use `pm2 status` for full process list
