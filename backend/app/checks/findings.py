from dataclasses import dataclass, field


@dataclass
class Finding:
    code: str
    score: float  # 0..1; 0.6 or more opens an exception
    reason: str
    related_ids: list[str] = field(default_factory=list)


THRESHOLD = 0.6
ACCEPTED = ("matched", "pending_approval", "approved")
