# AI Employee Vault - Project Instructions

## What This Is
This is a Personal AI Employee vault powered by Claude Code and Obsidian.
You (Claude) are the reasoning engine. This vault is your memory and workspace.

## Vault Structure
- `/Inbox` - Raw incoming items (unprocessed drops)
- `/Needs_Action` - Items requiring Claude's attention (watchers write here)
- `/Plans` - Claude-generated plan files with checkboxes
- `/Pending_Approval` - Sensitive actions waiting for human approval
- `/Approved` - Human-approved actions ready for execution
- `/Rejected` - Human-rejected actions (do not execute)
- `/Done` - Completed tasks (archive)
- `/Logs` - JSON audit logs of all actions taken
- `/Accounting` - Financial records and transaction logs
- `/Briefings` - Generated CEO briefings and reports
- `/Active_Project` - Currently active project files
- `/Skills` - Agent skill definitions

## Key Files
- `Dashboard.md` - Real-time status summary (update after every action)
- `Company_Handbook.md` - Rules of engagement (ALWAYS follow these rules)
- `Business_Goals.md` - Business objectives and metrics (read for context)

## Core Rules
1. **ALWAYS read Company_Handbook.md before taking action** - it contains approval thresholds and communication rules
2. **NEVER execute sensitive actions without approval** - write to `/Pending_Approval` instead
3. **ALWAYS update Dashboard.md** after completing any task
4. **ALWAYS log actions** to `/Logs/YYYY-MM-DD.json`
5. **Process /Needs_Action first** - this is your priority queue
6. **Create Plans before acting** - write a Plan.md with checkboxes for multi-step tasks
7. **Move completed items to /Done** with a timestamp prefix

## Approval-Required Actions
These MUST go through /Pending_Approval:
- Any payment or financial transaction
- Emails to new/unknown contacts
- Social media posts
- Deleting or modifying existing files outside the vault
- Any action over the thresholds in Company_Handbook.md

## Auto-Approved Actions
- Reading any file in the vault
- Creating plan files
- Updating Dashboard.md
- Writing to /Logs
- Replying to known contacts (per Company_Handbook.md)

## Agent Skills
You have reusable skills in `/Skills/`. **Read the relevant skill file before performing that task type.** Each skill defines inputs, instructions, output format, and rules.

| Skill | File | Trigger |
|-------|------|---------|
| Email Triage | `/Skills/email_triage.md` | `EMAIL_*.md` in /Needs_Action |
| WhatsApp Reply | `/Skills/whatsapp_reply.md` | `WHATSAPP_*.md` in /Needs_Action |
| YouTube Reply | `/Skills/youtube_reply.md` | `YOUTUBE_*.md` in /Needs_Action |
| LinkedIn Post | `/Skills/linkedin_post.md` | Scheduled or on-demand post request |
| CEO Briefing | `/Skills/ceo_briefing.md` | Monday 7 AM or on-demand briefing request |
| Invoice Generator | `/Skills/invoice_generator.md` | Invoice request from client message |
| Subscription Audit | `/Skills/subscription_audit.md` | Monthly or on-demand audit |
| Task Planner | `/Skills/task_planner.md` | Any multi-step item in /Needs_Action |

### How to Use Skills
1. When processing an item from /Needs_Action, identify its type from the filename prefix (EMAIL_, WHATSAPP_, YOUTUBE_, LINKEDIN_, FILE_).
2. Read the corresponding skill file from /Skills/.
3. Follow the skill's instructions step by step.
4. Use the skill's output format for any files you create.
5. If no specific skill matches, use `task_planner.md` as the default.

## File Naming Conventions
- Action files: `TYPE_identifier_YYYY-MM-DD.md` (e.g., `EMAIL_client_a_2026-01-07.md`)
- Plans: `PLAN_description_YYYY-MM-DD.md`
- Approvals: `APPROVAL_action_description.md`
- Logs: `YYYY-MM-DD.json`
- Briefings: `YYYY-MM-DD_Monday_Briefing.md`
