# 🤖 Personal AI Employee

### Building an Autonomous Digital Full-Time Employee (Digital FTE) with Claude Code, MCP Servers, Python & Obsidian

> **Panaversity – Personal AI Employee Hackathon 0**
> **Theme:** *Building Autonomous FTEs in 2026*

---

## 🚀 Project Vision

Most AI assistants wait for instructions.

**Personal AI Employee** is different.

It continuously monitors business activities, understands context, creates execution plans, requests approval for sensitive operations, and executes tasks autonomously using **Model Context Protocol (MCP)** servers.

Designed with a **Local-First**, **Human-in-the-Loop**, and **Agentic AI** architecture, this project demonstrates how modern AI systems can evolve from simple chatbots into trustworthy **Digital Full-Time Employees (Digital FTEs)** capable of handling real-world business workflows.

---

# 🌟 Hackathon Progress

This repository follows the official **Personal AI Employee Hackathon 0** progression.

| Tier            | Status         | Major Deliverables                                                                                                            |
| --------------- | -------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| 🥉 **Bronze**   | ✅ Completed    | AI Employee Vault, Obsidian Knowledge Base, Gmail Watcher, File System Watcher, Core Orchestrator, Human-in-the-Loop Approval |
| 🥈 **Silver**   | ✅ Completed    | WhatsApp Watcher, LinkedIn Integration, Scheduler, CEO Briefings, Specialized Agent Skills                                    |
| 🥇 **Gold**     | ✅ Completed    | Twitter/X, Facebook, Instagram, YouTube, Odoo Accounting, Cross-Domain Intelligence, Social Media MCP Servers                 |
| 💎 **Platinum** | 🚀 Future Work | Multi-Agent Collaboration, Cloud Deployment, Self-Improving Agents, Autonomous AI Teams                                       |

---

# ✨ What Makes This Project Different?

Unlike traditional AI assistants, this AI Employee can:

* 📧 Monitor Gmail automatically
* 💬 Watch WhatsApp conversations
* 💼 Process LinkedIn notifications
* 🐦 Monitor Twitter/X
* 📘 Watch Facebook activities
* 📸 Monitor Instagram interactions
* 🎥 Track YouTube comments
* 📂 Detect local file changes
* 🧠 Correlate events across multiple platforms
* 📊 Generate CEO Briefings
* 💰 Manage Accounting with Odoo
* 🔌 Execute actions using MCP Servers
* ✅ Require Human Approval for sensitive actions
* 📋 Maintain complete audit logs

---

# 📊 Project Statistics

| Component                  |  Count |
| -------------------------- | -----: |
| Autonomous Watchers        |  **8** |
| MCP Servers                |  **5** |
| Specialized Agent Skills   | **10** |
| Supported Platforms        |  **7** |
| Human-in-the-Loop Workflow |      ✅ |
| Cross-Domain Intelligence  |      ✅ |
| Local-First Architecture   |      ✅ |
| CEO Weekly Briefings       |      ✅ |

---

# 🏗 System Architecture

```text
                          External Platforms

 Gmail | WhatsApp | LinkedIn | Twitter/X
 Facebook | Instagram | YouTube | Local Files
                    │
                    ▼
        ┌──────────────────────────────┐
        │      Python Watchers         │
        └──────────────┬───────────────┘
                       │
                       ▼
        ┌──────────────────────────────┐
        │       Obsidian Vault         │
        │                              │
        │ • Needs_Action               │
        │ • Plans                      │
        │ • Pending_Approval           │
        │ • Dashboard                  │
        │ • Logs                       │
        └──────────────┬───────────────┘
                       │
                       ▼
        ┌──────────────────────────────┐
        │        Claude Code           │
        │                              │
        │ Read → Think → Plan → Write  │
        └──────────────┬───────────────┘
                       │
              Human Approval
                       │
                       ▼
        ┌──────────────────────────────┐
        │        MCP Servers           │
        └──────────────┬───────────────┘
                       │
                       ▼
              External Applications
```

---

# 🔄 Workflow

```text
Incoming Event
      │
      ▼
Watcher detects activity
      │
      ▼
Creates Markdown task
      │
      ▼
Claude Code analyzes context
      │
      ▼
Creates execution plan
      │
      ▼
Sensitive Action?
      │
 ┌────┴─────┐
 │          │
No         Yes
 │          │
 ▼          ▼
Execute   Pending Approval
 via MCP         │
                 ▼
         Human Approval
                 │
                 ▼
          Execute via MCP
                 │
                 ▼
      Update Dashboard & Logs
```

---

# 📁 Repository Structure

```text
AI_Employee_Vault/

├── Dashboard.md
├── Company_Handbook.md
├── Business_Goals.md
│
├── Needs_Action/
├── Plans/
├── Pending_Approval/
├── Approved/
├── Rejected/
├── Done/
├── Logs/
├── Accounting/
├── Briefings/
│
├── watchers/
│   ├── gmail_watcher.py
│   ├── whatsapp_watcher.py
│   ├── linkedin_watcher.py
│   ├── twitter_watcher.py
│   ├── facebook_watcher.py
│   ├── instagram_watcher.py
│   ├── youtube_watcher.py
│   ├── filesystem_watcher.py
│   └── cross_domain_integration.py
│
├── mcp_servers/
│   ├── gmail/
│   ├── linkedin/
│   ├── social_media/
│   ├── youtube/
│   └── odoo/
│
├── skills/
├── orchestrator.py
├── ralph_loop.py
└── README.md
```

