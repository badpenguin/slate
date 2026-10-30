"""Pure diff projections; historical rows never belong to editable source."""

from dataclasses import dataclass
from difflib import SequenceMatcher


@dataclass(frozen=True)
class DiffRow:
    """Describe a visual line and its optional current-document coordinates."""

    text: str
    kind: str
    old_line: int | None
    new_line: int | None
    gap_start: int | None = None
    gap_end: int | None = None


def project_diff(before: str, current: str) -> list[DiffRow]:
    """Build a unified projection without adding diff markers to source text."""

    # 2026-10-30: separate presentation from canonical text so deleted rows
    # cannot leak into saves or create a second editable document.
    old = before.splitlines(keepends=True)
    new = current.splitlines(keepends=True)
    rows = []
    for operation, start_old, end_old, start_new, end_new in SequenceMatcher(
        None, old, new, autojunk=True
    ).get_opcodes():
        if operation == "equal":
            for offset in range(end_new - start_new):
                rows.append(DiffRow(new[start_new + offset], "equal",
                                    start_old + offset, start_new + offset))
            continue
        # Replacements retain old rows as read-only history before new rows.
        for index in range(start_old, end_old):
            rows.append(DiffRow(old[index], "removed", index, None))
        for index in range(start_new, end_new):
            rows.append(DiffRow(new[index], "added", None, index))
    return rows


def editable_ranges(rows: list[DiffRow], current: str, start: int, end: int) -> list[tuple[int, int]]:
    """Map a visual character selection to editable canonical ranges only."""

    lines = current.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    ranges: list[tuple[int, int]] = []
    visual = 0
    for row in rows:
        finish = visual + len(row.text)
        if row.new_line is not None and start < finish and end > visual:
            left = offsets[row.new_line] + max(0, start - visual)
            right = offsets[row.new_line] + min(len(row.text), end - visual)
            if ranges and ranges[-1][1] == left:
                ranges[-1] = (ranges[-1][0], right)
            else:
                ranges.append((left, right))
        visual = finish
    return ranges


def compact_rows(rows: list[DiffRow], context: int = 3,
                 expanded: set[tuple[int, int]] | None = None) -> list[DiffRow]:
    """Keep changed blocks with context and represent omitted ranges explicitly."""

    # 2026-10-30: compact mode is only a projection; hidden equal lines remain
    # in the canonical source and cannot be deleted by a visible selection.
    expanded = expanded or set()
    output = []
    index = 0
    while index < len(rows):
        if rows[index].kind != "equal":
            output.append(rows[index])
            index += 1
            continue
        end = index
        while end < len(rows) and rows[end].kind == "equal":
            end += 1
        head = min(end, index + context) if index else index
        tail = max(head, end - context) if end < len(rows) else end
        if index == 0:
            tail = max(head, end - context) if end < len(rows) else end
        if (head, tail) in expanded or head == tail:
            output.extend(rows[index:end])
        else:
            output.extend(rows[index:head])
            output.append(DiffRow(f"⋯ {tail - head} unchanged lines · double-click to expand ⋯\n",
                                  "omitted", None, None, head, tail))
            output.extend(rows[tail:end])
        index = end
    return output
