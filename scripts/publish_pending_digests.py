#!/usr/bin/env python3
"""Publish pending digest payloads into digests.json / briefing_state.json.

Invoked by .github/workflows/publish-pending-digest.yml from the repo root
(pass a different root as argv[1] for local testing). For each
pending-digests/*.json payload it:

1. Validates the taskId contract (docs/digest-taskid-contract.md) — hard fail.
2. Prepends the digest to digests.json unless its id already exists.
3. Reconciles the payload's `state` with the latest repository state so a
   stale pending payload cannot erase newer completions or hidden items.
4. Removes completed or hidden items from Carried Over before publication.
5. Auto-consumes reminders (safety net): any ACTIVE reminder whose text
   matches an item label in the digest's "Reminders" section is moved to
   reminderHistory, whether or not the payload included a `state`. Without
   this, a state-less payload leaves consumption unrecorded, so the same
   reminders stay in the dashboard queue and re-appear in every subsequent
   briefing (this is exactly what happened 2026-07-28..30).
6. Deletes the pending file.

Auto-consumption mirrors the lifecycle rules in the daily-digest skill:
- persistent reminders (`expiresAt` after the digest date) stay active —
  they are meant to show every morning until expiry;
- scheduled reminders (`scheduledFor` after the digest date) stay active —
  if one was shown early by mistake it must still fire on its real date;
- everything else shown in the digest is moved to reminderHistory with
  `shownInDigestId` + `archivedAt`, and `lastModified` is bumped so older
  Supabase state cannot resurrect it.
"""

import json
import pathlib
import re
import sys
from datetime import datetime, timezone

REQUIRED_STATE_KEYS = {'completed', 'hidden', 'reactions', 'edits',
                       'reminders', 'reminderHistory'}
REMINDER_SECTION_RE = re.compile(r'reminder|recordator', re.IGNORECASE)


def normalize(text):
    """Whitespace-insensitive form of a reminder text / item label."""
    return ' '.join(str(text).split())


def validate_task_ids(pending_file, digest):
    """Contract: every action item (non-info section) must carry a stable
    taskId so carry-over completion survives across days. The display `id`
    is per-day and disposable; taskId is the canonical key."""
    seen_task_ids = set()
    for section in digest.get('sections', []):
        if not isinstance(section, dict) or section.get('info'):
            continue
        for item in section.get('items', []):
            if not isinstance(item, dict):
                continue
            task_id = item.get('taskId')
            if not task_id or not isinstance(task_id, str) or not task_id.strip():
                raise SystemExit(
                    f"{pending_file} item {item.get('id')!r} in section "
                    f"{section.get('title')!r} is missing a taskId"
                )
            if not re.match(r'^task-\d{8}-', task_id):
                print(f'WARNING: taskId {task_id!r} does not match expected '
                      f'format task-YYYYMMDD-<slug> ({pending_file})')
            if task_id in seen_task_ids:
                print(f"WARNING: duplicate taskId {task_id!r} within {digest.get('id')}")
            seen_task_ids.add(task_id)


def shown_reminder_labels(digest):
    """Normalized labels of every item in the digest's Reminders section(s)."""
    labels = set()
    for section in digest.get('sections', []):
        if not isinstance(section, dict):
            continue
        if not REMINDER_SECTION_RE.search(section.get('title') or ''):
            continue
        for item in section.get('items', []):
            if isinstance(item, dict) and item.get('label'):
                labels.add(normalize(item['label']))
    return labels


def auto_consume_reminders(state, digest, now_iso):
    """Move active reminders shown in `digest` into reminderHistory.

    Returns the ids consumed. Mutates `state` in place."""
    labels = shown_reminder_labels(digest)
    if not labels:
        return []

    digest_date = digest.get('isoDate') or ''
    history = state.setdefault('reminderHistory', [])
    history_ids = {r.get('id') for r in history if isinstance(r, dict)}
    kept, consumed = [], []
    for rem in state.get('reminders', []):
        if not isinstance(rem, dict) or normalize(rem.get('text', '')) not in labels:
            kept.append(rem)
            continue
        expires = rem.get('expiresAt')
        if expires and (not digest_date or digest_date < expires):
            kept.append(rem)  # persistent: shows daily until expiry
            continue
        scheduled = rem.get('scheduledFor')
        if scheduled and digest_date and digest_date < scheduled:
            kept.append(rem)  # not due yet: must still fire on its real date
            continue
        consumed.append(rem.get('id'))
        if rem.get('id') not in history_ids:
            entry = dict(rem)
            entry['shownInDigestId'] = digest.get('id')
            entry['archivedAt'] = now_iso
            history.append(entry)
    state['reminders'] = kept
    return consumed


