"""Exact, delivered memory versions are the authority for selective approval."""

import hashlib
import json
import re
import unicodedata
import uuid
from collections import Counter
from datetime import timedelta
from functools import lru_cache
from itertools import combinations
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, event, inspect, select

from app.domain_actions import Approval
from app.domain_memory import Entity, Fact, MemoryEntry
from app.models import (
    ApprovalAudit,
    ApprovalPreview,
    History,
    Invocation,
    Job,
    MemoryContext,
    Operation,
    Outbox,
    Tombstone,
    Update,
    now,
)
from app.privacy import guard_invocation, guard_operation, owner_lock
from app.queue import enqueue_text
from app.terminal import revoke
from app.tools import ToolResult


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def normalized(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def resolution_selector(source):
    match = re.fullmatch(
        r"\s*Актуальная\s+версия\s*[-—–:]\s*([^.!?\n]{1,200})[.!]?\s*"
        r"(?:Старую\s+(?:запись|версию)\s+удали(?:те)?|удали(?:те)?\s+старую\s+(?:запись|версию))[.!]?\s*",
        source,
        re.I,
    )
    return match.group(1).strip() if match else None


def resolution_requested(source, selector):
    if resolution_selector(source):
        return normalized(resolution_selector(source)) == normalized(selector)
    command = normalized(source)
    command = re.sub(r"^(?:пожалуйста[, ]*|прошу )", "", command)
    if re.search(r"[«»\"?]|\b(?:не|нельзя|никогда)\s+(?:удал|очист|остав|сохран)", command):
        return False
    if not re.match(r"^(?:оставь(?:те)?|сохрани(?:те)?|удали(?:те)?|актуальная версия)\b", command):
        return False
    if not re.search(r"\bудали(?:те)?\b", command) or not re.search(
        r"\b(?:оставь(?:те)?|сохрани(?:те)?|актуальная версия)\b", command
    ):
        return False
    return all(
        re.search(r"(?<!\w)" + re.escape(token) + r"\w*(?!\w)", command)
        for token in re.findall(r"\w+", normalized(selector))
    )


def snapshot(session, entry):
    facts = session.execute(
        select(Fact, Entity)
        .join(Entity, Fact.entity_id == Entity.id)
        .where(Fact.entry_id == entry.id)
        .order_by(Fact.id)
    ).all()
    return {
        "fingerprint": digest(
            {
                "id": str(entry.id),
                "invocation_id": str(entry.invocation_id),
                "original": entry.original,
                "source": entry.source_text,
                "source_update_id": entry.source_update_id,
                "model": entry.embedding_model,
                "embedding": [float(v) for v in entry.embedding],
                "facts": [
                    {
                        "id": str(f.id),
                        "entity_id": str(e.id),
                        "entity": e.name,
                        "predicate": f.predicate,
                        "value": f.value,
                    }
                    for f, e in facts
                ],
            }
        ),
        "fact_ids": [str(f.id) for f, _ in facts],
        "invocation_id": str(entry.invocation_id),
    }


def remember_context(session, op, invocation_id, entry_ids):
    existing = session.scalar(
        select(MemoryContext).where(MemoryContext.invocation_id == invocation_id)
    )
    if existing:
        return existing
    entries = session.scalars(
        select(MemoryEntry).where(
            MemoryEntry.owner_id == op.owner_id, MemoryEntry.id.in_(entry_ids)
        )
    ).all()
    context = MemoryContext(
        operation_id=op.id,
        invocation_id=invocation_id,
        owner_id=op.owner_id,
        chat_id=op.chat_id,
        context_epoch=op.context_epoch,
        entries={str(e.id): snapshot(session, e) for e in entries},
    )
    session.add(context)
    session.flush()
    return context


def delivered(session, operation):
    rows = session.scalars(
        select(Outbox).where(
            Outbox.operation_id == operation.id,
            Outbox.purpose == "final",
            Outbox.kind == "sendMessage",
            Outbox.terminal_revision == operation.terminal_revision,
        )
    ).all()
    return bool(rows) and all(r.status == "done" and r.acknowledged_at for r in rows)


def legacy_boundary(session, entry):
    producer = session.get(Invocation, entry.invocation_id)
    if not producer or producer.arguments.get("text") != entry.original:
        return False
    expected = sorted(
        (normalized(f["entity"]), normalized(f["predicate"]), f["value"])
        for f in producer.arguments.get("facts", [])
    )
    actual = sorted(
        (e.name, f.predicate, f.value)
        for f, e in session.execute(
            select(Fact, Entity)
            .join(Entity, Fact.entity_id == Entity.id)
            .where(Fact.entry_id == entry.id)
        )
    )
    return actual == expected


def latest_context(session, op, context_id=None):
    state = owner_lock(session, op.owner_id)
    # Legacy SearchMemory results retain exact IDs; never infer authority from model text.
    origins = session.scalars(
        select(Operation)
        .where(
            Operation.owner_id == op.owner_id,
            Operation.chat_id == op.chat_id,
            Operation.status == "done",
            Operation.context_epoch == state.context_epoch,
        )
        .order_by(Operation.received_at.desc())
    ).all()
    for origin in origins:
        if not delivered(session, origin):
            continue
        invocations = session.scalars(
            select(Invocation)
            .where(Invocation.operation_id == origin.id, Invocation.name == "search_memory")
            .order_by(Invocation.ordinal.desc())
        ).all()
        for inv in invocations:
            context = session.scalar(
                select(MemoryContext).where(MemoryContext.invocation_id == inv.id)
            )
            if context is None and inv.result:
                data = inv.result.get("data", {})
                ids = {uuid.UUID(e["id"]) for e in data.get("memory", [])}
                ids.update(uuid.UUID(f["entry_id"]) for f in data.get("facts", []))
                live = {
                    str(e.id): e
                    for e in session.scalars(
                        select(MemoryEntry).where(
                            MemoryEntry.owner_id == op.owner_id, MemoryEntry.id.in_(ids)
                        )
                    )
                }
                if any(
                    e["id"] not in live
                    or live[e["id"]].original != e.get("text")
                    or live[e["id"]].source_text != e.get("source")
                    for e in data.get("memory", [])
                ):
                    continue
                if len(live) != len(ids) or any(
                    not legacy_boundary(session, e) for e in live.values()
                ):
                    continue
                if ids:
                    context = remember_context(session, origin, inv.id, ids)
            if context and (context_id is None or context.id == context_id):
                return context
        if context_id is None and invocations:
            return None
    return None


LEADERSHIP = re.compile(
    r"\b(?:руковод\w*|руковож\w*|директор\w*|возглав\w*|начальник\w*|лидер\w*)\b", re.I
)
UNIT_PATTERN = re.compile(
    r"\b(?P<unit>команд[аыуеой]*|отдел[аыуеом]*|подразделени[еяюим]*|"
    r"департамент[аыуеом]*|компани[яиюей]*|фирм[аыуеой]*|организаци[яиюей]*|"
    r"проект[аыуеом]*|направлени[еяюим]*)\b",
    re.I,
)


def unit_kind(word):
    for prefix, kind in (
        ("команд", "team"),
        ("отдел", "department"),
        ("подразделени", "department"),
        ("департамент", "department"),
        ("компани", "company"),
        ("фирм", "company"),
        ("организаци", "company"),
        ("проект", "project"),
        ("направлени", "direction"),
    ):
        if word.startswith(prefix):
            return kind
    return None


def named_units(value, person_names=()):
    text = normalized(value)
    result = set()
    matches = list(UNIT_PATTERN.finditer(text))
    for index, match in enumerate(matches):
        tail = text[
            match.end() : matches[index + 1].start() if index + 1 < len(matches) else len(text)
        ]
        # Name boundaries are explicit punctuation, the leadership role, or the known subject.
        tail = re.split(r"[,;:.!?\n—–]|\s+-\s+", tail, maxsplit=1)[0]
        role = LEADERSHIP.search(tail)
        if role:
            tail = tail[: role.start()]
        tokens = re.findall(r"\w+", tail)
        for name in person_names:
            person = re.findall(r"\w+", normalized(name))
            if person and len(tokens) >= len(person) and tokens[-len(person) :] == person:
                tokens = tokens[: -len(person)]
        if tokens:
            result.add((unit_kind(match.group("unit")), " ".join(tokens)))
        else:
            return set()
    return result


def claim_scope(session, entry, selector):
    facts = session.execute(
        select(Fact, Entity)
        .join(Entity, Fact.entity_id == Entity.id)
        .where(Fact.entry_id == entry.id)
    ).all()
    subjects = person_subjects(session, entry, facts, selector)
    original_units = named_units(entry.original, subjects)
    projected = set()
    for fact, entity in facts:
        projected.update(named_units(entity.name, subjects))
        if UNIT_PATTERN.fullmatch(normalized(fact.predicate)):
            name = normalized(fact.value).strip('«»" ')
            if name and not UNIT_PATTERN.search(name) and not LEADERSHIP.search(name):
                projected.add((unit_kind(normalized(fact.predicate)), name))
        projected.update(named_units(fact.value, subjects))
    if UNIT_PATTERN.search(entry.original):
        # A projection cannot override or add an independent unit to the original claim.
        return (
            original_units
            if len(original_units) == 1 and projected.issubset(original_units)
            else set()
        )
    return projected if len(projected) == 1 else set()


ROLE_PREDICATES = {
    "роль",
    "должность",
    "позиция",
    "руководитель",
    "директор",
    "начальник",
    "руководит",
    "возглавляет",
    "лидер",
    "role",
    "position",
    "head",
}
IDENTITY_PREDICATES = {"имя", "фио", "name"}
COPULA = {"я", "мы", "это", "есть", "являюсь", "является"}
SELF_SUBJECTS = {"я", "мы", "пользователь", "user"}


def claim_words(value, scope, subjects, require_role=False, scalar_subject=None):
    """Consume the complete claim grammar; unconsumed text is a separate/unknown thought."""
    if re.search(r"[^\w\s,.:;!?«»\"'’—–-]", value):
        return False
    words = re.findall(r"\w+", normalized(value))
    raw = unicodedata.normalize("NFKC", value)
    positions = list(re.finditer(r"\w+", raw))
    if require_role and (
        ";" in raw
        or re.search(r"[.!?]\s+\w", re.sub(r"\b[А-ЯЁA-Z]\.", "", raw))
        or sum(word in {"я", "мы"} for word in words) > 1
    ):
        return False
    role_indices = [i for i, w in enumerate(words) if LEADERSHIP.fullmatch(w)]
    if len(role_indices) > 1 or (require_role and len(role_indices) != 1):
        return False
    unit_indices = [i for i, w in enumerate(words) if UNIT_PATTERN.fullmatch(w)]
    if len(unit_indices) > 1:
        return False
    consumed = set(role_indices)
    kind, name = next(iter(scope))
    name_words = name.split()
    if unit_indices:
        index = unit_indices[0]
        if (
            unit_kind(words[index]) != kind
            or words[index + 1 : index + 1 + len(name_words)] != name_words
        ):
            return False
        consumed.update(range(index, index + 1 + len(name_words)))
    people = set()
    for subject in sorted(subjects, key=len, reverse=True):
        tokens = re.findall(r"\w+", normalized(subject))
        for start in range(len(words) - len(tokens) + 1):
            indices = set(range(start, start + len(tokens)))
            if tokens and not (indices & consumed) and words[start : start + len(tokens)] == tokens:
                if normalized(subject) not in SELF_SUBJECTS:
                    spelling = raw[
                        positions[start].start() : positions[start + len(tokens) - 1].end()
                    ]
                    if (
                        require_role
                        and normalized(subject) != scalar_subject
                        and not person_name(spelling)
                    ):
                        return False
                    if require_role and len(tokens) > 1 and "," in raw[: positions[start].start()]:
                        return False
                    people.add(normalized(subject))
                consumed.update(indices)
    if len(people) > 1:
        return False
    consumed.update(i for i, w in enumerate(words) if w in COPULA)
    # A scalar unit name is one of the allowed structured projections.
    if not require_role and words == name_words:
        return True
    return len(consumed) == len(words)


SPEECH_VERBS = r"(?:сообщил[а]?|сказал[а]?|написал[а]?|уточнил[а]?)"
PERSON_NAME = re.compile(
    r"(?:[А-ЯЁA-Z][а-яёa-z]+(?:[-'’][А-ЯЁA-Z]?[а-яёa-z]+)?|[А-ЯЁA-Z]\.)"
    r"(?:\s+(?:[А-ЯЁA-Z][а-яёa-z]+(?:[-'’][А-ЯЁA-Z]?[а-яёa-z]+)?|[А-ЯЁA-Z]\.)){0,2}"
)
GIVEN_NAMES = {
    "александр",
    "алексей",
    "андрей",
    "анна",
    "антон",
    "артём",
    "артем",
    "валерий",
    "василий",
    "вера",
    "виктор",
    "владимир",
    "дарья",
    "денис",
    "дмитрий",
    "евгений",
    "екатерина",
    "елена",
    "иван",
    "игорь",
    "илья",
    "ирина",
    "кирилл",
    "ксения",
    "мария",
    "михаил",
    "наталья",
    "николай",
    "олег",
    "ольга",
    "павел",
    "пётр",
    "петр",
    "роман",
    "светлана",
    "сергей",
    "софия",
    "татьяна",
    "юлия",
    "юрий",
}
SURNAME = re.compile(
    r"[а-яё]+(?:ов|ев|ёв|ин|ын|ский|цкий|ой|ова|ева|ёва|ина|ына|ская|цкая|ая|"
    r"енко|ян|дзе|швили|ук|юк|ич)"
)


def person_name(value):
    value = value.strip()
    if not PERSON_NAME.fullmatch(value):
        return False
    surnames = 0
    given = False
    for word in normalized(value).split():
        parts = word.split("-")
        if re.fullmatch(r"[а-яёa-z]\.", word):
            continue
        if all(part in GIVEN_NAMES for part in parts):
            given = True
        elif re.fullmatch(r"[а-яё]+(?:ович|евич|овна|евна)", word):
            continue
        elif all(SURNAME.fullmatch(part) for part in parts):
            surnames += 1
        else:
            return False
    return surnames <= 1 and bool(surnames or given)


def person_subjects(session, entry, facts, selector):
    # Entity names are case-folded at storage; recover spelling, never grammar authority.
    arguments = session.scalars(
        select(Invocation.arguments)
        .join(MemoryEntry, MemoryEntry.invocation_id == Invocation.id)
        .join(Fact, Fact.entry_id == MemoryEntry.id)
        .where(
            MemoryEntry.owner_id == entry.owner_id,
            Fact.entity_id.in_({entity.id for _, entity in facts}),
        )
    ).all()
    spellings = [item.get("entity", "") for args in arguments for item in args.get("facts", [])]
    subjects = set(SELF_SUBJECTS)
    if person_name(selector):
        subjects.add(normalized(selector))
    for _, entity in facts:
        name = normalized(entity.name)
        occurrences = re.finditer(r"(?<!\w)" + re.escape(name) + r"(?!\w)", entry.original, re.I)
        if any(person_name(match.group()) for match in occurrences) or any(
            normalized(spelling) == name and person_name(spelling) for spelling in spellings
        ):
            subjects.add(name)
    return subjects


def unit_subject(value, scope):
    words = re.findall(r"\w+", normalized(value))
    kind, name = next(iter(scope))
    return bool(
        words
        and UNIT_PATTERN.fullmatch(words[0])
        and unit_kind(words[0]) == kind
        and words[1:] == name.split()
    )


def attributed_claim(original, narrators):
    # The attribution is grammar, not a second thought, only for a known fact subject.
    for narrator in sorted(narrators, key=len, reverse=True):
        prefix = re.escape(normalized(narrator)) + r"\s+" + SPEECH_VERBS + r"\s*:\s*"
        for opening, closing in (("«", "»"), ('"', '"')):
            wrapper = re.fullmatch(
                prefix + re.escape(opening) + r'([^«»"]+)' + re.escape(closing) + r"\s*[.!]?",
                " ".join(unicodedata.normalize("NFKC", original).split()),
                re.I,
            )
            if wrapper:
                return wrapper.group(1)
    return original


def role_subjects(facts, scope):
    result = set()
    for fact, entity in facts:
        predicate = normalized(fact.predicate)
        if not LEADERSHIP.fullmatch(predicate):
            continue
        # Only a complete unit/leadership relation can project its person into the original.
        if unit_subject(entity.name, scope) and person_name(fact.value):
            result.add(fact.value.strip())
    return result


def single_claim(
    session, entry, scope, selector, scalar_subject=None, slot_entity=None, projected_role=False
):
    facts = session.execute(
        select(Fact, Entity)
        .join(Entity, Fact.entity_id == Entity.id)
        .where(Fact.entry_id == entry.id)
    ).all()
    subjects = person_subjects(session, entry, facts, selector)
    subjects.update(role_subjects(facts, scope))
    if scalar_subject:
        subjects.add(scalar_subject)
    narrators = {
        entity.name
        for fact, entity in facts
        if normalized(entity.name) in subjects
        and (
            normalized(fact.predicate) in ROLE_PREDICATES or UNIT_PATTERN.fullmatch(fact.predicate)
        )
    }
    original_claim = attributed_claim(entry.original, narrators)
    _, name = next(iter(scope))
    # Unquoted multiword names cannot distinguish a unit from an additional clause.
    if len(name.split()) > 1 and not re.search(
        r'[«"]' + re.escape(name) + r'[»"]', normalized(entry.original)
    ):
        return False
    if not claim_words(
        original_claim, scope, subjects, require_role=True, scalar_subject=scalar_subject
    ):
        return False
    kind, name = next(iter(scope))
    for fact, entity in facts:
        predicate = normalized(fact.predicate)
        if (
            normalized(entity.name) not in {normalized(s) for s in subjects}
            and not unit_subject(entity.name, scope)
            and entity.id != slot_entity
        ):
            return False
        if UNIT_PATTERN.fullmatch(predicate):
            if unit_kind(predicate) != kind or normalized(fact.value).strip('«»" ') != name:
                return False
        elif projected_role and leadership_projection(predicate, fact.value):
            if (
                normalized(entity.name) not in {normalized(s) for s in subjects}
                or leadership_projection(predicate, fact.value) != scope
                or not claim_words(fact.value, scope, subjects)
            ):
                return False
        elif predicate in ROLE_PREDICATES or LEADERSHIP.fullmatch(predicate):
            if not claim_words(fact.value, scope, subjects):
                return False
        elif predicate in IDENTITY_PREDICATES:
            subject = normalized(fact.value)
            if subject not in {normalized(s) for s in subjects} or not re.search(
                r"(?<!\w)" + re.escape(subject) + r"(?!\w)", normalized(entry.original)
            ):
                return False
        else:
            return False
    return True


def same_claim(session, old, retain, selector):
    """Match the leadership slot of one named unit, independently of the person/entity."""
    if not all(LEADERSHIP.search(e.original) for e in (old, retain)):
        return False
    if any(
        re.search(
            r"\b(?:заместител\w*|помощник\w*|ассистент\w*|"
            r"техническ\w*|финансов\w*|коммерческ\w*|операционн\w*)\b|"
            r"(?:директор\w*|руководител\w*)\s+по\b",
            e.original,
            re.I,
        )
        for e in (old, retain)
    ):
        return False
    old_scope = claim_scope(session, old, selector)
    retain_scope = claim_scope(session, retain, selector)
    return (
        bool(old_scope)
        and old_scope == retain_scope
        and single_claim(session, old, old_scope, selector)
        and single_claim(session, retain, retain_scope, selector)
    )


class ResolutionArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selector: str = Field(min_length=1, max_length=200)
    context_id: uuid.UUID | None = None
    retain_entry_id: uuid.UUID | None = None


class ShownResolutionArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conflict_ref: str = Field(min_length=1, max_length=64)
    retain_ref: Literal["a", "b"]


def leadership_unit(predicate):
    words = normalized(predicate).split()
    if len(words) == 2 and LEADERSHIP.fullmatch(words[0]) and UNIT_PATTERN.fullmatch(words[1]):
        return unit_kind(words[1])
    return None


def leadership_projection(predicate, value):
    kind = leadership_unit(predicate)
    if kind:
        return {(kind, normalized(value).strip('«»" '))}
    words = re.findall(r"\w+", normalized(value))
    if LEADERSHIP.fullmatch(predicate) and len(words) > 1 and UNIT_PATTERN.fullmatch(words[0]):
        return {(unit_kind(words[0]), " ".join(words[1:]))}
    return None


def projected_claim(session, entry):
    facts = session.execute(
        select(Fact, Entity)
        .join(Entity, Fact.entity_id == Entity.id)
        .where(Fact.entry_id == entry.id)
    ).all()
    for fact, entity in facts:
        person_slot = leadership_projection(fact.predicate, fact.value)
        if person_slot:
            subject = normalized(entity.name)
            spans = re.finditer(r"(?<!\w)" + re.escape(subject) + r"(?!\w)", entry.original, re.I)
            if subject not in SELF_SUBJECTS and not any(
                len(span.group().split()) == 1 and PERSON_NAME.fullmatch(span.group())
                for span in spans
            ):
                continue
            slot_entity = None
        elif LEADERSHIP.fullmatch(fact.predicate):
            if len(fact.value.split()) != 1 or not PERSON_NAME.fullmatch(fact.value.strip()):
                continue
            subject = normalized(fact.value)
            slot_entity = entity.id
        else:
            continue
        scope = named_units(entry.original, [subject])
        if len(scope) != 1:
            continue
        kind, unit = next(iter(scope))
        if person_slot:
            if person_slot != scope:
                continue
        elif not unit_subject(entity.name, scope) and normalized(entity.name) != unit:
            continue
        # The projection is a witness for one whole claim, never a replacement for its grammar.
        if single_claim(
            session,
            entry,
            scope,
            "",
            scalar_subject=subject,
            slot_entity=slot_entity,
            projected_role=True,
        ):
            return scope, "self" if subject in SELF_SUBJECTS else subject
    return None


def projected_pair(session, entries):
    claims = [projected_claim(session, entry) for entry in entries]
    return bool(all(claims) and claims[0][0] == claims[1][0] and claims[0][1] != claims[1][1])


def structured_pair(session, entries, witness):
    facts = session.execute(
        select(Fact, Entity)
        .join(Entity, Fact.entity_id == Entity.id)
        .where(Fact.id.in_([uuid.UUID(i) for i in witness["fact_ids"]]))
    ).all()
    if (
        len(facts) != 2
        or {str(f.entry_id) for f, _ in facts} != {str(e.id) for e in entries}
        or any(
            str(e.id) != witness["entity_id"] or f.predicate != witness["predicate"]
            for f, e in facts
        )
        or len({normalized(f.value) for f, _ in facts}) != 2
    ):
        return False
    scopes = []
    for entry in entries:
        fact, entity = next((f, e) for f, e in facts if f.entry_id == entry.id)
        # A persisted scalar slot supplies the subject, never an arbitrary sentence/entity.
        if (
            not LEADERSHIP.fullmatch(fact.predicate)
            or not PERSON_NAME.fullmatch(fact.value.strip())
            or len(fact.value.split()) != 1
        ):
            return False
        subject = normalized(fact.value)
        scope = named_units(entry.original, [subject])
        if len(scope) != 1:
            return False
        _, unit = next(iter(scope))
        if not unit_subject(entity.name, scope) and normalized(entity.name) != unit:
            return False
        if not single_claim(
            session, entry, scope, "", scalar_subject=subject, slot_entity=entity.id
        ):
            return False
        scopes.append(scope)
    return scopes[0] == scopes[1]


def legacy_pair(session, entries):
    witnesses = set(SELF_SUBJECTS)
    for entry in entries:
        # Only scalar spans already accepted by the historical conservative validator.
        for match in PERSON_NAME.finditer(entry.original):
            if person_name(match.group()):
                witnesses.add(match.group())
    return any(same_claim(session, *entries, selector) for selector in sorted(witnesses))


def validate_pair(session, entries, witness):
    if witness.get("kind") == "structured":
        return structured_pair(session, entries, witness)
    if witness.get("kind") == "projection":
        return projected_pair(session, entries)
    return witness.get("kind") == "legacy" and legacy_pair(session, entries)


def discover_pairs(session, context, conflicts):
    entries = {
        str(e.id): e
        for e in session.scalars(
            select(MemoryEntry).where(
                MemoryEntry.owner_id == context.owner_id,
                MemoryEntry.id.in_([uuid.UUID(i) for i in context.entries]),
            )
        )
    }
    candidates, blocked = {}, set()
    for group in conflicts:
        ids = {f["entry_id"] for f in group}
        if len(ids) != 2 or len(group) != 2:
            blocked.update(ids)
            continue
        if not ids.issubset(entries):
            continue
        ordered = sorted(ids)
        fact = session.get(Fact, uuid.UUID(group[0]["fact_id"]))
        if not fact:
            continue
        witness = {
            "kind": "structured",
            "entity_id": str(fact.entity_id),
            "predicate": fact.predicate,
            "fact_ids": sorted(f["fact_id"] for f in group),
        }
        if validate_pair(session, [entries[i] for i in ordered], witness):
            candidates[tuple(ordered)] = witness
    legacy_ids = sorted(i for i, entry in entries.items() if LEADERSHIP.search(entry.original))
    claims = {i: projected_claim(session, entries[i]) for i in legacy_ids}
    for ids in combinations(legacy_ids, 2):
        left, right = (claims[i] for i in ids)
        if ids not in candidates and left and right and left[0] == right[0] and left[1] != right[1]:
            candidates[ids] = {"kind": "projection"}
        elif ids not in candidates and legacy_pair(session, [entries[i] for i in ids]):
            candidates[ids] = {"kind": "legacy"}
    degree = Counter(i for ids in candidates for i in ids)
    pairs = [
        {
            "conflict_ref": "mc_" + uuid.uuid4().hex[:12],
            "a": ids[0],
            "b": ids[1],
            "validator": witness,
        }
        for ids, witness in candidates.items()
        if not (set(ids) & blocked) and all(degree[i] == 1 for i in ids)
    ]
    context.shown_conflicts = {"schema": 1, "pairs": pairs, "delivery": None}
    return pairs


def pair_records(session, context):
    result = []
    for pair in (context.shown_conflicts or {}).get("pairs", []):
        versions = [session.get(MemoryEntry, uuid.UUID(pair[k])) for k in ("a", "b")]
        if any(
            not entry
            or entry.owner_id != context.owner_id
            or snapshot(session, entry) != context.entries.get(str(entry.id))
            for entry in versions
        ) or not validate_pair(session, versions, pair["validator"]):
            return []
        result.append(
            {
                "conflict_ref": pair["conflict_ref"],
                **{key: entry.original for key, entry in zip(("a", "b"), versions, strict=True)},
            }
        )
    return result


def human_conflict_records(session, context):
    ids = {pair[k] for pair in (context.shown_conflicts or {}).get("pairs", []) for k in ("a", "b")}
    inv = session.get(Invocation, context.invocation_id)
    for group in (inv.result or {}).get("data", {}).get("conflicts", []):
        ids.update(f["entry_id"] for f in group)
    records = []
    for entry_id in sorted(ids):
        entry = session.get(MemoryEntry, uuid.UUID(entry_id))
        if (
            not entry
            or entry.owner_id != context.owner_id
            or snapshot(session, entry) != context.entries.get(entry_id)
        ):
            from app.privacy import ContextChanged

            raise ContextChanged()
        records.append(entry.original)
    return records


def shown_authority(session, context):
    contract = context.shown_conflicts or {}
    delivery = contract.get("delivery")
    origin = session.get(Operation, context.operation_id)
    state = owner_lock(session, context.owner_id)
    if (
        contract.get("schema") != 1
        or not delivery
        or not origin
        or origin.status != "done"
        or context.context_epoch != state.context_epoch
        or origin.context_epoch != state.context_epoch
        or origin.owner_id != context.owner_id
        or origin.chat_id != context.chat_id
        or origin.terminal_revision != delivery.get("revision")
    ):
        return False
    rows = session.scalars(
        select(Outbox)
        .where(
            Outbox.operation_id == origin.id,
            Outbox.purpose == "final",
            Outbox.kind == "sendMessage",
            Outbox.terminal_revision == delivery["revision"],
            Outbox.key.in_(delivery["keys"]),
        )
        .order_by(Outbox.key)
    ).all()
    if (
        not rows
        or [r.key for r in rows] != sorted(delivery["keys"])
        or any(r.status != "done" or not r.acknowledged_at for r in rows)
        or digest([r.payload.get("text") for r in rows]) != delivery["text_hash"]
    ):
        return False
    return bool(pair_records(session, context))


def latest_shown_context(session, op):
    state = owner_lock(session, op.owner_id)
    origins = session.scalars(
        select(Operation)
        .where(
            Operation.owner_id == op.owner_id,
            Operation.chat_id == op.chat_id,
            Operation.id != op.id,
            Operation.status == "done",
            Operation.context_epoch == state.context_epoch,
        )
        .order_by(Operation.received_at.desc(), Operation.id.desc())
    )
    for origin in origins:
        if not delivered(session, origin):
            continue
        searches = session.scalars(
            select(Invocation)
            .where(Invocation.operation_id == origin.id, Invocation.name == "search_memory")
            .order_by(Invocation.ordinal.desc())
        ).all()
        if not searches:
            continue
        context = session.scalar(
            select(MemoryContext).where(MemoryContext.invocation_id == searches[0].id)
        )
        # A delivered later search is the boundary, including a search without a pair.
        return context if context and shown_authority(session, context) else None
    return None


def bind_shown_delivery(session, context, operation, key_prefix):
    records = pair_records(session, context)
    if not records or context.shown_conflicts.get("delivery"):
        return
    session.flush()
    rows = session.scalars(
        select(Outbox)
        .where(
            Outbox.operation_id == operation.id,
            Outbox.key.startswith(key_prefix + ":"),
            Outbox.terminal_revision == operation.terminal_revision,
        )
        .order_by(Outbox.key)
    ).all()
    body = "".join(
        r.payload["text"] for r in sorted(rows, key=lambda r: int(r.key.rsplit(":", 1)[1]))
    )
    if not rows or any(record[k] not in body for record in records for k in ("a", "b")):
        return
    context.shown_conflicts = {
        **context.shown_conflicts,
        "delivery": {
            "revision": operation.terminal_revision,
            "keys": [r.key for r in rows],
            "text_hash": digest([r.payload["text"] for r in rows]),
        },
    }


def prepare_shown_resolution(session, op, invocation_id, args):
    existing = session.scalar(select(Approval).where(Approval.invocation_id == invocation_id))
    if existing:
        preview = session.get(ApprovalPreview, existing.id)
        if existing.status != "pending" or not preview:
            return clarification()
        return approval_result(existing, preview.text)
    # The owner lock serializes duplicate/replayed choices across different operations.
    recorded = session.scalar(
        select(Approval)
        .where(
            Approval.owner_id == op.owner_id,
            Approval.chat_id == op.chat_id,
            Approval.status == "pending",
            Approval.action_kind == "memory_resolution_shown",
            Approval.payload["conflict_ref"].astext == args.conflict_ref,
        )
        .order_by(Approval.id)
        .limit(1)
    )
    if recorded:
        preview = session.get(ApprovalPreview, recorded.id)
        if preview:
            return approval_result(recorded, preview.text)
    context = latest_shown_context(session, op)
    if not context:
        return clarification()
    pair = next(
        (p for p in context.shown_conflicts["pairs"] if p["conflict_ref"] == args.conflict_ref),
        None,
    )
    if not pair:
        return clarification()
    retain = session.get(MemoryEntry, uuid.UUID(pair[args.retain_ref]))
    old = session.get(MemoryEntry, uuid.UUID(pair["b" if args.retain_ref == "a" else "a"]))
    return create_resolution(session, op, invocation_id, context, old, retain, pair)


def choice_label(source):
    """Recognize a complete choice sentence, once, against delivered structured variants."""
    match = re.fullmatch(
        r"\s*([^.!?\n]{1,200}?)\s*[-—–:]\s*"
        r"(?:правильный|верный|актуальный)\s+вариант[.!]?\s*",
        source,
        re.I,
    )
    return normalized(match.group(1)) if match else None


def resolution_state(row):
    # The immutable approval payload is the durable choice; the approval row owns its lifecycle.
    return {
        "choice_recorded": True,
        "state": ("expired" if row.expires_at <= now() else "awaiting_approval")
        if row.status == "pending"
        else row.status,
        "approval_id": str(row.id),
        "keep_entry_id": row.payload["retain_entry_id"],
        "delete_entry_id": row.payload["old_entry_id"],
        "memory_changed": row.status == "executed",
    }


def continue_resolution_turn(session, op):
    update = session.get(Update, op.update_id) if op.update_id is not None else None
    source = update.payload.get("message", {}).get("text", "") if update else ""
    label = choice_label(source)
    confirm = normalized(source).rstrip(".! ") in {
        "да",
        "удалить",
        "да, удалить",
        "подтверждаю",
        "подтвердить",
        "confirm",
        "a",
        "а",
    }
    if not label and not confirm:
        return None
    # A later search/history truncation must never replace an already recorded choice.
    cards = session.scalars(
        select(Approval)
        .where(
            Approval.owner_id == op.owner_id,
            Approval.chat_id == op.chat_id,
            Approval.status == "pending",
            Approval.action_kind.in_({"memory_resolution_shown", "memory_resolution"}),
        )
        .order_by(Approval.expires_at, Approval.id)
    ).all()
    if cards:
        replies = []
        for card in cards:
            preview = session.get(ApprovalPreview, card.id)
            if card.expires_at <= now() or not preview:
                return ToolResult(
                    status="ok",
                    presentation="canonical",
                    data=resolution_state(card),
                    user_message="Выбор версии сохранён, но карточка подтверждения недоступна. "
                    "Проверьте записи заново перед удалением.",
                )
            payload = card.payload
            if payload.get("hash") != digest(
                {k: v for k, v in payload.items() if k != "hash"}
            ) or digest(preview.text) != payload.get("preview_hash"):
                raise ValueError("invalid_resolution_snapshot")
            replies.append(approval_result(card, preview.text))
        if len(replies) == 1:
            return replies[0]
        # No text confirmation guesses which of several concrete approvals to execute.
        return ToolResult(
            status="ok",
            presentation="canonical",
            user_message="Есть несколько карточек. Подтвердите нужное действие кнопкой.\n\n"
            + "\n\n".join(f"{i}. {reply.user_message}" for i, reply in enumerate(replies, 1)),
            buttons=[
                [{**button, "text": f"{button['text']} {i}"} for button in reply.buttons[0]]
                for i, reply in enumerate(replies, 1)
            ],
        )
    if confirm:
        previous = session.scalar(
            select(Approval)
            .join(Operation, Approval.operation_id == Operation.id)
            .where(
                Approval.owner_id == op.owner_id,
                Approval.chat_id == op.chat_id,
                Approval.action_kind.in_({"memory_resolution_shown", "memory_resolution"}),
            )
            .order_by(Operation.received_at.desc(), Approval.id.desc())
            .limit(1)
        )
        if (
            previous
            and previous.payload.get("retain_entry_id")
            and previous.payload.get("old_entry_id")
        ):
            text = {
                "executed": "Эта карточка уже подтверждена. Повторное удаление не выполняется.",
                "cancelled": "Карточка отменена. Удаление по ней не выполнено.",
                "expired": (
                    "Срок подтверждения истёк. Выбор сохранён; проверьте записи перед удалением."
                ),
                "stale": "Выбор сохранён, но записи изменились. Проверьте память перед удалением.",
            }.get(previous.status)
            if text:
                return ToolResult(
                    status="ok",
                    presentation="canonical",
                    data=resolution_state(previous),
                    user_message=text,
                )
        return None
    context = latest_shown_context(session, op)
    if not context:
        return None
    matches = []
    for pair in context.shown_conflicts["pairs"]:
        for key in ("a", "b"):
            entry = session.get(MemoryEntry, uuid.UUID(pair[key]))
            other = session.get(MemoryEntry, uuid.UUID(pair["b" if key == "a" else "a"]))
            labels = {normalized(entry.original)}
            # Use differing stored fact values/subjects, never infer a person's identity via LLM.
            mine = session.execute(
                select(Fact, Entity)
                .join(Entity, Fact.entity_id == Entity.id)
                .where(Fact.entry_id == entry.id)
            ).all()
            theirs = session.execute(
                select(Fact, Entity)
                .join(Entity, Fact.entity_id == Entity.id)
                .where(Fact.entry_id == other.id)
            ).all()
            other_labels = {
                normalized(value) for fact, entity in theirs for value in (fact.value, entity.name)
            }
            labels.update(
                normalized(value)
                for fact, entity in mine
                for value in (fact.value, entity.name)
                if normalized(value) not in other_labels
            )
            if label in labels:
                matches.append((pair, key))
    if len(matches) != 1:
        return None
    pair, retain_ref = matches[0]
    inv = session.scalar(
        select(Invocation).where(
            Invocation.operation_id == op.id, Invocation.call_id == "resolution-choice"
        )
    )
    if inv is None:
        inv = Invocation(
            operation_id=op.id,
            call_id="resolution-choice",
            name="prepare_memory_resolution",
            arguments={"conflict_ref": pair["conflict_ref"], "retain_ref": retain_ref},
        )
        session.add(inv)
        session.flush()
    result = prepare_shown_resolution(session, op, inv.id, ShownResolutionArgs(**inv.arguments))
    inv.result = result.model_dump()
    inv.model_result = {"status": result.status, "data": result.data}
    return result


def clarification():
    return ToolResult(
        status="needs_clarification",
        presentation="canonical",
        user_message=(
            "Уточните, какую показанную версию сохранить и какую старую запись удалить. "
            "Сначала покажу обе записи; удаление требует подтверждения."
        ),
    )


def prepare_resolution(session, op, invocation_id, args):
    update = session.get(Update, op.update_id) if op.update_id is not None else None
    source = update.payload.get("message", {}).get("text", "") if update else ""
    selector = args.selector
    if not resolution_requested(source, selector):
        return clarification()
    context = latest_context(session, op, args.context_id)
    if not context or len(context.entries) < 2:
        return clarification()
    entries = session.scalars(
        select(MemoryEntry).where(
            MemoryEntry.owner_id == op.owner_id,
            MemoryEntry.id.in_([uuid.UUID(i) for i in context.entries]),
        )
    ).all()
    if len(entries) != len(context.entries) or any(
        snapshot(session, e) != context.entries[str(e.id)] for e in entries
    ):
        return clarification()
    tokens = re.findall(r"\w+", normalized(selector))
    matches = [
        e
        for e in entries
        if all(
            re.search(r"(?<!\w)" + re.escape(t) + r"(?!\w)", normalized(e.original)) for t in tokens
        )
    ]
    if len(matches) != 1 or (args.retain_entry_id and matches[0].id != args.retain_entry_id):
        return clarification()
    retain = matches[0]
    old_candidates = [
        e for e in entries if e.id != retain.id and same_claim(session, e, retain, selector)
    ]
    if len(old_candidates) != 1:
        return clarification()
    old = old_candidates[0]
    return create_resolution(session, op, invocation_id, context, old, retain)


def create_resolution(session, op, invocation_id, context, old, retain, pair=None):
    existing = session.scalar(select(Approval).where(Approval.invocation_id == invocation_id))
    if existing:
        preview = session.get(ApprovalPreview, existing.id)
        if existing.status != "pending" or not preview:
            return clarification()
        return approval_result(existing, preview.text)
    preview_text = (
        f"Сохранить актуальную запись:\n{retain.original}\n\n"
        f"Удалить старую запись:\n{old.original}\n\n"
        "Будет удалена одна старая запись и все её факты "
        f"({len(context.entries[str(old.id)]['fact_ids'])}), "
        "и связанные сохранённые копии. "
        "Остальные записи, поручения, документы и встречи сохраняются. "
        "Удаление необратимо. Подтверждение действует 24 часа."
    )
    from app.memory_output import validate_memory_output

    validate_memory_output(preview_text)
    payload = {
        "schema": 2 if pair else 1,
        "owner_id": op.owner_id,
        "chat_id": op.chat_id,
        "context_id": str(context.id),
        "origin_operation_id": str(context.operation_id),
        "origin_invocation_id": str(context.invocation_id),
        "old_entry_id": str(old.id),
        "retain_entry_id": str(retain.id),
        "old": context.entries[str(old.id)],
        "retain": context.entries[str(retain.id)],
        "preview_hash": digest(preview_text),
    }
    if pair:
        payload["shown"] = context.shown_conflicts
        payload["conflict_ref"] = pair["conflict_ref"]
        payload["choice_recorded"] = {
            "keep_entry_id": str(retain.id),
            "delete_entry_id": str(old.id),
        }
    payload["hash"] = digest(payload)
    approval = Approval(
        invocation_id=invocation_id,
        operation_id=op.id,
        owner_id=op.owner_id,
        chat_id=op.chat_id,
        action_kind="memory_resolution_shown" if pair else "memory_resolution",
        payload=payload,
        expires_at=now() + timedelta(hours=24),
    )
    session.add(approval)
    session.flush()
    session.add(ApprovalPreview(approval_id=approval.id, owner_id=op.owner_id, text=preview_text))
    return approval_result(approval, preview_text)


def approval_result(row, preview):
    return ToolResult(
        status="ok",
        presentation="canonical",
        data=resolution_state(row),
        user_message=preview,
        buttons=[
            [
                {"text": "Подтвердить", "callback_data": f"a:{row.id}:y"},
                {"text": "Отменить", "callback_data": f"a:{row.id}:n"},
            ]
        ],
    )


class PrepareMemoryResolution:
    arguments = ShownResolutionArgs
    description = (
        "Зафиксировать ясный выбор актуальной показанной версии и подготовить удаление другой. "
        "Удаление — кнопкой."
    )

    async def prepare(self, ctx, args):
        return args

    def apply(self, session, ctx, args, prepared):
        op = guard_invocation(session, ctx)
        return prepare_shown_resolution(session, op, uuid.UUID(ctx.idempotency_key), args)


class LegacyPrepareMemoryResolution(PrepareMemoryResolution):
    arguments = ResolutionArgs

    def apply(self, session, ctx, args, prepared):
        op = guard_invocation(session, ctx)
        return prepare_resolution(session, op, uuid.UUID(ctx.idempotency_key), args)


def references(value, identities):
    if isinstance(value, dict):
        return any(str(k) in identities or references(v, identities) for k, v in value.items())
    if isinstance(value, list):
        return any(references(v, identities) for v in value)
    return isinstance(value, str) and any(identity in value for identity in identities)


def fact_order_prefix(value, facts):
    counts = Counter(facts)
    choices = tuple(counts)

    @lru_cache(maxsize=None)
    def matches(offset, remaining):
        tail = value[offset:]
        if not tail:
            return True
        for index, fact in enumerate(choices):
            if not remaining[index]:
                continue
            if fact.startswith(tail):
                return True
            token = fact + "\n"
            if tail.startswith(token):
                updated = list(remaining)
                updated[index] -= 1
                if matches(offset + len(token), tuple(updated)):
                    return True
        return False

    return matches(0, tuple(counts[f] for f in choices))


def legacy_render_matches(body, entry, facts):
    from app.memory_overview import render_entry

    header = re.match(r"^Сохранённые записи: \d+\. На этой странице: \d+\.\n\n", body)
    if not header:
        return False
    body = body[header.end() :]
    rendered = render_entry(entry, facts)
    # Exact framed records also handle UTF-16 truncation and embedded separators.
    if re.search(r"(?:^|\n\n)" + re.escape(rendered) + r"(?:\n\n|$)", body):
        return True
    if not facts:
        return False
    prefix = entry.original + "\nФакты (версии сохраняются отдельно):\n"
    fact_lines = [f"{e.name} / {f.predicate}: {f.value}" for f, e in facts]
    expected = Counter(("\n".join(fact_lines)).split("\n"))
    # Legacy queries had no fact ordering contract. Compare full fact lines as a multiset.
    for start in re.finditer(r"(?:^|\n\n)" + re.escape(prefix), body):
        tail = body[start.end() :]
        ends = [m.start() for m in re.finditer(r"\n\n", tail)] + [len(tail)]
        for end in ends:
            candidate = tail[:end]
            if Counter(candidate.split("\n")) == expected:
                return True
            marker = "\n[Запись сокращена для показа]"
            if candidate.endswith(marker):
                fragment = candidate[: -len(marker)]
                units = len((prefix + fragment).encode("utf-16-le")) // 2
                if units in {3459, 3460} and fact_order_prefix(fragment, fact_lines):
                    return True
    return False


def legacy_overview_copies(session, owner, entries, approval_id):
    approval = session.get(Approval, approval_id)
    if not approval:
        return set()
    tracked = set(
        session.scalars(select(MemoryContext.operation_id).where(MemoryContext.owner_id == owner))
    )
    records = [
        (
            entry,
            session.execute(
                select(Fact, Entity)
                .join(Entity, Fact.entity_id == Entity.id)
                .where(Fact.entry_id == entry.id)
            ).all(),
        )
        for entry in entries
    ]
    copied = set()
    for op in session.scalars(
        select(Operation).where(
            Operation.owner_id == owner,
            Operation.chat_id == approval.chat_id,
            Operation.scenario == "memory_overview",
        )
    ):
        # Exact ID provenance takes priority even when another record has identical text.
        if op.id in tracked:
            continue
        groups = {}
        for out in session.scalars(
            select(Outbox).where(
                Outbox.operation_id == op.id,
                Outbox.kind == "sendMessage",
                Outbox.purpose == "final",
            )
        ):
            prefix, _, index = out.key.rpartition(":")
            if index.isdigit() and isinstance(out.payload.get("text"), str):
                groups.setdefault((prefix, out.terminal_revision), []).append(
                    (int(index), out.payload["text"])
                )
        bodies = ["".join(text for _, text in sorted(parts)) for parts in groups.values()]
        bodies.extend(
            h.message.get("content", "")
            for h in session.scalars(select(History).where(History.operation_id == op.id))
            if isinstance(h.message.get("content"), str)
        )
        if any(
            legacy_render_matches(body, entry, facts) for body in bodies for entry, facts in records
        ):
            copied.add(op.id)
    return copied


def scrub_references(session, owner, entry_ids, approval_id, current=None):
    """Closure includes derivative copies created after preparation, never unrelated domain rows."""
    entries = session.scalars(
        select(MemoryEntry).where(MemoryEntry.owner_id == owner, MemoryEntry.id.in_(entry_ids))
    ).all()
    inv_ids = {e.invocation_id for e in entries}
    identities = {str(i) for i in entry_ids | inv_ids}
    identities.update(
        str(i) for i in session.scalars(select(Fact.id).where(Fact.entry_id.in_(entry_ids)))
    )
    source_ops = set(
        session.scalars(select(Invocation.operation_id).where(Invocation.id.in_(inv_ids)))
    )
    source_updates = {e.source_update_id for e in entries if e.source_update_id is not None}
    source_texts = {e.source_text for e in entries if e.source_text}
    for retained in session.scalars(
        select(MemoryEntry).where(MemoryEntry.owner_id == owner, MemoryEntry.id.not_in(entry_ids))
    ):
        producer = session.get(Invocation, retained.invocation_id)
        if (
            retained.source_update_id in source_updates or producer.operation_id in source_ops
        ) and retained.source_text in source_texts:
            retained.source_text = ""
    copied_ops = set(source_ops) | legacy_overview_copies(session, owner, entries, approval_id)
    affected_inv = set(inv_ids)
    for context in session.scalars(
        select(MemoryContext).where(MemoryContext.owner_id == owner)
    ).all():
        if references(context.entries, identities) or references(
            context.shown_conflicts, identities
        ):
            copied_ops.add(context.operation_id)
            affected_inv.add(context.invocation_id)
            session.delete(context)
    for card in session.scalars(select(Approval).where(Approval.owner_id == owner)).all():
        if references(card.payload, identities):
            copied_ops.add(card.operation_id)
            affected_inv.add(card.invocation_id)
            session.query(ApprovalPreview).filter(ApprovalPreview.approval_id == card.id).delete()
            if card.id != current and card.status == "pending":
                card.status, card.stale_reason = "stale", "context_changed"
                session.add(
                    ApprovalAudit(
                        callback_id=f"stale:{approval_id}:{card.id}",
                        approval_id=card.id,
                        operation_id=card.operation_id,
                        outcome="context_changed",
                    )
                )
    for inv in session.scalars(
        select(Invocation).join(Operation).where(Operation.owner_id == owner)
    ).all():
        if (
            inv.id in affected_inv
            or references(inv.result, identities)
            or references(inv.model_result, identities)
        ):
            affected_inv.add(inv.id)
            copied_ops.add(inv.operation_id)
            inv.arguments, inv.result, inv.model_result = {}, {}, {}
    from app.privacy import fence_memory_writes

    fence_memory_writes(session, owner, copied_ops, approval_id)
    for op_id in copied_ops:
        operation = session.get(Operation, op_id)
        if operation.update_id is not None:
            update = session.get(Update, operation.update_id)
            update.payload = {}
        session.execute(delete(History).where(History.operation_id == op_id))
        overview_revoked = False
        for out in session.scalars(select(Outbox).where(Outbox.operation_id == op_id)):
            if out.key.startswith("task:") or out.key.startswith(f"{op_id}:terminal-error"):
                continue
            if out.status in {"pending", "running"}:
                if operation.scenario == "memory_overview" and out.purpose == "final":
                    overview_revoked = True
                revoke(out)
            out.payload = {}
        if overview_revoked:
            from app.terminal import terminal_error

            terminal_error(
                session,
                operation,
                "context_changed",
                recovery=not session.get(Tombstone, f"operation:{operation.id}"),
            )
        elif operation.scenario == "memory_overview":
            from app.latency import update_delivery

            # Redacting the stored body does not erase a real historical Telegram receipt.
            update_delivery(session, operation)
        for job in session.scalars(select(Job).where(Job.operation_id == op_id)):
            if job.kind == "data_cleanup":
                continue
            job.payload = {}
            live_mutations = session.scalar(
                select(Invocation.id)
                .where(
                    Invocation.operation_id == op_id,
                    Invocation.id.not_in(affected_inv),
                    Invocation.name.in_({"save_memory", "create_task", "prepare_meeting"}),
                    Invocation.result.is_not(None),
                )
                .limit(1)
            )
            if not live_mutations and job.status in {"pending", "running"}:
                revoke(job)
    for inv_id in inv_ids:
        key = f"invocation:{inv_id}"
        if not session.get(Tombstone, key):
            session.add(Tombstone(key=key, owner_id=owner, approval_id=approval_id))


def execute_resolution(session, row):
    p = row.payload
    if type(p.get("schema")) is not int or (row.action_kind, p.get("schema")) not in {
        ("memory_resolution", 1),
        ("memory_resolution_shown", 2),
    }:
        raise ValueError("invalid_resolution_version")
    if (
        p.get("owner_id") != row.owner_id
        or p.get("chat_id") != row.chat_id
        or p.get("hash") != digest({k: v for k, v in p.items() if k != "hash"})
    ):
        raise ValueError("invalid_resolution_snapshot")
    context = session.get(MemoryContext, uuid.UUID(p["context_id"]))
    if context and (
        context.owner_id != row.owner_id
        or context.chat_id != row.chat_id
        or str(context.operation_id) != p["origin_operation_id"]
        or str(context.invocation_id) != p["origin_invocation_id"]
    ):
        raise ValueError("invalid_resolution_context")
    if (
        not context
        or context.entries.get(p["old_entry_id"]) != p["old"]
        or context.entries.get(p["retain_entry_id"]) != p["retain"]
    ):
        row.status, row.stale_reason = "stale", "context_changed"
        session.query(ApprovalPreview).filter(ApprovalPreview.approval_id == row.id).delete()
        return False
    if p["schema"] == 2 and (
        context.shown_conflicts != p.get("shown")
        or not shown_authority(session, context)
        or not any(
            pair["conflict_ref"] == p.get("conflict_ref")
            and {pair["a"], pair["b"]} == {p["old_entry_id"], p["retain_entry_id"]}
            for pair in (context.shown_conflicts or {}).get("pairs", [])
        )
    ):
        row.status, row.stale_reason = "stale", "context_changed"
        session.query(ApprovalPreview).filter(ApprovalPreview.approval_id == row.id).delete()
        return False
    old = session.get(MemoryEntry, uuid.UUID(p["old_entry_id"]))
    retain = session.get(MemoryEntry, uuid.UUID(p["retain_entry_id"]))
    if (
        not old
        or not retain
        or old.id == retain.id
        or any(e.owner_id != row.owner_id for e in (old, retain))
        or snapshot(session, old) != p["old"]
        or snapshot(session, retain) != p["retain"]
    ):
        row.status, row.stale_reason = "stale", "context_changed"
        session.query(ApprovalPreview).filter(ApprovalPreview.approval_id == row.id).delete()
        return False
    preview = session.get(ApprovalPreview, row.id)
    if not preview or digest(preview.text) != p["preview_hash"]:
        raise ValueError("invalid_resolution_preview")
    state = owner_lock(session, row.owner_id)
    state.context_epoch += 1
    scrub_references(session, row.owner_id, {old.id}, row.id, current=row.id)
    key = f"memory:{old.id}"
    if not session.get(Tombstone, key):
        session.add(Tombstone(key=key, owner_id=row.owner_id, approval_id=row.id))
    session.execute(delete(Fact).where(Fact.entry_id == old.id))
    session.delete(old)
    session.flush()
    session.execute(
        delete(Entity).where(
            Entity.owner_id == row.owner_id, ~Entity.id.in_(select(Fact.entity_id))
        )
    )
    row.status, row.approved_at, row.executed_at = "executed", now(), now()
    return True


async def prepare_job(sessions, job, lease):
    with sessions.begin() as session:
        op = guard_operation(session, job.operation_id, lease)
        inv = session.scalar(
            select(Invocation).where(
                Invocation.operation_id == op.id, Invocation.call_id == "resolution-command"
            )
        )
        if inv is None:
            inv = Invocation(
                operation_id=op.id,
                call_id="resolution-command",
                name="prepare_memory_resolution",
                arguments={},
            )
            session.add(inv)
            session.flush()
        selector = resolution_selector(job.payload.get("message", {}).get("text", ""))
        result = prepare_resolution(session, op, inv.id, ResolutionArgs(selector=selector or "?"))
        inv.result = result.model_dump()
        enqueue_text(session, op, result.user_message, result.buttons)
        op.scenario, op.status = "memory_resolution", "done"


@event.listens_for(ApprovalPreview, "before_update")
def immutable_preview(mapper, connection, target):
    if any(
        inspect(target).attrs[k].history.has_changes() for k in ("text", "owner_id", "approval_id")
    ):
        raise ValueError("immutable_approval_preview")
