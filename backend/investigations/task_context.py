"""The small, explicit contract between issue providers, preprocessing, and the coding agent."""
PREPROCESS_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "expected_behavior": {"type": "string"},
        "actual_behavior": {"type": "string"},
        "reproduction_steps": {"type": "array", "items": {"type": "string"}},
        "error_details": {"type": "string"},
        "keywords": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "expected_behavior", "actual_behavior", "reproduction_steps", "error_details", "keywords"],
    "additionalProperties": False,
}
SELECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "repository_id": {"type": ["integer", "null"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string"},
    },
    "required": ["repository_id", "confidence", "reason"],
    "additionalProperties": False,
}


def bounded_evidence(value, limit):
    if len(value) <= limit:
        return value
    marker = "\n[Long ticket text shortened; beginning and end retained.]\n"
    head = (limit - len(marker)) * 2 // 3
    tail = limit - len(marker) - head
    return value[:head] + marker + value[-tail:]


def preprocessing_input(ticket):
    # Never forward arbitrary issue fields (reporter, assignee, watchers, attachments,
    # history, comments, etc.) even if a provider response contains them.
    return {
        "title": bounded_evidence(str(ticket.get("title", "")), 200),
        "description": bounded_evidence(str(ticket.get("description", "")), 20000),
        "labels": [str(label)[:100] for label in ticket.get("labels", [])[:20]],
        "components": [str(name)[:200] for name in ticket.get("components", [])[:20]],
    }


def coding_brief(processed):
    """Only validated OpenAI fields enter the higher-model handoff."""
    missing = "Not specified in the ticket."
    steps = processed.get("reproduction_steps", [])
    return "\n\n".join([
        "Bug summary\n" + (processed.get("summary") or missing),
        "Expected behavior\n" + (processed.get("expected_behavior") or missing),
        "Actual behavior\n" + (processed.get("actual_behavior") or missing),
        "Reproduction steps\n" + ("\n".join(f"{index}. {text}" for index, text in enumerate(steps, 1)) or missing),
        "Exact technical evidence\n" + (processed.get("error_details") or missing),
        "Search keywords\n" + ", ".join(processed.get("keywords", [])),
    ])


def coding_task(run):
    return {
        "issue_key": run.issue_key,
        "issue_source": run.issue_source,
        "issue_url": run.issue_url,
        "processed_bug": coding_brief(run.preprocessed),
        "repository": {
            "name": run.repository.name,
            "url": run.repository.url,
            "base_branch": run.base_branch,
            "fix_branch": run.branch,
            "validation_commands": run.repository.validation_commands,
        },
    }
