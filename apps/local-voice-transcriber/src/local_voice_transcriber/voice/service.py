"""Прикладные операции общего голосового справочника.

Сервис не выполняет diarization и не создаёт sample по результату match. Он
принимает только явное пользовательское подтверждение и возвращает безопасные
проекции, пригодные для общего directory UI.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .repository import (
    ProfileState,
    SharedVoiceProfile,
    VoiceDirectoryRepository,
    VoiceProfileStateError,
    VoiceSampleEnrollment,
)


class ExplicitSampleConfirmationRequiredError(PermissionError):
    """Образец нельзя добавить неявно из candidate или auto-assignment."""


@dataclass(frozen=True, slots=True)
class ProfileTransitionResult:
    """Безопасный результат archive/restore без record ID и source metadata."""

    profile: SharedVoiceProfile
    pending_operation_ids: tuple[str, ...]


class VoiceDirectoryService:
    """Сценарии shared directory, независимые от FastAPI и реального ZeusDB."""

    def __init__(self, repository: VoiceDirectoryRepository) -> None:
        self._repository = repository

    def create_profile(
        self,
        *,
        profile_id: str,
        display_name: str,
        actor_id: str,
        aliases: Sequence[str] = (),
    ) -> SharedVoiceProfile:
        """Создаёт общую карточку, доступную другим пользователям только как projection."""

        return self._repository.create_profile(
            profile_id=profile_id,
            display_name=display_name,
            created_by=actor_id,
            aliases=aliases,
        )

    def list_profiles(
        self,
        *,
        query: str = "",
        state: ProfileState | None = ProfileState.ACTIVE,
    ) -> list[SharedVoiceProfile]:
        """Возвращает безопасный список каталога, не зависящий от владельца job."""

        return self._repository.list_shared_profiles(query=query, state=state)

    def rename_profile(
        self, *, profile_id: str, display_name: str, actor_id: str
    ) -> SharedVoiceProfile:
        """Переименовывает только active profile."""

        return self._repository.rename_profile(
            profile_id=profile_id,
            display_name=display_name,
            actor_id=actor_id,
        )

    def add_alias(self, *, profile_id: str, alias: str, actor_id: str) -> SharedVoiceProfile:
        """Добавляет глобальный псевдоним с аудитом."""

        return self._repository.add_alias(profile_id=profile_id, alias=alias, actor_id=actor_id)

    def add_explicit_sample(
        self,
        *,
        explicit_confirm: bool,
        sample_id: str,
        operation_id: str,
        profile_id: str,
        voice_pack_id: str,
        protected_artifact_id: str,
        artifact_content_sha256: str,
        protected_path: str,
        quality: Mapping[str, object],
        actor_id: str,
        source_group_id: str | None = None,
        source_interval_id: str | None = None,
        retention_until: str | None = None,
    ) -> VoiceSampleEnrollment:
        """Ставит в очередь создание reference sample только по явному действию.

        Проверку, что source group/interval относится к job вызывающего пользователя,
        выполняет HTTP/worker слой до вызова этого метода. Сервис требует хотя бы
        одну source-ссылку, чтобы нельзя было незаметно пополнить справочник из
        автоматического кандидата.
        """

        if not explicit_confirm:
            raise ExplicitSampleConfirmationRequiredError(
                "Добавление голосового образца требует явного подтверждения"
            )
        if source_group_id is None and source_interval_id is None:
            raise ValueError("Нужна локальная группа или diarization-интервал пользователя")
        return self._repository.create_explicit_sample(
            sample_id=sample_id,
            profile_id=profile_id,
            voice_pack_id=voice_pack_id,
            protected_artifact_id=protected_artifact_id,
            artifact_content_sha256=artifact_content_sha256,
            protected_path=protected_path,
            quality=quality,
            created_by=actor_id,
            operation_id=operation_id,
            source_group_id=source_group_id,
            source_interval_id=source_interval_id,
            retention_until=retention_until,
        )

    def archive_profile(
        self, *, profile_id: str, actor_id: str, reason: str
    ) -> ProfileTransitionResult:
        """Немедленно скрывает archive-профиль из matching read-model."""

        transition = self._repository.archive_profile(
            profile_id=profile_id,
            actor_id=actor_id,
            reason=reason,
        )
        return ProfileTransitionResult(
            profile=transition.profile,
            pending_operation_ids=tuple(operation.id for operation in transition.operations),
        )

    def restore_profile(
        self, *, profile_id: str, actor_id: str, reason: str
    ) -> ProfileTransitionResult:
        """Возвращает archive-профиль; writer соберёт новое поколение по outbox."""

        transition = self._repository.restore_profile(
            profile_id=profile_id,
            actor_id=actor_id,
            reason=reason,
        )
        return ProfileTransitionResult(
            profile=transition.profile,
            pending_operation_ids=tuple(operation.id for operation in transition.operations),
        )

    def require_active_profile(self, profile_id: str) -> SharedVoiceProfile:
        """Удобный guard для group decision: archived profile выбрать нельзя."""

        profile = self._repository.get_shared_profile(profile_id)
        if profile.state is not ProfileState.ACTIVE:
            raise VoiceProfileStateError("Выбрать можно только активный голосовой профиль")
        return profile
