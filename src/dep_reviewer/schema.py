"""JSON schema for Claude's final verdict (enforced with structured outputs)."""

LEVEL = {"type": "string", "enum": ["low", "medium", "high"]}

REVIEW_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "risk",
        "confidence",
        "summary",
        "dependencies",
        "upstream_changes",
        "impacted_code",
        "test_assessment",
        "recommendations",
    ],
    "properties": {
        "risk": LEVEL,
        "confidence": LEVEL,
        "summary": {"type": "string", "description": "2-4 sentences: what changed and why this risk level."},
        "dependencies": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "ecosystem", "from_version", "to_version", "update_type", "direct", "scope"],
                "properties": {
                    "name": {"type": "string"},
                    "ecosystem": {"type": "string"},
                    "from_version": {"type": "string"},
                    "to_version": {"type": "string"},
                    "update_type": {"type": "string", "enum": ["major", "minor", "patch", "other"]},
                    "direct": {"type": "boolean"},
                    "scope": {"type": "string", "enum": ["runtime", "development", "build", "ci", "unknown"]},
                },
            },
        },
        "upstream_changes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["dependency", "kind", "description", "affects_repo", "source_url"],
                "properties": {
                    "dependency": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": ["breaking", "deprecation", "behavior", "security_fix", "requirement", "feature", "other"],
                    },
                    "description": {"type": "string"},
                    "affects_repo": {"type": "string", "enum": ["yes", "no", "unclear"]},
                    "source_url": {"type": "string", "description": "Where this was found, or empty string."},
                },
            },
        },
        "impacted_code": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["file", "line", "description"],
                "properties": {
                    "file": {"type": "string"},
                    "line": {"type": "integer", "description": "1-based line, or 0 for the whole file."},
                    "description": {"type": "string"},
                },
            },
        },
        "test_assessment": {"type": "string"},
        "recommendations": {"type": "array", "items": {"type": "string"}},
    },
}
