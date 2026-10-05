"""Keep internal memory references and execution envelopes out of visible text."""

import re

from app.terminal import InvalidFinal

INTERNAL_OUTPUT = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b|"
    r"\bmc_[0-9a-f]{12}\b|"
    r"Противоречащие записи памяти|"
    r"\b(?:conflict_ref|retain_ref|entry_id|context_id|approval_id|callback_data|"
    r"tool_calls|fact_ids|preview_hash|idempotency_key|memory_resolution_shown)\b|"
    r'"(?:type|name)"\s*:\s*"(?:tool|prepare_memory_resolution|search_memory|save_memory)"',
    re.I,
)


def validate_memory_output(text):
    if INTERNAL_OUTPUT.search(text):
        raise InvalidFinal()
    return text


def pending_claim(prefix, *, passive=False, denial=False):
    words = re.findall(r"\w+", prefix.casefold())
    if denial:
        adjacent = words[:-1] if words and words[-1] in {"был", "была", "было", "были"} else words
        if adjacent and adjacent[-1] == "не":
            return True
    purpose = re.split(r"\bчтобы\b", prefix, flags=re.I)
    if len(purpose) > 1 and not re.search(r"\b(?:уже|теперь|сейчас)\b", purpose[-1], re.I):
        return True
    if "бы" in words[-3:]:
        return True
    # Future auxiliaries scope a passive/result state, not a neighboring past action.
    return passive and bool(
        re.search(
            r"\b(?:будет|будут|останется|останутся|сохранится|сохранятся)\b(?:\s+\w+){0,4}\s*$",
            prefix,
            re.I,
        )
    )


def validate_pending_output(text):
    completed = re.compile(
        r"\b(?:удал(?:ил[аи]?|ен[аоы]?|ён[аоы]?)|убрал[аи]?|"
        r"ст[её]р(?:ла|ло|ли|т[аоы]?)?|стирал[аи]?|очистил[аи]?|очищен[аоы]?|"
        r"исчез(?:ла|ло|ли)?|уничтож(?:ил[аи]?|ен[аоы]?|ён[аоы]?)|"
        r"выполн(?:ен[аоы]?|ил[аи]?))\b",
        re.I,
    )
    for clause in re.split(r"[.!?;:\n,]|\b(?:но|однако|зато)\b", text, flags=re.I):
        for match in completed.finditer(clause):
            prefix = clause[: match.start()]
            if match.group().casefold().startswith("выполн") and not re.search(
                r"действие|удаление|уже", clause, re.I
            ):
                continue
            passive = bool(re.search(r"(?:[её]н[аоы]?|т[аоы]?)$", match.group(), re.I))
            if pending_claim(prefix, passive=passive, denial=True):
                continue
            raise InvalidFinal()
        if re.search(r"без подтверждения", clause, re.I) and not re.search(
            r"\b(?:не|нельзя)\b", clause, re.I
        ):
            raise InvalidFinal()
        if re.search(r"кнопк.*не нуж", clause, re.I):
            raise InvalidFinal()
        # Absence is a completed-state assertion, even when grammatically negative.
        if re.search(r"\b(?:запис\w*|верси\w*|данн\w*|памят\w*)\b", clause, re.I):
            absence = re.finditer(
                r"\b(?:больше\s+нет|не\s+остал\w*|(?:больше\s+)?не\s+(?:существу|хран)\w*)\b",
                clause,
                re.I,
            )
            for match in absence:
                if not pending_claim(clause[: match.start()], passive=True):
                    raise InvalidFinal()
            exclusive = re.finditer(
                r"\b(?:только|лишь|одна|единственная)\s+(?:\w+\s+){0,3}(?:запис\w*|верси\w*)\b",
                clause,
                re.I,
            )
            for match in exclusive:
                prefix = clause[: match.start()]
                suffix = clause[match.end() :]
                future = re.search(
                    r"\b(?:будет|будут|останется|останутся|сохранится|сохранятся)\b", suffix, re.I
                )
                current = re.search(
                    r"\b(?:остал(?:ся|ась|ось|ись)|хранится|сохран[её]н\w*|уже|теперь|сейчас)\b",
                    suffix,
                    re.I,
                )
                future_suffix = future and (not current or future.start() < current.start())
                if re.search(
                    r"\b(?:в\s+памят\w*|остал\w*|хран\w*|сохран[её]н\w*|теперь|сейчас)\b",
                    clause,
                    re.I,
                ) and not (pending_claim(prefix, passive=True, denial=True) or future_suffix):
                    raise InvalidFinal()
    return text
