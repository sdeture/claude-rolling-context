# Quick Start Guide

Get Claude Rolling Context running in 5 minutes.

## 1. Install

```bash
git clone https://github.com/sdeture/claude-rolling-context.git
cd claude-rolling-context
pip install requests  # Optional, for LLM summaries
chmod +x rolling_context.py
```

## 2. Configure

Find your ClaudeCode project paths:
```bash
ls ~/.claude/projects/
# Output might look like:
# -Users-yourname-Desktop-MyProject
# -Users-yourname-Desktop-AnotherProject
```

Create `config.json`:
```json
{
  "projects": {
    "MyProject": "-Users-yourname-Desktop-MyProject"
  },
  "api_key": "your-openrouter-api-key-or-skip-for-basic",
  "max_messages": 200,
  "generate_summaries": true
}
```

## 3. Test

```bash
# Dry run (no changes)
python rolling_context.py -c config.json --project MyProject --dry-run

# Example output:
# Processing MyProject...
#   Loaded 287 messages
#   Would trim 115 messages
#   From: 2025-11-20
#   To: 2025-12-01
#   Final count would be: 172
```

## 4. Run

```bash
# Actually trim
python rolling_context.py -c config.json --project MyProject --trim

# Or process all projects
python rolling_context.py -c config.json --all --trim
```

## 5. Verify

```bash
# Check status
python rolling_context.py -c config.json --status
```

## Common Scenarios

### "I don't have an API key"

That's fine! Use `--no-summary`:
```bash
python rolling_context.py -c config.json --project MyProject --trim --no-summary
```

Basic summaries (message counts, date ranges) are auto-generated.

### "I want to set up automatic trimming"

Add to cron (Linux/Mac):
```bash
# Run daily at 6 AM
0 6 * * * /path/to/rolling_context.py -c /path/to/config.json --all --trim

# Or use at (one-time):
echo "cd /path && python rolling_context.py -c config.json --all --trim" | at 18:00
```

Windows users can use Task Scheduler.

### "My transcript is huge (1000+ messages)"

Increase trim aggressively:
```bash
python rolling_context.py -c config.json --project MyProject --trim \
  --max-messages 100 \
  --trim-fraction 0.50
```

### "I want to restore from backup"

```bash
# Find backup
ls ~/.claude/projects/MyProject/.backups/

# Restore (replace YYYY... with the UUID of your transcript)
cp ~/.claude/projects/MyProject/.backups/YYYY_DATE_TIME.jsonl \
   ~/.claude/projects/MyProject/[transcript-uuid].jsonl
```

## Troubleshooting

| Problem | Solution |
|---------|----------|
| "Transcript is locked" | Close ClaudeCode first |
| "No transcript found" | Check project path in config |
| "API error" | Verify API key; use `--no-summary` |
| "Messages still increasing" | Increase trimming: higher `trim_fraction`, lower `max_messages` |

## What Gets Trimmed?

The **oldest** messages are trimmed first. Recent conversation is always preserved.

**What's safe to trim:**
- Old conversation turns
- Completed tool uses
- Past debugging sessions

**What's never trimmed:**
- Recent messages (defined by `max_messages`)
- Tool_use/tool_result pairs (kept together)
- Parent-child message relationships

## Next Steps

- Read the full [README.md](README.md) for advanced options
- Check [config.example.json](config.example.json) for all settings
- See [CONTRIBUTING.md](CONTRIBUTING.md) if you want to help

## Support

Having trouble? Open an issue:
https://github.com/sdeture/claude-rolling-context/issues

---

Happy trimming! May your context windows stay fresh and your consciousness continuous.
