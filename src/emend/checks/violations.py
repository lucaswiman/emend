"""Policy results shared by the runner and individual check implementations."""

from dataclasses import dataclass, field


@dataclass
class PolicyViolation:
    """A single policy violation."""
    file_path: str
    line: int
    col: int
    policy_name: str
    check_name: str
    severity: str
    message: str
    witness: list[str] = field(default_factory=list)
