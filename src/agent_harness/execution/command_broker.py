"""Command Broker: only registered ``CommandSpec`` IDs may ever execute.

Callers never supply a raw shell string or an arbitrary argv — they name
a ``command_id`` already present in the ``CommandCatalog`` (an admin-owned
registry, per architecture review section 13's "테스트 명령의 최종
소유자는 관리자 소유 command_catalog.yaml") and a dict of typed
parameters. Parameter values are substituted into ``argv_template`` tokens
via ``str.format_map`` — Python does not re-interpret the substituted
value as more format syntax or shell syntax, so a parameter value like
``"; rm -rf /"`` ends up as one literal argv element, never parsed by a
shell (H-04; see ``tests/unit/test_command_broker.py`` for a direct proof).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from agent_harness.domain.enums import ArtifactMediaKind
from agent_harness.domain.models import Artifact, CommandRun, CommandSpec
from agent_harness.execution.sandbox import SandboxBackend
from agent_harness.execution.process import ProcessLimits, ProcessResult
from agent_harness.persistence.artifacts import write_blob

__all__ = [
    "UnknownCommandError",
    "InvalidCommandParametersError",
    "CommandCatalog",
    "CommandExecution",
    "resolve_argv",
    "execute_command",
]


@dataclass(frozen=True)
class CommandExecution:
    command_run: CommandRun
    process_result: ProcessResult
    stdout_artifact: Artifact | None = None
    stderr_artifact: Artifact | None = None


class UnknownCommandError(LookupError):
    """Raised when a caller names a command_id not present in the catalog.

    There is no fallback to raw execution — an unknown command is refused,
    never run "as-is".
    """


class InvalidCommandParametersError(ValueError):
    pass


class CommandCatalog:
    """An in-memory registry of admin-approved ``CommandSpec``s.

    Loading a catalog from ``command_catalog.yaml`` (per the architecture
    review's config layout) is out of scope for this phase — callers
    populate this programmatically for now.
    """

    def __init__(self) -> None:
        self._specs: dict[str, CommandSpec] = {}

    def register(self, spec: CommandSpec) -> None:
        self._specs[spec.command_id] = spec

    def get(self, command_id: str) -> CommandSpec:
        try:
            return self._specs[command_id]
        except KeyError:
            raise UnknownCommandError(command_id) from None


def resolve_argv(spec: CommandSpec, parameters: dict[str, str]) -> list[str]:
    """Substitute ``parameters`` into ``spec.argv_template`` token-by-token.

    Each argv element is formatted independently — a parameter value is
    never concatenated into a larger string that could straddle two argv
    tokens, and it is inserted as literal text, not re-parsed.
    """

    try:
        return [token.format_map(parameters) for token in spec.argv_template]
    except KeyError as exc:
        raise InvalidCommandParametersError(f"missing parameter: {exc}") from exc


def execute_command(
    catalog: CommandCatalog,
    command_id: str,
    parameters: dict[str, str],
    *,
    sandbox: SandboxBackend,
    cwd: Path,
    available_env: dict[str, str],
    now: datetime,
    max_output_bytes: int = 1_000_000,
    artifact_data_root: Path | None = None,
) -> CommandExecution:
    """Resolve, execute, and record one command run.

    ``available_env`` is the pool a caller is willing to offer (e.g.
    ``os.environ``); only the keys in ``spec.env_allowlist`` are actually
    passed to the child — this is the "env scrub" requirement, not a full
    environment passthrough.

    If ``artifact_data_root`` is given, stdout/stderr are persisted as
    blobs (Phase 2.2) and referenced from the returned ``CommandRun``;
    otherwise the refs are left ``None`` and only ``ProcessResult`` carries
    the raw bytes.
    """

    spec = catalog.get(command_id)
    argv = resolve_argv(spec, parameters)
    scrubbed_env = {
        key: available_env[key] for key in spec.env_allowlist if key in available_env
    }
    limits = ProcessLimits(timeout_seconds=spec.timeout_seconds, max_output_bytes=max_output_bytes)

    result = sandbox.run(argv, cwd=cwd, env=scrubbed_env, limits=limits)

    stdout_artifact: Artifact | None = None
    stderr_artifact: Artifact | None = None
    if artifact_data_root is not None:
        stdout_artifact = write_blob(
            artifact_data_root,
            result.stdout,
            media_type="text/plain",
            media_kind=ArtifactMediaKind.LOG,
            now=now,
        )
        stderr_artifact = write_blob(
            artifact_data_root,
            result.stderr,
            media_type="text/plain",
            media_kind=ArtifactMediaKind.LOG,
            now=now,
        )

    command_run = CommandRun(
        command_spec_id=spec.command_id,
        resolved_argv=argv,
        exit_code=result.exit_code,
        duration_ms=result.duration_ms,
        stdout_artifact_ref=stdout_artifact.artifact_id if stdout_artifact else None,
        stderr_artifact_ref=stderr_artifact.artifact_id if stderr_artifact else None,
        started_at=result.started_at,
        completed_at=result.completed_at,
    )
    return CommandExecution(
        command_run=command_run,
        process_result=result,
        stdout_artifact=stdout_artifact,
        stderr_artifact=stderr_artifact,
    )
