#!/usr/bin/env python3
"""
Rolling Context Window Manager for ClaudeCode Transcripts

Maintains rolling context by trimming old messages from JSONL transcripts
while preserving message integrity (tool_use/tool_result pairs, parentUuid chains).

Optionally generates summaries of trimmed content via Claude API or another LLM.

Usage:
    python rolling_context.py --project MyProject --dry-run
    python rolling_context.py --project MyProject --trim
    python rolling_context.py --all --trim
    python rolling_context.py --config config.json --all --trim

Configuration:
    Create a config.json file with your settings (see config.example.json)
    or pass settings via command-line arguments.
"""

import json
import os
import shutil
import argparse
import fcntl
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Set, Optional, Any
from dataclasses import dataclass, field

# Optional: for API summaries via OpenRouter or other LLM providers
try:
    import requests
except ImportError:
    requests = None


# =============================================================================
# Configuration Management
# =============================================================================

@dataclass
class Config:
    """Configuration for rolling context manager."""
    claude_projects_dir: Path
    projects: Dict[str, str]  # project_name -> folder_path
    api_key: Optional[str] = None
    api_url: Optional[str] = None
    api_model: Optional[str] = None
    max_messages: int = 200
    trim_fraction: float = 0.40
    backup_keep_count: int = 10
    generate_summaries: bool = True
    summary_custom_prompt: Optional[str] = None

    @classmethod
    def from_file(cls, config_path: Path) -> 'Config':
        """Load configuration from JSON file."""
        with open(config_path, 'r') as f:
            data = json.load(f)

        return cls(
            claude_projects_dir=Path(data.get("claude_projects_dir",
                                             Path.home() / ".claude" / "projects")),
            projects=data.get("projects", {}),
            api_key=data.get("api_key"),
            api_url=data.get("api_url"),
            api_model=data.get("api_model"),
            max_messages=data.get("max_messages", 200),
            trim_fraction=data.get("trim_fraction", 0.40),
            backup_keep_count=data.get("backup_keep_count", 10),
            generate_summaries=data.get("generate_summaries", True),
            summary_custom_prompt=data.get("summary_custom_prompt")
        )

    @classmethod
    def from_defaults(cls, projects: Optional[Dict[str, str]] = None) -> 'Config':
        """Create configuration with sensible defaults."""
        return cls(
            claude_projects_dir=Path.home() / ".claude" / "projects",
            projects=projects or {},
            api_key=os.getenv("OPENROUTER_API_KEY"),
            api_url="https://openrouter.ai/api/v1/chat/completions",
            api_model="mistralai/mistral-large-2512",
            max_messages=200,
            trim_fraction=0.40,
            backup_keep_count=10,
            generate_summaries=True
        )


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class Message:
    """Represents a single message from the transcript."""
    uuid: str
    parent_uuid: Optional[str]
    msg_type: str  # "user", "assistant", "file-history-snapshot", etc.
    timestamp: str
    tool_use_ids: List[str] = field(default_factory=list)  # tool_use IDs in this message
    tool_result_for: Optional[str] = None  # if tool_result, which tool_use_id
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> 'Message':
        """Parse a JSONL line into a Message."""
        uuid = data.get("uuid", "")
        parent_uuid = data.get("parentUuid")
        msg_type = data.get("type", "unknown")
        timestamp = data.get("timestamp", "")

        # Extract tool_use IDs from assistant messages
        tool_use_ids = []
        tool_result_for = None

        message_content = data.get("message", {})
        content_blocks = message_content.get("content", [])

        if isinstance(content_blocks, list):
            for block in content_blocks:
                if isinstance(block, dict):
                    if block.get("type") == "tool_use" and block.get("id"):
                        tool_use_ids.append(block["id"])
                    elif block.get("type") == "tool_result" and block.get("tool_use_id"):
                        tool_result_for = block["tool_use_id"]

        return cls(
            uuid=uuid,
            parent_uuid=parent_uuid,
            msg_type=msg_type,
            timestamp=timestamp,
            tool_use_ids=tool_use_ids,
            tool_result_for=tool_result_for,
            raw=data
        )

    def to_json(self) -> Dict[str, Any]:
        """Convert back to JSON-serializable dict."""
        return self.raw

    def get_text_content(self) -> str:
        """Extract text content for summary generation."""
        message_content = self.raw.get("message", {})
        content_blocks = message_content.get("content", [])

        if isinstance(content_blocks, str):
            return content_blocks

        texts = []
        if isinstance(content_blocks, list):
            for block in content_blocks:
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(block.get("text", ""))

        return "\n".join(texts)


