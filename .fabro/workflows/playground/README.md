# Playground Workflow

## Overview

This workflow generates short science essays with human-in-the-loop topic approval and automated quality validation. Essays are saved to the `essey/` directory.

## Flow

1. **Propose Topic**: AI suggests an interesting science topic
2. **Approval Gate**: Human approves or rejects the topic
3. **Generate Essay**: AI writes a 5-sentence essay with sources
4. **Score Essay**: Automated quality check evaluates:
   - Topic correspondence (must score ≥6/10)
   - Source attachment (must have ≥3 sources)
5. **Retry or Exit**:
   - PASS: Workflow completes successfully
   - FAIL with retries remaining: Loop back to essay generation with feedback
   - FAIL with no retries: Report failure and exit

## Configuration

Edit `workflow.toml` to adjust quality thresholds:

```toml
[workflow.metadata]
max_retries = 3              # Maximum retry attempts (default: 3)
topic_score_threshold = 6    # Minimum topic score 0-10 (default: 6)
min_sources = 3              # Minimum source count (default: 3)
```

## Running

```bash
# Interactive mode (prompts for approval)
fabro run .fabro/workflows/playground/playground.fabro

# Auto-approve mode (for testing)
fabro run .fabro/workflows/playground/playground.fabro --auto-approve
```

## Quality Criteria

### Topic Correspondence (≥6/10)
- Essay directly addresses the stated topic
- Main concepts are covered
- Information is accurate and relevant
- Stays focused without tangents

### Source Attachment (≥3)
- At least 3 distinct URLs in Sources/References section
- Accepts markdown links, raw URLs, or list items
- Sources should be credible (academic, reputable websites)

## Troubleshooting

**Essay keeps failing quality check:**
- Review the feedback in score_essay output
- Adjust thresholds in workflow.toml if criteria are too strict
- Manually review generated essays in the essey/ directory to see patterns

**Workflow doesn't loop on failure:**
- Verify max_visits=4 is set on generate_essay node
- Check that score_essay has conditional edges for PASS and FAIL

**Configuration not applied:**
- Ensure workflow.toml syntax is valid
- Check that quality_gate section name matches exactly
