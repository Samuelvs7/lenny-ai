"""Search-query construction for follow-up questions.

**The problem this solves.** Generation gets the conversation history, but
*retrieval* only ever saw the latest message. So a conversation like:

    User: What did guests say about finding product-market fit?
    User: What about for B2B specifically?

searched the corpus for "B2B specifically" — losing the topic entirely and
returning passages about LinkedIn ads. The answer was grounded in the wrong
thing, which is worse than not answering.

**The fix.** When a message reads as a follow-up rather than a self-contained
question, the retrieval query is built from the conversation's topic plus the
new message. Generation still receives the real history; only the *search* text
is rewritten.

**Why a heuristic and not an LLM rewrite.** Asking the model to condense the
conversation into a standalone query is the textbook approach and is more
accurate. It also costs a full extra generation round trip — 30-90 seconds on
the CPU-bound local model that is the mandated demo path — before retrieval can
even start. That more than doubles perceived latency to fix a subset of turns.
The heuristic below costs microseconds, is deterministic, and is trivially
testable. If this ran primarily on a cloud model, an LLM rewrite would be the
better trade; the switch would be contained to this module.
"""

from __future__ import annotations

import re

from app.domain import Message, MessageRole

#: A message this short is very unlikely to carry its own topic.
_SHORT_QUESTION_WORDS = 7

#: Openers that explicitly refer back to something already discussed.
_FOLLOW_UP_OPENERS = re.compile(
    r"^\s*(what about|how about|and (what|how|why)|what if|why (is|does|do|would) (that|it|this)"
    r"|tell me more|say more|go deeper|expand on (that|this|it)"
    r"|(can|could) you (elaborate|expand)|same (question|thing) (for|but)"
    r"|(ok|okay|and|but|so)\b)",
    re.IGNORECASE,
)

#: Pronouns and deictics with no antecedent in the message itself.
_DANGLING_REFERENCE = re.compile(
    r"\b(that|those|this|these|it|its|they|them|their|he|she|his|her|the same|instead)\b",
    re.IGNORECASE,
)

#: Words that carry no topical signal when we splice two questions together.
_NOISE = re.compile(
    r"^(what|how|why|when|who|which|where|about|for|the|a|an|is|are|do|does|did|"
    r"can|could|should|would|specifically|exactly|instead|more|else|with|"
    # Conversational scaffolding that carries no topic. Without these, a rewrite
    # of "What did guests say about X?" splices in "guests say", which adds
    # nothing to retrieval and dilutes the embedding.
    r"guest|guests|say|says|said|tell|talk|talks|discuss|mention|mentioned|"
    r"episode|episodes|podcast|lenny)$",
    re.IGNORECASE,
)


def is_follow_up(question: str) -> bool:
    """Does this message depend on earlier turns to make sense?

    Conservative: a self-contained question must keep its own exact wording,
    because splicing an unrelated previous topic into it would *degrade*
    retrieval. Only clear dependents are rewritten.
    """
    text = " ".join(question.split())
    if not text:
        return False

    if _FOLLOW_UP_OPENERS.match(text):
        return True

    words = text.split()
    return bool(
        len(words) <= _SHORT_QUESTION_WORDS and _DANGLING_REFERENCE.search(text)
    )


def _topic_terms(text: str, limit: int = 12) -> list[str]:
    """Content words from an earlier question, for splicing into a new query."""
    terms: list[str] = []
    seen: set[str] = set()
    for raw in re.findall(r"[A-Za-z0-9][A-Za-z0-9'’-]*", text):
        token = raw.strip("'’-")
        key = token.lower()
        if not token or key in seen or _NOISE.match(key) or len(token) < 3:
            continue
        seen.add(key)
        terms.append(token)
        if len(terms) >= limit:
            break
    return terms


def _last_grounded_question(history: list[Message]) -> str | None:
    """The most recent user question that actually produced a grounded answer.

    **Not simply the most recent user message.** A question the assistant
    *declined* established no topic, so splicing it into a follow-up actively
    poisons retrieval. Observed live: a session went

        1. "What did guests say about finding product-market fit?"  -> answered
        2. "What is the best recipe for sourdough bread?"           -> DECLINED
        3. "What about for B2B specifically?"                       -> follow-up

    and step 3 was rewritten using the *sourdough* terms, dropping similarity
    from 0.70 to 0.49 and causing a spurious refusal. Walking back past
    declined turns is what makes the refusal signal useful rather than harmful.
    """
    declined_questions: set[str] = set()
    for index, message in enumerate(history):
        if message.role is not MessageRole.USER:
            continue
        reply = next(
            (m for m in history[index + 1 :] if m.role is MessageRole.ASSISTANT), None
        )
        if reply is not None and reply.metadata.get("declined"):
            declined_questions.add(message.content)

    for message in reversed(history):
        if message.role is MessageRole.USER and message.content not in declined_questions:
            return message.content
    return None


def build_retrieval_query(question: str, history: list[Message]) -> str:
    """The text to search the corpus with.

    Returns ``question`` unchanged for self-contained messages. For follow-ups,
    returns the previous grounded question's topic terms followed by the new
    message, so retrieval sees both the subject and the qualifier.
    """
    if not history or not is_follow_up(question):
        return question

    previous = _last_grounded_question(history)
    if not previous:
        return question

    terms = _topic_terms(previous)
    if not terms:
        return question

    return f"{' '.join(terms)} {question}".strip()
