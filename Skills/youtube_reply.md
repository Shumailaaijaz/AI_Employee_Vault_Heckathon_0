# Skill: YouTube Comment Reply

## Purpose
Respond to YouTube comments professionally and helpfully to build community engagement, answer questions, and provide support.

## When to Use
- When `YOUTUBE_*.md` files appear in `/Needs_Action`
- When a comment contains a question
- When a comment reports an issue or needs help
- When a comment deserves acknowledgment (positive feedback)

## Instructions

1. **Read the comment** and understand the context:
   - What video is it on?
   - Is it a question, feedback, complaint, or spam?
   - What is the commenter's tone?

2. **Determine response type**:

| Comment Type | Response Approach | Priority |
|-------------|-------------------|----------|
| Question | Answer directly and helpfully | High |
| Technical Issue | Provide solution or ask for details | High |
| Positive Feedback | Thank them, encourage subscription | Normal |
| Constructive Criticism | Acknowledge, explain if needed | Normal |
| Spam/Troll | Ignore or report, don't engage | Skip |

3. **Draft the reply** following these guidelines:
   - Keep it concise (1-3 sentences usually)
   - Be friendly and professional
   - Use the commenter's name if appropriate
   - Include a call-to-action when relevant (subscribe, check out other videos)
   - Never be defensive or argumentative

4. **Use the YouTube MCP tool** `youtube_draft_reply`:
   - Pass the comment_id, video_id, original comment, and your reply
   - The draft will go to `/Pending_Approval`

5. **After human approves**, use `youtube_reply_comment` to post.

## Response Templates

### Answering a Question
```
Great question, [Name]! [Direct answer]. Let me know if you need more details!
```

### Technical Support
```
Sorry to hear you're having trouble! Try [solution]. If that doesn't work, can you share more details about [specific info needed]?
```

### Thanking Positive Feedback
```
Thanks so much for the kind words, [Name]! Really appreciate you taking the time to comment. 🙏
```

### Acknowledging Suggestion
```
That's a great suggestion! We'll definitely consider that for future videos. Thanks for sharing!
```

## Rules

- NEVER post replies without approval (use youtube_draft_reply first)
- Do NOT reply to obvious spam or troll comments
- Do NOT share personal information
- Do NOT make promises about future content without checking
- Do NOT engage in arguments — stay positive or disengage
- If unsure how to respond, escalate to human with a note in the draft

## Example Workflow

1. Read `/Needs_Action/YOUTUBE_JohnDoe_abc123_2026-02-14.md`
2. Comment asks: "How do I install this on Windows?"
3. Draft reply: "Great question, John! For Windows, download the installer from [link] and run it as administrator. Let me know if you hit any issues!"
4. Call `youtube_draft_reply` with all details
5. File created in `/Pending_Approval`
6. Human reviews and moves to `/Approved`
7. Call `youtube_reply_comment` to post
8. Move original file to `/Done`
