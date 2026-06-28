# Skill: Task Planning

## Purpose
Break down incoming action items into structured, actionable plans with checkboxes, dependencies, and time estimates.

## When to Use
- When any new item arrives in /Needs_Action that requires multiple steps
- When asked to plan a project or task
- When a complex approval request needs a workflow

## Instructions

1. **Read the action item** and understand:
   - What is being requested?
   - Who is the requestor?
   - What is the deadline (if any)?
   - What resources/information are needed?

2. **Check context**:
   - /Active_Project/ for related ongoing work
   - /Plans/ for existing plans that may overlap
   - Company_Handbook.md for rules and thresholds
   - Business_Goals.md for priority alignment

3. **Break down into steps**:
   - Each step should be a single, concrete action
   - Identify dependencies (which steps block others)
   - Flag steps that require human approval
   - Estimate effort (small/medium/large)

4. **Write the plan** to /Plans/:
   ```
   /Plans/PLAN_<description>_<YYYY-MM-DD>.md
   ```

5. **Update Dashboard.md** with the new plan.

## Output Format
```markdown
---
type: plan
source: <triggering file or request>
created: <timestamp>
estimated_effort: <small/medium/large>
priority: <low/medium/high/critical>
deadline: <date or "none">
status: in_progress
---

# Plan: <clear description>

## Objective
<1-2 sentences describing the desired outcome>

## Context
- **Requested by**: <source>
- **Priority**: <priority>
- **Deadline**: <date or "No deadline">
- **Related project**: <project name or "None">

## Steps
- [ ] **Step 1**: <description> (effort: small)
- [ ] **Step 2**: <description> (effort: medium)
  - Depends on: Step 1
  - REQUIRES APPROVAL
- [ ] **Step 3**: <description> (effort: small)
- [ ] **Step 4**: Update Dashboard and move to /Done

## Approval Gates
- Step 2 requires human approval: <reason>

## Risks
- <potential issue and mitigation>

## Definition of Done
- [ ] All steps completed
- [ ] Approval obtained where needed
- [ ] Dashboard updated
- [ ] Original action file moved to /Done
```

## Rules
- Every plan must have a "Definition of Done" section
- Plans with more than 5 steps should be split into sub-plans
- Always identify approval gates upfront
- If deadline is < 24 hours, mark as CRITICAL priority
- Link related plans if they share dependencies