---

# 🤖 Autonomous Watchers

| Watcher                      | Purpose                                                  |
| ---------------------------- | -------------------------------------------------------- |
| 📧 Gmail Watcher             | Detects important emails and creates actionable tasks    |
| 💬 WhatsApp Watcher          | Monitors conversations and identifies urgent requests    |
| 💼 LinkedIn Watcher          | Tracks professional notifications and engagement         |
| 🐦 Twitter/X Watcher         | Monitors mentions and direct messages                    |
| 📘 Facebook Watcher          | Watches comments and Messenger activity                  |
| 📸 Instagram Watcher         | Tracks mentions, comments, and messages                  |
| 🎥 YouTube Watcher           | Monitors comments and channel activity                   |
| 📂 File System Watcher       | Detects new or modified local files                      |
| 🔀 Cross-Domain Intelligence | Correlates related events across all monitored platforms |

---

# 🔌 MCP Server Integrations

| MCP Server          | Function                                     |
| ------------------- | -------------------------------------------- |
| 📧 Gmail MCP        | Send and manage emails                       |
| 💼 LinkedIn MCP     | Publish professional posts                   |
| 🌐 Social Media MCP | Facebook, Instagram & Twitter posting        |
| 🎥 YouTube MCP      | Reply to comments                            |
| 💰 Odoo MCP         | Accounting, invoices, and finance automation |

---

# 🧠 Specialized Agent Skills

The AI Employee dynamically selects the most appropriate skill based on the incoming task.

Current skills include:

* Email Triage
* WhatsApp Response Assistant
* LinkedIn Content Generator
* Social Media Publisher
* CEO Briefing Generator
* Accounting Auditor
* Invoice Generator
* Subscription Audit
* Task Planner
* YouTube Comment Assistant

---

# 🔒 Human-in-the-Loop (HITL)

Safety is a core design principle.

The AI Employee **never executes sensitive actions automatically**.

Actions requiring approval include:

* Financial transactions
* Invoice creation
* Sending emails
* Publishing social media posts
* External communications
* File deletion

Approval requests are stored inside:

```text
Pending_Approval/
```

Once approved:

```text
Approved/
```

The Orchestrator executes the task through the appropriate MCP Server.

---

# 🔐 Security

Security is built into every layer of the system.

* ✅ Local-First Architecture
* ✅ Environment Variables for Secrets
* ✅ Human Approval for Sensitive Actions
* ✅ Complete Audit Logging
* ✅ Credential Isolation
* ✅ No Hardcoded Secrets
* ✅ Secure MCP Communication

---

# 🛠 Technology Stack

| Layer                | Technology     |
| -------------------- | -------------- |
| AI Reasoning         | Claude Code    |
| Programming Language | Python 3.13    |
| MCP Servers          | Node.js        |
| Browser Automation   | Playwright     |
| Knowledge Base       | Obsidian       |
| Accounting           | Odoo Community |
| Version Control      | Git & GitHub   |

---

# 🚀 Getting Started

## Clone the Repository

```bash
git clone https://github.com/Shumailaaijaz/AI_Employee_Vault_Heckathon_0.git
cd AI_Employee_Vault_Heckathon_0
```

## Install Dependencies

```bash
uv sync
```

## Install Playwright

```bash
uv run playwright install chromium
```

## Install MCP Server Dependencies

```bash
npm install
```

## Configure Environment

```bash
cp .env.example .env
```

Add your API keys and credentials to the `.env` file.

## Run the AI Employee

```bash
uv run python orchestrator.py
```

---

# 📸 Project Screenshots

> Add screenshots of the following:

* Dashboard
* Obsidian Vault
* Claude Code
* Pending Approval Workflow
* CEO Briefing
* Watchers in Action
* Logs & Reports

---

# 🎥 Demo Video

📺 **Watch the Project Demo**

> Add your YouTube or Google Drive demo link here.

---

# 🗺 Future Roadmap (Platinum Tier)

* 🤝 Multi-Agent Collaboration
* ☁️ Cloud Deployment
* 🧠 Self-Improving Agents
* 📅 Calendar Integration
* 🎙 Voice Assistant
* 💼 Microsoft Teams Integration
* 💬 Slack Integration
* 🌍 Multi-User Enterprise Support

---

# 🙏 Acknowledgements

This project was developed as part of **Panaversity's Personal AI Employee Hackathon 0**, inspired by the vision of building autonomous **Digital Full-Time Employees (Digital FTEs)** using **Claude Code**, **Obsidian**, **Python**, and **Model Context Protocol (MCP)**.

Special thanks to the Panaversity team for designing this challenge and promoting the future of Agentic AI systems.

---

# 👩‍💻 Prepared By

## **Shumaila Aijaz**

**AI Engineer • Agentic AI Developer • Full Stack Developer**

**GitHub:** https://github.com/Shumailaaijaz

---

⭐ **If you found this project interesting, please consider giving it a Star!**

*"The future of software isn't just applications—it's autonomous AI Employees that collaborate with humans to get work done."*

**— Prepared by Shumaila Aijaz**