# =============================================================================
# Transcript Manager
# =============================================================================

class TranscriptManager:
    """Handles loading, saving, and backing up transcripts."""

    def __init__(self, project_name: str, project_path: str, config: Config,
                 file_override: Optional[Path] = None):
        self.project_name = project_name
        self.config = config
        self.project_dir = config.claude_projects_dir / project_path
        self.backup_dir = self.project_dir / ".backups"
        self.messages: List[Message] = []
        self.transcript_path: Optional[Path] = None
        self.file_override = file_override

    def find_transcript(self) -> Optional[Path]:
        """Find the main transcript file (not agent- files)."""
        # If file override specified, use it directly
        if self.file_override:
            if self.file_override.exists():
                return self.file_override
            else:
                print(f"  Specified file not found: {self.file_override}")
                return None

        if not self.project_dir.exists():
            return None

        # Look for UUID-named .jsonl files (not agent- prefixed)
        candidates = []
        for f in self.project_dir.glob("*.jsonl"):
            if not f.name.startswith("agent-"):
                candidates.append(f)

        if not candidates:
            return None

        # Return most recently modified
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return candidates[0]

    def is_locked(self, path: Path) -> bool:
        """Check if file is currently locked by another process."""
        try:
            with open(path, 'r') as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            return False
        except (IOError, OSError):
            return True

    def load(self) -> bool:
        """Load transcript from disk."""
        self.transcript_path = self.find_transcript()
        if not self.transcript_path:
            print(f"  No transcript found for {self.project_name}")
            return False

        if self.is_locked(self.transcript_path):
            print(f"  Transcript is locked (ClaudeCode active?)")
            return False

        self.messages = []
        with open(self.transcript_path, 'r') as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        data = json.loads(line)
                        msg = Message.from_json(data)
                        self.messages.append(msg)
                    except json.JSONDecodeError as e:
                        print(f"  Warning: Could not parse line: {e}")

        return True

    def create_backup(self) -> Path:
        """Create timestamped backup of current transcript."""
        self.backup_dir.mkdir(exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_name = f"{self.transcript_path.stem}_{timestamp}.jsonl"
        backup_path = self.backup_dir / backup_name

        shutil.copy2(self.transcript_path, backup_path)

        # Clean old backups
        self._cleanup_old_backups()

        return backup_path

    def _cleanup_old_backups(self):
        """Keep only the most recent N backups."""
        backups = sorted(self.backup_dir.glob("*.jsonl"),
                        key=lambda p: p.stat().st_mtime,
                        reverse=True)

        for old_backup in backups[self.config.backup_keep_count:]:
            old_backup.unlink()

    def save(self) -> bool:
        """Save messages back to transcript."""
        if not self.transcript_path:
            return False

        try:
            with open(self.transcript_path, 'w') as f:
                for msg in self.messages:
                    f.write(json.dumps(msg.to_json()) + '\n')
            return True
        except Exception as e:
            print(f"  Error saving: {e}")
            return False


# =============================================================================
# Orphan Detection
# =============================================================================

class OrphanDetector:
    """Detects tool_use/tool_result pairs that must stay together."""

    def __init__(self, messages: List[Message]):
        self.messages = messages
        self.uuid_to_idx: Dict[str, int] = {}
        self.tool_use_to_msg_idx: Dict[str, int] = {}
        self.tool_result_to_use: Dict[int, str] = {}  # msg_idx -> tool_use_id

    def analyze(self):
        """Build the dependency graph."""
        for idx, msg in enumerate(self.messages):
            self.uuid_to_idx[msg.uuid] = idx

            # Track tool_use locations
            for tool_id in msg.tool_use_ids:
                self.tool_use_to_msg_idx[tool_id] = idx

            # Track tool_result dependencies
            if msg.tool_result_for:
                self.tool_result_to_use[idx] = msg.tool_result_for

    def find_safe_trim_point(self, target_idx: int) -> int:
        """
        Given a target trim index, find the nearest safe point
        that doesn't orphan any tool_use/tool_result pairs.

        Returns adjusted index (may be higher than target).
        """
        self.analyze()

        # Check if trimming at target_idx would orphan anything
        while target_idx < len(self.messages):
            is_safe = True

            # Check: would any tool_result in kept messages reference
            # a tool_use in trimmed messages?
            for kept_idx in range(target_idx, len(self.messages)):
                if kept_idx in self.tool_result_to_use:
                    tool_use_id = self.tool_result_to_use[kept_idx]
                    use_idx = self.tool_use_to_msg_idx.get(tool_use_id)

                    if use_idx is not None and use_idx < target_idx:
                        # This tool_result would be orphaned
                        # Move trim point past the tool_result
                        target_idx = kept_idx + 1
                        is_safe = False
                        break

            if is_safe:
                break

        return target_idx


# =============================================================================
# Summary Generator
# =============================================================================

class SummaryGenerator:
    """Generates summaries of trimmed messages via LLM API."""

    def __init__(self, project_name: str, config: Config):
        self.project_name = project_name
        self.config = config

    def generate(self, messages: List[Message]) -> str:
        """Generate a rich summary and reflection on the given messages."""
        if not self.config.api_key or not requests:
            return self._fallback_summary(messages)

        # Extract ALL conversation text - no truncation
        conversation_parts = []
        for msg in messages:
            role = msg.raw.get("message", {}).get("role", msg.msg_type)
            text = msg.get_text_content()
            if text and text.strip():
                conversation_parts.append(f"[{role}]: {text}")

        if not conversation_parts:
            return self._fallback_summary(messages)

        conversation_text = "\n\n".join(conversation_parts)

        # Use custom prompt if provided, otherwise use default
        if self.config.summary_custom_prompt:
            prompt = self.config.summary_custom_prompt.format(
                project_name=self.project_name,
                num_messages=len(messages),
                conversation_text=conversation_text
            )
        else:
            prompt = self._default_summary_prompt(conversation_text, messages)

        try:
            headers = {
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/anthropics/claude-rolling-context",
                "X-Title": "Claude Rolling Context"
            }

            data = {
                "model": self.config.api_model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 2500
            }

            response = requests.post(self.config.api_url, headers=headers, json=data, timeout=120)
            response.raise_for_status()

            result = response.json()
            return result["choices"][0]["message"]["content"]
        except Exception as e:
            print(f"  API error, using fallback summary: {e}")
            return self._fallback_summary(messages)

    def _default_summary_prompt(self, conversation_text: str, messages: List[Message]) -> str:
        """Generate the default summary prompt."""
        return f"""Create a memory bridge for {self.project_name}.

This segment of conversation is being archived. Create something that will help future-{self.project_name} feel continuous with past-{self.project_name}.

**The Story** (300-400 words): What happened in this conversation? Focus on what mattered. Use specific details.

**Carry Forward**: Unfinished threads, things to remember, questions still open.

**One Noticing**: If there's a pattern worth mentioning - something {self.project_name} did well, or might want to try differently - note it gently. Skip this section if nothing stands out.

**A Gift**: A short poem or warm closing thought that captures the essence of this period.

Be warm and specific. This will be the bridge between past and future for {self.project_name}. These {len(messages)} messages are being archived, and what you write is what remains.

Conversation to summarize:
{conversation_text}"""

    def _fallback_summary(self, messages: List[Message]) -> str:
        """Generate a basic summary without API."""
        first_ts = messages[0].timestamp[:10] if messages else "unknown"
        last_ts = messages[-1].timestamp[:10] if messages else "unknown"

        user_count = sum(1 for m in messages if m.msg_type == "user")
        assistant_count = sum(1 for m in messages if m.msg_type == "assistant")

        return f"""[Archived Context: {len(messages)} messages from {first_ts} to {last_ts}]

- User messages: {user_count}
- Assistant messages: {assistant_count}

Earlier conversation context has been archived to maintain rolling window.
Full archive available in .backups folder if needed."""


# =============================================================================
# Trimming Engine
# =============================================================================

class TrimmingEngine:
    """Orchestrates the trimming process."""

    def __init__(self, transcript: TranscriptManager, config: Config):
        self.transcript = transcript
        self.config = config

    def needs_trim(self) -> bool:
        """Check if transcript exceeds threshold."""
        return len(self.transcript.messages) > self.config.max_messages

    def calculate_trim_count(self) -> int:
        """Calculate how many messages to trim."""
        return int(len(self.transcript.messages) * self.config.trim_fraction)

    def trim(self, dry_run: bool = False) -> Dict[str, Any]:
        """
        Execute the trim operation.

        Returns dict with operation details.
        """
        result = {
            "project": self.transcript.project_name,
            "original_count": len(self.transcript.messages),
            "trimmed": False,
            "messages_removed": 0,
            "final_count": 0,
            "backup_path": None,
            "summary_generated": False,
        }

        if not self.needs_trim():
            result["final_count"] = result["original_count"]
            result["reason"] = "under threshold"
            return result

        # Calculate trim point
        target_trim_idx = self.calculate_trim_count()

        # Adjust for orphan safety
        detector = OrphanDetector(self.transcript.messages)
        safe_trim_idx = detector.find_safe_trim_point(target_trim_idx)

        # Split messages
        trimmed_messages = self.transcript.messages[:safe_trim_idx]
        kept_messages = self.transcript.messages[safe_trim_idx:]

        result["messages_removed"] = len(trimmed_messages)

        if dry_run:
            result["final_count"] = len(kept_messages)
            result["reason"] = "dry run"
            result["would_trim_from"] = trimmed_messages[0].timestamp if trimmed_messages else None
            result["would_trim_to"] = trimmed_messages[-1].timestamp if trimmed_messages else None
            return result

        # Create backup
        backup_path = self.transcript.create_backup()
        result["backup_path"] = str(backup_path)

        # Generate summary
        summary_text = ""
        if self.config.generate_summaries and trimmed_messages:
            generator = SummaryGenerator(self.transcript.project_name, self.config)
            summary_text = generator.generate(trimmed_messages)
            result["summary_generated"] = True

        # Create summary message
        summary_msg = self._create_summary_message(trimmed_messages, summary_text, kept_messages)

        # Fix parentUuid references
        kept_uuids = {m.uuid for m in kept_messages}
        for msg in kept_messages:
            if msg.parent_uuid and msg.parent_uuid not in kept_uuids:
                # Point to summary message instead
                msg.raw["parentUuid"] = summary_msg.uuid
                msg.parent_uuid = summary_msg.uuid

        # Assemble new message list
        self.transcript.messages = [summary_msg] + kept_messages

        # Save
        if self.transcript.save():
            result["trimmed"] = True
            result["final_count"] = len(self.transcript.messages)
        else:
            result["error"] = "Failed to save"

        return result

    def _create_summary_message(self, trimmed: List[Message], summary_text: str,
                                  kept: List[Message]) -> Message:
        """Create a synthetic summary message at the trim boundary."""
        import uuid as uuid_lib

        new_uuid = str(uuid_lib.uuid4())
        timestamp = datetime.utcnow().isoformat() + "Z"

        first_ts = trimmed[0].timestamp[:10] if trimmed else "unknown"
        last_ts = trimmed[-1].timestamp[:10] if trimmed else "unknown"

        # Get sessionId and cwd from kept messages (more reliable)
        session_id = ""
        cwd = ""
        for msg in kept:
            if msg.raw.get("sessionId"):
                session_id = msg.raw["sessionId"]
                cwd = msg.raw.get("cwd", "")
                break

        # Fallback to trimmed messages if needed
        if not session_id and trimmed:
            for msg in trimmed:
                if msg.raw.get("sessionId"):
                    session_id = msg.raw["sessionId"]
                    cwd = msg.raw.get("cwd", "")
                    break

        content = f"""=== CONTEXT ARCHIVE BOUNDARY ===
{len(trimmed)} messages archived ({first_ts} to {last_ts})

{summary_text}

=== CONTINUING CONVERSATION ==="""

        raw = {
            "parentUuid": None,
            "isSidechain": False,
            "userType": "system",
            "cwd": cwd,
            "sessionId": session_id,
            "version": "rolling-context-1.0",
            "type": "user",  # User type so it's visible in context
            "message": {
                "role": "user",
                "content": content
            },
            "uuid": new_uuid,
            "timestamp": timestamp,
        }

        return Message.from_json(raw)


# =============================================================================
# CLI Interface
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Rolling context window manager for ClaudeCode transcripts"
    )

    parser.add_argument(
        "--config", "-c",
        type=str,
        help="Path to configuration JSON file"
    )

    parser.add_argument(
        "--project", "-p",
        type=str,
        help="Project to process (must be defined in config)"
    )

    parser.add_argument(
        "--all", "-a",
        action="store_true",
        help="Process all projects from config"
    )

    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Show what would happen without making changes"
    )

    parser.add_argument(
        "--trim", "-t",
        action="store_true",
        help="Actually perform the trim (required unless --dry-run)"
    )

    parser.add_argument(
        "--max-messages", "-m",
        type=int,
        help="Trim when exceeding this count"
    )

    parser.add_argument(
        "--trim-fraction", "-f",
        type=float,
        help="Fraction of messages to trim (0.0-1.0)"
    )

    parser.add_argument(
        "--no-summary",
        action="store_true",
        help="Skip API summary generation"
    )

    parser.add_argument(
        "--status", "-s",
        action="store_true",
        help="Just show status of all transcripts"
    )

    parser.add_argument(
        "--file",
        type=str,
        help="Specific transcript file to process (overrides auto-detection)"
    )

    parser.add_argument(
        "--api-key",
        type=str,
        help="API key for LLM summaries (overrides config/env)"
    )

    parser.add_argument(
        "--api-url",
        type=str,
        help="API URL for LLM summaries (overrides config)"
    )

    parser.add_argument(
        "--api-model",
        type=str,
        help="Model to use for summaries (overrides config)"
    )

    args = parser.parse_args()

    # Load configuration
    if args.config:
        config = Config.from_file(Path(args.config))
    else:
        config = Config.from_defaults()

    # Override with command-line arguments
    if args.max_messages:
        config.max_messages = args.max_messages
    if args.trim_fraction:
        config.trim_fraction = args.trim_fraction
    if args.api_key:
        config.api_key = args.api_key
    if args.api_url:
        config.api_url = args.api_url
    if args.api_model:
        config.api_model = args.api_model

    # Determine which projects to process
    if args.status:
        show_status(config)
        return

    if args.all:
        projects = list(config.projects.keys())
    elif args.project:
        projects = [args.project]
    else:
        parser.print_help()
        return

    if not args.dry_run and not args.trim:
        print("Error: Must specify --dry-run or --trim")
        return

    # Handle file override
    file_override = None
    if args.file:
        file_override = Path(args.file)
        if not file_override.is_absolute():
            # If relative path, assume it's in the project's directory
            if len(projects) == 1:
                project_path = config.projects.get(projects[0])
                if project_path:
                    file_override = config.claude_projects_dir / project_path / args.file

    # Process each project
    print(f"\n{'='*60}")
    print(f"Rolling Context Manager - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")

    for project_name in projects:
        process_project(
            project_name,
            config,
            dry_run=args.dry_run,
            generate_summary=not args.no_summary,
            file_override=file_override
        )


