#!/usr/bin/env python3
"""Sync GitHub Project board with issue state. Requires write-scoped GH_TOKEN."""

from github import Github, ProjectColumn, Issue
import os
import yaml

def main():
    gh = Github(os.environ['GH_TOKEN'])
    repo = gh.get_repo('ditto-assistant/ditto-subnet')
    project = repo.get_project(1)  # DittoBench Board ID

    # Map columns by original name (To Do, In Progress, Done)
    original_column_names = ["To Do", "In Progress", "Done"]
    columns = {}
    for col in project.get_columns():
        original_name = col.name.split(' (')[0]
        if original_name in original_column_names:
            columns[original_name] = col

    for issue in repo.get_issues(labels=['bounty']):
        metadata = parse_metadata(issue.body)
        status = metadata.get('status', 'todo')
        
        # Safely get current column position
        current_col = next((c for c in issue.get_project_cards() if c.column_name), None)
        
        target_col = columns.get(status.title())
        # Prevent move if no target or no current column
        if target_col and current_col and current_col.column_id != target_col.id:
            current_col.move(target_col.id)

        update_column_counters(columns)

def parse_metadata(body: str) -> dict:
    """Extract YAML frontmatter from issue body"""
    if not body.startswith('---'):
        return {}
    parts = body.split('---', 2)
    return yaml.safe_load(parts[1]) if len(parts) > 2 else {}

def update_column_counters(columns: dict[str, ProjectColumn]):
    """Update column names with slot counters using original names"""
    for orig_name, col in columns.items():
        cards = col.get_cards()
        available = sum(1 for card in cards if 'available' in (card.note or '').lower())
        claimed = len(cards) - available
        # Reset to original name with fresh counter
        col.edit(f"{orig_name} ({available}/{available + claimed} slots)")

if __name__ == '__main__':
    main()