def merge_states(current, incoming):
    """Reconcile a pending payload with repository-backed UI state.

    Briefing publication must never roll back cumulative completion or hidden
    markers. Map-like UI fields are merged with repository state winning on
    conflicts, and reminder consumption wins over preservation.
    """
    current = current if isinstance(current, dict) else {}
    incoming = incoming if isinstance(incoming, dict) else {}
    merged = dict(incoming)

    for key in ('completed', 'reactions', 'edits', 'assigneeOverrides'):
        values = dict(incoming.get(key) or {})
        values.update(current.get(key) or {})
        if key == 'completed':
            for source in (current.get(key) or {}, incoming.get(key) or {}):
                for task_id, value in source.items():
                    if value is True:
                        values[task_id] = True
        merged[key] = values

    merged['hidden'] = list(dict.fromkeys(
        list(current.get('hidden') or []) + list(incoming.get('hidden') or [])
    ))

    history_by_id = {}
    history_without_id = []
    for source in (current.get('reminderHistory') or [], incoming.get('reminderHistory') or []):
        for reminder in source:
            if not isinstance(reminder, dict):
                history_without_id.append(reminder)
            elif reminder.get('id'):
                history_by_id[reminder['id']] = reminder
            else:
                history_without_id.append(reminder)
    merged['reminderHistory'] = history_without_id + list(history_by_id.values())

    active_by_id = {}
    active_without_id = []
    for source in (current.get('reminders') or [], incoming.get('reminders') or []):
        for reminder in source:
            if not isinstance(reminder, dict):
                active_without_id.append(reminder)
            elif reminder.get('id'):
                active_by_id[reminder['id']] = reminder
            else:
                active_without_id.append(reminder)
    consumed_ids = set(history_by_id)
    merged['reminders'] = active_without_id + [
        reminder for reminder_id, reminder in active_by_id.items()
        if reminder_id not in consumed_ids
    ]

    merged['lastModified'] = max(
        current.get('lastModified') or 0,
        incoming.get('lastModified') or 0,
    )
    return merged


def prune_completed_carryovers(digest, state):
    """Remove completed/hidden carry-overs and return the removed taskIds."""
    completed = state.get('completed') or {}
    hidden = set(state.get('hidden') or [])
    removed = []
    kept_sections = []
    for section in digest.get('sections', []):
        if not isinstance(section, dict) or section.get('info'):
            kept_sections.append(section)
            continue
        if (section.get('title') or '').strip().lower() != 'carried over':
            kept_sections.append(section)
            continue
        kept_items = []
        for item in section.get('items', []):
            task_id = item.get('taskId') if isinstance(item, dict) else None
            display_id = item.get('id') if isinstance(item, dict) else None
            if completed.get(task_id) is True or task_id in hidden or display_id in hidden:
                removed.append(task_id or display_id)
            else:
                kept_items.append(item)
        if kept_items:
            section['items'] = kept_items
            kept_sections.append(section)
    digest['sections'] = kept_sections
    return removed


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n',
                    encoding='utf-8')


def main(root):
    pending_dir = root / 'pending-digests'
    pending_files = sorted(pending_dir.glob('*.json')) if pending_dir.exists() else []
    if not pending_files:
        print('No pending digest files found.')
        return

    digests_path = root / 'digests.json'
    state_path = root / 'briefing_state.json'

    digests = json.loads(digests_path.read_text(encoding='utf-8'))
    if not isinstance(digests, list):
        raise SystemExit('digests.json must contain a JSON array')

    current_state = json.loads(state_path.read_text(encoding='utf-8'))
    existing_ids = {item.get('id') for item in digests if isinstance(item, dict)}
    new_digests = []
    parsed_digests = []
    state_update = None

    for pending_file in pending_files:
        payload = json.loads(pending_file.read_text(encoding='utf-8'))
        digest = payload.get('digest', payload)
        if not isinstance(digest, dict):
            raise SystemExit(f'{pending_file} does not contain a digest object')
        digest_id = digest.get('id')
        if not digest_id:
            raise SystemExit(f'{pending_file} digest is missing id')
        validate_task_ids(pending_file, digest)
        # A duplicate id is skipped for digests.json, but its reminders are
        # still auto-consumed below — re-queueing an already-published digest
        # is the sanctioned way to record consumption it missed.
        if digest_id in existing_ids:
            print(f'Skipping duplicate digest {digest_id}')
        else:
            new_digests.append(digest)
            existing_ids.add(digest_id)
        parsed_digests.append(digest)
        if isinstance(payload, dict) and isinstance(payload.get('state'), dict):
            missing = REQUIRED_STATE_KEYS - set(payload['state'])
            if missing:
                raise SystemExit(
                    f'{pending_file} state update is missing required keys: {sorted(missing)}')
            state_update = payload['state']
        else:
            print(f'WARNING: {pending_file} has no state payload; relying on '
                  f'reminder auto-consumption. The generator should include '
                  f'the full state (see the daily-digest skill).')

    effective_state = merge_states(current_state, state_update or current_state)
    for digest in parsed_digests:
        removed = prune_completed_carryovers(digest, effective_state)
        if removed:
            print(f"Removed {len(removed)} completed/hidden carry-over(s) from "
                  f"{digest.get('id')}: {', '.join(map(str, removed))}")

    if new_digests:
        digests = new_digests + digests
        write_json(digests_path, digests)
        print(f'Prepended {len(new_digests)} digest(s).')
    else:
        print('No new digests to prepend.')

    # Safety net: consume shown reminders against the reconciled state (a no-op
    # when the generator already consumed them in its payload).

    now = datetime.now(timezone.utc)
    now_iso = now.strftime('%Y-%m-%dT%H:%M:%SZ')
    consumed_any = False
    for digest in parsed_digests:
        consumed = auto_consume_reminders(effective_state, digest, now_iso)
        if consumed:
            consumed_any = True
            print(f"Auto-consumed {len(consumed)} reminder(s) shown in "
                  f"{digest.get('id')}: {', '.join(map(str, consumed))}")

    if consumed_any:
        # Newer than any pre-publish Supabase write, so the sync workflow's
        # newest-wins fields can't roll this consumption back.
        effective_state['lastModified'] = int(now.timestamp() * 1000)

    if state_update is not None or consumed_any:
        write_json(state_path, effective_state)
        print('Updated briefing_state.json.')

    for pending_file in pending_files:
        pending_file.unlink()
        print(f'Removed {pending_file}')


if __name__ == '__main__':
    main(pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else '.'))