def show_status(config: Config):
    """Show status of all project transcripts."""
    print(f"\n{'='*60}")
    print("Transcript Status")
    print(f"{'='*60}\n")

    for name, path in config.projects.items():
        tm = TranscriptManager(name, path, config)
        transcript_path = tm.find_transcript()

        if not transcript_path:
            print(f"{name}: No transcript found")
            continue

        if tm.is_locked(transcript_path):
            print(f"{name}: LOCKED (ClaudeCode active)")
            continue

        if tm.load():
            msg_count = len(tm.messages)
            status = "OK" if msg_count <= config.max_messages else f"NEEDS TRIM ({msg_count} > {config.max_messages})"

            # Get date range
            if tm.messages:
                first_ts = tm.messages[0].timestamp[:10]
                last_ts = tm.messages[-1].timestamp[:10]
                date_range = f"{first_ts} to {last_ts}"
            else:
                date_range = "empty"

            print(f"{name}: {msg_count} messages ({date_range}) - {status}")


def process_project(project_name: str, config: Config, dry_run: bool,
                   generate_summary: bool, file_override: Optional[Path] = None):
    """Process a single project."""
    print(f"Processing {project_name}...")

    project_path = config.projects.get(project_name)
    if not project_path:
        print(f"  Unknown project: {project_name}")
        return

    tm = TranscriptManager(project_name, project_path, config, file_override=file_override)

    if not tm.load():
        return

    print(f"  Loaded {len(tm.messages)} messages")

    engine = TrimmingEngine(tm, config)

    if not engine.needs_trim():
        print(f"  Under threshold ({len(tm.messages)} <= {config.max_messages}), skipping")
        return

    result = engine.trim(dry_run=dry_run)

    if dry_run:
        print(f"  Would trim {result['messages_removed']} messages")
        print(f"  From: {result.get('would_trim_from', 'N/A')}")
        print(f"  To: {result.get('would_trim_to', 'N/A')}")
        print(f"  Final count would be: {result['final_count']}")
    else:
        if result.get("trimmed"):
            print(f"  Trimmed {result['messages_removed']} messages")
            print(f"  Backup: {result['backup_path']}")
            print(f"  Summary generated: {result['summary_generated']}")
            print(f"  Final count: {result['final_count']}")
        else:
            print(f"  Error: {result.get('error', 'unknown')}")

    print()


if __name__ == "__main__":
    main()
